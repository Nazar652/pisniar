# AGENTS.md

## What this is

`pisniar` — a Telegram bot that automates posting music tracks to a channel:
resolve a link's metadata, prepare a post, render a vertical video fragment
(the cover, the audio and synced lyrics under the cover), publish.

## Stack and runtime

- Python 3.13 (`>=3.13,<3.14`), dependency/venv management via `uv`.
- src-layout, import package `pisniar` (`src/pisniar/`).
- aiogram 3 (long polling) + FastAPI (health checks), Celery + Redis
  (worker), SQLAlchemy 2.0 async + asyncpg, Alembic.
- `yt-dlp` (pinned in `uv.lock`, **never self-updating** — `--no-update` is
  passed explicitly) plus `ffmpeg`/`ffprobe`, both run as subprocesses and both
  present **only in the worker image**. The `bot` image has neither, and the
  `media-data` volume (`/data`) is mounted only into `worker`.
- Postgres and Redis run **only in Docker**; never install them on the host.
- `REDIS_PASSWORD` in `.env` is a hard prerequisite: `compose.yaml` starts
  Redis with `--requirepass` and builds `REDIS_URL` from it, so `docker
  compose` refuses to start without it. The broker is published on
  `127.0.0.1:56379` by `compose.override.yml`, and anything that can enqueue
  a task there makes the worker act without passing `AdminAllowlistMiddleware`.
  The healthcheck authenticates through `REDISCLI_AUTH`, never `redis-cli -a`,
  and `config.redaction_secrets()` carries the password so it cannot surface
  in a kombu traceback or in `readyz_redis_unavailable` (`exc_info=True`).

## Commands

Canonical commands are `make` targets (`make help` lists all of them).

```
make check       # uv sync --frozen + uv lock --check + ruff format --check + ruff check + ty check + unit tests
make up           # docker compose up -d --wait
make ps            # docker compose ps
make test-int      # full test suite (unit + integration) inside the bot container
make test-fonts    # the subtitle font and the glyph-width table, inside the worker container
make migrate       # alembic upgrade head inside the bot container
make revision m="add lyric table"   # alembic revision --autogenerate
make alembic-check # alembic check: model/migration drift
make shell         # shell in the bot container
make verify        # full local + Docker verification in one entrypoint
```

The type checker is **`ty`** (Astral), not `mypy` and not `pyright` — do not
suggest or introduce either.

## Architectural boundaries

- `pisniar/api/` never imports `pisniar/bot/handlers` — this is what keeps
  the health/readiness API extractable into its own service without a
  rewrite.
- `pisniar/worker/` never imports `pisniar/bot/`, and `pisniar/bot/` never
  imports `pisniar/worker/tasks` directly. The bot dispatches Celery tasks by
  name through `pisniar.worker.celery_app`, not by importing task functions.
- `pisniar/bot/` imports only `pisniar.resolve.urls` (syntactic
  classification, no network) and `pisniar.telegram.keyboards` (the
  `callback_data` grammar, `aiogram` only) — the full resolve pipeline
  (`resolve.service`/`resolve.registry`, the six source adapters, `post/`
  caption building) runs only in `pisniar/worker/`.
- `pisniar/resolve/` (six source adapters + ISRC-first matching/merge, plus
  the LRCLIB adapter and the LRC parser) and `pisniar/post/`
  (caption/hashtags/sanitize/render) never import `bot/` or `worker/` — they
  are pure libraries the worker orchestrates. LRCLIB is not one of the six:
  `resolve.service` never calls it, only `worker.lyrics` does, at render time.
- The one edge back the other way is `post.sanitize`, which `resolve/` may
  import (`musicbrainz` logs a source-supplied URL through it) — and it is
  the **only** `post/` module allowed there. That works because
  `post.sanitize` is a leaf: it imports `pisniar.resolve.urls` and the
  stdlib, nothing else. Keep it one, and keep it a leaf — a second import
  or a heavier dependency turns `resolve/` <-> `post/` into a cycle.
- A post's status changes **only** through `set_post_status()`
  (`pisniar.db.states`). Never assign `post.status = ...` directly — it skips
  transition validation and the `last_error` bookkeeping that function owns.
- `RESOLVED` means **the draft is already in the admin's chat**, so
  `deliver_draft()` runs *before* `set_post_status(RESOLVED)` and the commit.
  There is deliberately no `RESOLVED -> FAILED` edge; a delivery failure
  happens while the post is still `RESOLVING`, which can legally fail.
- A Celery dispatch failure moves the post straight to `FAILED`: it is
  committed as `RESOLVING` before `send_task`, and left there, the bot's own
  "спробуйте /retry" advice would be refused by `/retry` itself.
- The `RESOLVING -> RESOLVING` self-edge exists for Celery redelivery only.
  Manual `/retry` from `RESOLVING` is refused in `bot.handlers` —
  unless `post.updated_at` is older than `TASK_TIME_LIMIT_SECONDS`, which
  means the worker died mid-task and nothing else can free the post.
  That branch restamps `updated_at` by hand: the self-edge changes no column,
  so the ORM `onupdate` never fires and the post would stay stale.
- The worker locks `post` `FOR UPDATE` for the whole task — that read-then-
  write pair is the delivery idempotency guard — but the `track` lock
  (`FOR NO KEY UPDATE`) lives in a **separate short transaction of its own**
  (`persist_track_in_own_transaction`): lock, write, commit, nothing else.
  `INSERT INTO post` takes `FOR KEY SHARE` on its parent track, so a
  `FOR UPDATE` on the track freezes the bot for the whole resolve; holding
  even the weaker lock across the network part makes a second post of the
  same track time out deterministically; and `deliver_draft` inside that
  window keeps it open for up to aiogram's 60-second timeout, which is the
  same failure again. The main transaction re-reads the track without a lock
  afterwards. Both the worker and the bot engines carry a connection-level
  `lock_timeout` (`db.session`), so every lock acquisition can raise and each
  one is wrapped.
- Every terminal branch of `resolve_post` alerts the admin **whether or not
  the database write succeeded** — Telegram does not depend on Postgres, and
  Postgres can restart while Telegram stays up. `fail_post`
  and `clean_up_non_track` both report what they could not write
  instead of raising past their own alert.
- A Telegram alert delivery failure (`pisniar.telegram.notifier.send_alert`)
  never raises into business logic and never causes a Celery task retry.
  Draft delivery (`pisniar.telegram.draft.deliver_draft`) and channel
  publication (`pisniar.telegram.publish.publish_to_channel`) are the
  opposite on purpose: both are the product, so their failures propagate to
  `failed`.
- **Channel publication is idempotent through three barriers, and only the
  first one deduplicates.** (1) `publish_post` locks the `post` row `FOR
  UPDATE` and returns early if `channel_message_id IS NOT NULL` or the status
  is no longer `PUBLISHING`; (2) the same send-before-status order as the
  draft — send, write the reference, commit, with **no network call in
  between**; (3) `uq_post_channel_message`, which is a sanity check and
  nothing more: two sends produce two *different* `message_id`s and both rows
  would insert cleanly. The window between the send and the commit is an
  accepted risk — it takes a process killed exactly between the two — and
  there is no `publish_attempted_at` column.
- The publish target is `Settings.channel_id` and nothing else, while every
  caption links to `channel.CHANNEL_LINK_URL`. Nothing derives one from the
  other, so the two are reconciled at runtime instead:
  `channel.warn_if_alerts_share_the_channel()` at startup (alerts leaking
  into a public channel) and `publish.warn_if_channel_identity_diverges()`
  against `get_chat()` (a post linking to one channel and landing in
  another). Both warn and never refuse — publishing into a test channel is
  exactly this divergence, on purpose. The admin-facing link to a published
  post is built from the chat the post actually reached, never from
  `CHANNEL_LINK_URL`.
- The `PUBLISHING -> PUBLISHING` self-edge, like `RESOLVING`'s, exists for
  Celery redelivery only. A second button tap is refused in `bot.handlers`
  before `set_post_status()` ever sees it — the transition table alone
  accepts it and would dispatch a second task.
- **There are two inline buttons and one callback grammar.** «Опублікувати в
  канал» (`publish:<id>`) and «Зробити відео» (`render:<id>`)
  both live under the draft; `telegram.keyboards.parse_callback` is the only
  thing that reads a payload, and an unknown prefix is refused rather than
  falling through to publication. Both taps follow the same order —
  **database first (lock, guard, status, commit), keyboard second, dispatch
  last** — so a tap the database declined never loses its own button, and a
  double tap has nothing left to press.
- **`RESOLVED -> PUBLISHING` stays legal while a render is running**, which
  means the text draft's publish button is live during it. Refusing that tap
  with the generic "wrong state" sentence would name `downloading` and read
  to the admin like a breakage, so `MEDIA_IN_PROGRESS_STATUSES` gets a
  sentence that says what is actually happening.
- **`/retry` on a `failed` post routes by `post.failed_from_status`**, the
  column `set_post_status()` writes — four branches, one per stage
  (`resolve`, `download`, `render`, `republish`). The stage is never inferred
  from `draft_message_id`: a post that failed while downloading has a draft
  too, and reading that as a failed publication would publish the cover
  photo of a post whose video never rendered. A `failed` row with no stage
  recorded predates the column; for it alone a draft means "the resolve
  succeeded, publication failed" — no media stage existed when it failed.
- **Every in-progress state has an exit when the worker dies mid-task**,
  and each is measured against **its own** Celery hard limit:
  `RESOLVING` against `worker.celery_app.TASK_TIME_LIMIT_SECONDS`,
  `DOWNLOADING`/`RENDERING` against the `Settings` limits of their tasks.
  `DOWNLOADED` gets one too — nothing runs in it, so a post left there means
  the worker died between the download's commit and the render dispatch.
  Each reclaim restamps `updated_at` by hand, for the same reason as the
  `RESOLVING` one, and each refusal has a
  sentence of its own, never a state name the admin has to decode.
- The bot's shared `Bot` client stays `parse_mode=None` globally — the same
  client sends alerts, and alert text regularly contains `<` (tracebacks,
  URLs). `parse_mode="HTML"` is passed explicitly, at `deliver_draft()` and
  `publish_to_channel()`, and nowhere else.

## Resolver conventions (`pisniar/resolve/`)

- Every adapter call goes through `registry.isolated_source_call()`: one
  source raising must not fail a post the other five could fill. The
  failure lands in `ResolvedTrack.source_failures`, which the worker alerts
  the admin about — isolation is only acceptable while the loss is audible.
  One entry per source: `deezer:album` is fetched twice (year/release type
  in the ISRC fan-out, genres in `service.resolve_genres`).
- Isolation covers the **source**, never the infrastructure underneath it:
  `SQLAlchemyError` is re-raised, because `SourceCache` talks to Postgres
  inside every one of these calls and a dead database would otherwise be
  read as "all six sources failed" — an empty resolve, on the branch whose
  own error handling then has nothing left to report through.
- **The asymmetry around `songlink.fetch` is deliberate.** The *primary*
  call in `service.resolve_track()` stays outside `isolated_source_call` —
  it produces the resolve's entry entity, and without it there is nothing
  for the other sources to key on, so failing loudly is the honest outcome.
  The *bridged* call (`bridge_youtube_to_spotify`, after the ISRC fan-out)
  is isolated: by then the track is already filled, and raising there would
  throw all of it away.
- A `user_input` link (the clip the admin submitted) is written into the
  result before any network call, so it never counts as proof that a source
  answered, and a resolve without `title` is never delivered as a draft —
  without either rule a completely failed resolve reaches the admin as a
  `RESOLVED` post.
- **ISRC-first**: a link found via a direct ISRC lookup (Deezer
  `/track/isrc:`, Spotify `q=isrc:`, MusicBrainz) never goes through
  `matching.is_match()` — the identifier is the proof. A link found via
  name search always does, and a rejected candidate is never written to
  `platform_links` at all (no "low confidence" tier exists).
- `track.platform_links[platform]` = `{"url", "source", "match"}`, `match`
  in `manual | exact_isrc | aggregator | confirmed`. Priority on conflict is
  that same order (`resolve.merge.platform_link_priority`), read
  straight off `match` — `aggregator` is a song.link cluster link, where we
  trust Odesli's clustering rather than an identifier we checked ourselves,
  and `manual`/`user_input` is never overwritten by anything automatic.
- Every candidate enters through `resolve.merge.merge_platform_link()`,
  which drops a non-`http(s)` URL with a warning rather than letting one bad
  link (MusicBrainz maps platform by host from a publicly editable database)
  fail the whole post at render time.
- A re-resolve **merges into** the stored row, it does not overwrite it
  (`merge_stored_platform_links` / `merge_stored_artists`): a stored
  `manual` link and a non-empty `artists[].hashtag` always win, a platform
  missing from the fresh result is kept, and a fresh result wins a tie. The
  same rule covers scalars (`merge_stored_scalars`): an absent fresh
  value never nulls a stored one. When `resolved.source_failures` is
  non-empty, a stored `title`/`artists` replaces a fresh value that is
  **weak** — from `songlink:youtube` or with no recorded source
  (`keep_stored_title_and_artists_after_source_failure`). Otherwise
  `merge_stored_scalars` would wave a raw clip name over a clean Spotify
  title; a strong fresh value (Spotify answered, iTunes timed out) still wins. A
  re-resolve that comes back completely empty fails the post instead of
  deleting a track that was resolved once before — deletion is only for a
  track that never resolved.
- **`persist_track` merges in a fixed order, and `apply_manual_fields` is last:**
  `merge_stored_platform_links`, `drop_removed_platforms`,
  `keep_stored_title_and_artists_after_source_failure`, `merge_stored_artists`,
  `merge_stored_scalars`, `apply_manual_fields`. A name in `track.manual_fields`
  makes the stored value replace the fresh one unconditionally — `None` included —
  so a manually emptied year or release type survives a re-resolve; the
  `manual_fields_kept` log names the fields where the fresh value differed. A key in
  `track.removed_platforms` is dropped from the result after every platform merge, and
  a clip the admin sends removes `youtube` from that list again.
- **Automatic links meet a host list** (`resolve/link_policy.py`), inside
  `merge_platform_link`: https only, no port or credentials, and a host that belongs
  to that platform (Bandcamp by suffix); an unknown platform key is refused. A refused
  candidate is not stored; it is logged as `platform_link_rejected_by_allowlist` and
  carried in `ResolvedTrack.link_rejections`, which the worker's alert under the
  draft lists next to `source_failures`. Manual and already-stored links skip this
  check — a manual link is validated where it is entered (`check_manual_link`: track
  link of that platform, valid `videoId` for YouTube), and the album field has its
  own parser (`parse_spotify_album_link`, stores the id).
- **A caption is built from stored rows only** (`post.caption_input`): the worker
  and the Mini App read `track`/`post` JSONB through the same
  `build_caption_from_rows`, which also returns what did not fit
  (`CaptionDegradation`). Keep it that way — a second conversion drifts.
- `track.artists` = `[{"name", "hashtag", "spotify_id"}]`; `hashtag` is
  `null` until a human overrides it (`post.hashtags.default_hashtag()`
  computes the display default at caption-build time, it is not stored).
- The `source_fetch` cache is written through `resolve.cache.SourceCache`,
  which opens **its own short session** per call — never the caller's
  business transaction. Rows go stale after `SOURCE_FETCH_TTL_SECONDS`
  (1 hour) and are refreshed with `INSERT ... ON CONFLICT DO UPDATE`,
  except that a negative payload may never overwrite a still-fresh positive
  row. A source error that is not a documented miss raises instead of
  being cached at all — see `deezer.DeezerApiError`, `SonglinkParseError`.
- `post.genres` = `[{"name", "hashtag", "source"}]`, sourced from Deezer
  album genres keyed by numeric id (`resolve.genres`) — never by the
  localized name string, which Deezer varies by request IP.
- **`platform_links["youtube"]` vs `["youtubeMusic"]` are different
  objects.** If the admin submitted a YouTube clip, `["youtube"]` holds
  that exact clip (`source: "user_input"`) and is never touched again.
  `["youtubeMusic"]` is always resolved separately via `ytmusicapi` (the
  canonical "— Topic" recording). **Audio is downloaded using the `videoId`
  from `platform_links["youtubeMusic"]`, never `["youtube"]`** — the latter
  may hold a clip with intro/outro or different mastering.
- `post.caption` already contains **rendered, escaped Telegram HTML**
  (`pisniar.post.render.render_html`). Whoever sends it (the channel publish
  included) must pass it through as-is with `parse_mode="HTML"` — escaping it
  again would double-encode `&amp;` into `&amp;amp;` etc. The post carries a
  cover, so it goes out as `send_photo(caption=...)`, which is also why
  the caption is built at 1024 and never rebuilt at publish time: the admin
  has to approve the exact text subscribers see.

## Media pipeline conventions (`pisniar/worker/`)

- **The audio source is `platform_links["youtubeMusic"]` and there is no
  fallback under any condition.** `download.youtube_music_video_id()` reads
  that key alone; a missing one is `failed` plus an alert with **no network
  call at all**, never a quiet slide over to `["youtube"]` (which may hold a
  clip with an intro, an outro or a different mastering). The id then goes
  through `matching.is_valid_youtube_video_id()` and **we** build the watch
  URL from it — the stored URL string is never handed to `yt-dlp`.
- **A long task holds no lock.** The pattern is three phases, and it is not
  negotiable: transaction 1 (milliseconds) locks the post, guards the status,
  writes `DOWNLOADING`/`RENDERING`, commits; then yt-dlp or ffmpeg runs for
  minutes with **no transaction and no lock open**; then transaction 2
  (milliseconds) locks the post again, writes the artifact row and the final
  status. A lock held across a long phase is a defect here, not a slowdown:
  `INSERT INTO post` waits on its parent track, so a held track lock freezes
  the bot and times out a second post of the same track, and a post lock
  held across a network call keeps the row locked for as long as that call
  takes. A resolve's network part is ~20 seconds; a render is an order of
  magnitude longer. Every value the long phase needs is copied out as a
  plain dataclass (`DownloadClaim`/`RenderClaim`) — no ORM object survives the commit.
- **Idempotency therefore lives on the filesystem, not on the lock.** Work is
  written to `<final name>.partial` inside `/data/tmp/<post_id>/` and published
  with `os.replace()`, so "a non-empty file exists under the final name" means
  "it is complete". `storage.existing_artifact()` is that guard; a copy-then-
  delete would break it. The database halves are `UNIQUE (track_id, video_id)`
  on `audio_asset` and `UNIQUE (post_id, params_hash)` on `render`, both
  written with `ON CONFLICT DO UPDATE` (which must set `updated_at` itself —
  a Core statement never fires the ORM `onupdate`).
- **The exit code is never the verdict.** `ffmpeg` exiting 0 on an unusable
  file is the media pipeline's main silent failure, so `ffmpeg.verify_media()` decides:
  size above zero, the required streams present, the duration where it was
  promised. A downloaded file is checked the same way against
  `track.duration_ms` with `matching.DURATION_TOLERANCE_MS` — that is what
  catches yt-dlp handing back a stub or a different recording. Rows that
  predate `duration_ms` (it is nullable) take an explicit branch: still probed,
  comparison skipped, logged as `audio_duration_unverified`.
- **`isolated_source_call` does not apply here.** It is about metadata
  sources, where losing one means a poorer post. A failed download or render
  means there is no product, so it fails loudly. The cover fetch in
  `render.download_cover()` is on the same side of that line.
- **`except Exception` would swallow `SoftTimeLimitExceeded`** — Celery raises
  it as an ordinary exception, and the task would then report "помилка" where
  the truth is "таймаут". Both media tasks catch it separately, in the async
  body *and* around `run_async()` in the sync wrapper, because Celery raises it
  in the main thread, i.e. out of the event loop rather than out of an `await`.
- **The media tasks carry their own time limits** from `Settings`
  (`download_task_*`, `render_task_*`). `TASK_SOFT_TIME_LIMIT_SECONDS` /
  `TASK_TIME_LIMIT_SECONDS` in `worker/celery_app.py` are the resolve's, and
  `bot/handlers.py` reads them as the "this resolve has gone stale" window —
  raising them for a render would silently widen it.
- **Paths are built only from numeric ids, a validated video id and a hex
  hash** (`worker/storage.py`): `/data/audio/<track_id>/<video_id>.<ext>`
  (per track — two posts of one recording download it once),
  `/data/render/<post_id>/<params_hash>.mp4` (per post — the caption and the
  fragment bounds belong to the publication), `/data/tmp/<post_id>/` (removed
  in a `finally`, always). Never from a track title: that is path traversal
  and Cyrillic filenames in one. `shutil.disk_usage()` is checked **before** a
  download starts, because running out mid-render leaves a file that looks
  finished.
- **The fragment rule has no heuristics**: start at 0, length 45 s,
  a shorter track taken whole. `post.fragment_start_ms`/`fragment_end_ms` are
  absolute from the start of the **full** track. Bounds already on the post win
  as long as they still fit the downloaded file.
- **`scale=...:in_range=pc:out_range=tv` plus `-color_range tv` is required,
  not a refinement.** The cover arrives from the Spotify CDN as
  full-range JPEG, and a naive `format=yuv420p` produces `yuvj420p` *every
  time*; `subtitles=` takes `yuv420p` directly and changes neither. The render
  is 720x1280, H.264 + AAC, `+faststart`, explicit fps, `-maxrate`/`-bufsize`,
  and its size is asserted against the 50 MB Telegram ceiling with exactly one
  lower-bitrate retry before the post fails. The worst realistic input renders
  to ~4.4 MB, so that branch guards a render defect, not normal input.
- `render.RenderParameters.subtitles_path` is an **optional path passed in from
  outside**, not a flag decided inside: `None` means the filtergraph has no
  `subtitles=` step at all, never an empty file.
- **The frame is the cover on top and the lyrics below it.** The cover is
  scaled into 720x1280 and padded at `render.COVER_TOP_OFFSET_PX` (100 px), so a
  square cover fills 100…820 and `ass.TEXT_BAND_TOP_PX` (820) starts the text
  band; a long line overflows into black, never onto the image. The band holds
  three slots — previous, current, next — at y 955/1050/1145
  (`SLOT_SPACING_PX` 95), centred by `\an5` plus an explicit `\pos`. The
  current line is 44 px white and wraps to two lines, the neighbours 32 px
  half-transparent and one line; the rest is cut with `…`. `test_ass.py` pins
  the cover offset and the band against each other, and any change to the
  filtergraph or the encoder bumps `render.RENDERER_VERSION`, an input to
  `params_hash`.
- **The chain is started by a button and continues by itself.** «Зробити
  відео» dispatches `download_post_audio`; that task dispatches
  `render_post_video` **after** its own commit, so a redelivered render can
  never outrun the `DOWNLOADED` it needs. A failed internal dispatch fails
  the post, exactly like a failed resolve dispatch, rather than leaving it in
  a state no task will move it out of.
- **The preview is the only safeguard before publication.** Publication cannot be
  revoked, so `RENDERED` means «прев'ю вже в чаті адміна»: the render
  task sends the video, writes `preview_chat_id`/`preview_message_id`, sets
  the status and commits, in that order — the draft's `RESOLVED` rule,
  mirrored. A delivery failure
  therefore happens while the post is still `RENDERING`, which is the state
  that can legally fail — `RENDERED` deliberately has no edge to `FAILED`.
  The text draft is a separate message (`editMessageMedia` cannot turn text
  into media), so it loses its keyboard when the preview arrives: its publish
  button would send the cover photo instead of the video.
- **That transaction holds the post lock across one Telegram upload**, and
  that is deliberate — it is the same delivery guard `publish_post`'s barrier
  1 is, and the lock is what makes the `preview_message_id IS NULL` check
  atomic against redelivery. The "milliseconds" of the three-phase rule above
  are about yt-dlp and ffmpeg, which run with no transaction open at all.
- **`telegram.video.send_rendered_video` is the one `send_video` call** the
  preview and the publication share, which is what makes «the admin sees the
  exact file that reaches the channel» structural instead of a promise. The
  caption is passed verbatim: the video caption limit is the same 1024 the
  photo caption already fits, so nothing is rebuilt.
- **The publication branch is chosen by the status, never by a file.** A
  previous failed attempt can leave an `.mp4` behind, so `send_video` is
  reached only through `post_reached_rendered()` — `preview_message_id`, the
  durable trace of `RENDERED`, written by the transaction that set it — plus
  a `render` row whose file is still on the volume. If the post was previewed
  as a video and the video is gone, publication **fails loudly**; falling
  back to `send_photo` would put a different post in the channel than the one
  the admin approved.
- **A post with a preview is never downloaded or rendered again**, so the
  render publication takes is always the one the admin saw. `video_for_publication`
  picks the post's newest `render` row, and that is safe only because no row
  can appear after `preview_message_id` is written: both media tasks exit
  without work on a previewed post, and «Зробити відео» refuses it with a
  sentence of its own before any status changes.
- **A media task never acts on a `failed` post**: it can only get there by
  redelivery. Every bot path (the button, each `/retry` branch, a stale
  reclaim) writes the target status before it dispatches, and the download
  commits `DOWNLOADED` before it chains the render. So `FAILED` is absent from
  `DOWNLOAD_ENTRY_STATUSES`/`RENDER_ENTRY_STATUSES`, though the transition
  table allows it.
- **Cleanup has three levels.** `/data/tmp/<post_id>/` goes in a
  `finally`, always. Artifacts of deleted rows go **after** the commit — the
  reverse order would leave a surviving row pointing at a deleted file, and
  an orphan file is the safer half. Retention: the video is removed
  after a **successful** publication (Telegram holds the copy now), audio
  is never removed, and there is no sweeper. The `render` row stays as the
  record of what was published; only its file is gone.
- **The thumbnail is derived, not stored.** `<params_hash>.jpg` sits beside
  its `.mp4`, cut from the rendered file's own first frame, so it needs no
  column and the same directory removal takes both. It is cosmetic:
  `render_thumbnail()` returns `None` instead of raising, and a missing file
  is not a send failure — Telegram builds its own preview.

## Lyrics and subtitles (`worker/lyrics.py`, `worker/ass.py`, `resolve/sources/lrclib.py`)

- **The text path runs in the render's long phase, with no session open.**
  Stored renderable lines win and LRCLIB is never asked; only when there are
  none does `worker.lyrics.prepare_lyrics` fetch, persist in a short
  transaction of its own and read the lines back as plain values. Then
  `ass.build_ass_subtitles` shifts them into fragment time, and
  `render.text_version_of` hashes the resulting `.ass` text. `text_version`
  (`None` without text) is an input to `params_hash`, so the lines, their
  timing, the fragment offset, the wrapping and the styles all change the
  hash with nothing to bump by hand; the same text finds its own file on the
  volume and never runs ffmpeg again. The `.ass` lives in `/data/tmp/<post_id>/`
  and is regenerated on every render.
- **"No text" and "the post fails" are split by whose failure it is.** A video
  without text: every LRCLIB class but synced (absent, unsynced, instrumental,
  rejected by matching), `LrclibApiError`, `LrcParseError`, a network error,
  and synced lines that miss the fragment (`lyrics_outside_fragment`). A failed
  post: persisting, reading the lines back, generating the `.ass`
  (`AssGenerationError`), plus `SoftTimeLimitExceeded` and `SQLAlchemyError`,
  which are re-raised before the one broad `except` around `fetch_lyrics` —
  the cache under it talks to Postgres, and a dead database is not "no text".
  LRCLIB is deliberately not behind `isolated_source_call`: that `except` is
  its isolation, and `RenderOutcome.lyrics_reason` carries the class to where
  the preview is sent.
- **A video without text is explained in three places**, since each can fail
  alone: the structured log, `render.text_version IS NULL`, and a notice
  answering the preview in its chat — never in the caption, which is verbatim.
  `lyrics_outside_fragment` is decided where the `.ass` is built (lines exist,
  the fragment holds none). The notice is sent through `send_alert` after the
  `RENDERED` commit, only while `text_version` is `None`, one sentence per
  class; a lost notice is a log line, never a failed post, and a redelivery
  stops at the claim before it.
- **LRCLIB is classified by the body, never by the status.** Synced,
  unsynced, instrumental and the documented miss are the only answers; the
  miss is a 404 whose JSON body has `"name": "TrackNotFound"` **and**
  `"statusCode": 404`. Everything else raises `LrclibApiError` and is **never
  cached**: 429, 500/502/503/504 or a transport error after three attempts,
  any other non-200 status, a non-JSON body, a 404 with any other body, and the
  empty positive (a 200 with both lyric
  fields empty and `instrumental` not `true`). A cached "no lyrics" for an
  outage would hide the text for a whole TTL. A cached row is re-verified on
  the read path, not trusted.
- **LRCLIB compares the artist string exactly** (case aside), and stores one
  joined string, so the artists are joined with `", "` — either half of a duo
  alone answers 404. A documented 404 only proves our exact string is unknown,
  so `/api/search` by title follows before the miss is final, and every
  candidate goes through `matching.is_match()` against the track's artists,
  title and `duration_ms`. The duration is never sent to LRCLIB.
- **Song text reaches libass as untrusted input.** libass executes override
  blocks inside the text (`{\an7\fs90}` moves and resizes a line), and ASS
  has no escape inside a line, so `ass.escape_ass_text` replaces `{`/`}` with
  `(`/`)` and drops `\` altogether — no `\N`, `\h` or `\an7` from a lyric. The
  only `\N` in the file is the wrapper's own.
- **A font name missing from the image falls back to Noto Sans silently.**
  `fc-match` answers an unknown family with the same file as an installed
  one, so a wrong `ass.FONT_FAMILY` shows up only as rectangles in a video.
  The one guard is `tests/integration/test_ass_font.py` (`make test-fonts`),
  which checks the family against `fc-list :lang=uk` inside the worker.
- **libass reads `Fontsize` as the ascender-to-descender cell height**, not
  the em square, so every width is scaled by `text_metrics.FONT_SIZE_TO_EM_SCALE`
  (derived from the font's `hhea` metrics, never a literal). Line widths come
  from `worker/noto_sans_widths.py`, a table of real advance widths baked by
  `scripts/generate_glyph_widths.py` from the worker image's font — generated,
  never edited by hand, and pinned to that font by the same `make test-fonts`
  test, so a font package update fails loudly instead of wrapping with stale
  numbers. An unknown codepoint counts as a full em and kerning is ignored:
  both err wide, so a line wraps early rather than leaving the frame.
- **The render limits cover LRCLIB's worst case.** Timeouts, retries and the
  fallback search add up to ~187 s, so `render_task_*` is 360/420 s; the
  hard limit is also the stale-render window `/retry` waits out.

## Data conventions

- Every duration/offset is `BIGINT` milliseconds — never a float number of
  seconds.
- Every timestamp column is `TIMESTAMPTZ` in UTC.
- Lyric timecodes are absolute from the start of the full track, not the
  fragment. At render time: `ass_t = lyric.start_ms - post.fragment_start_ms`.
- `updated_at` is maintained by an ORM-level `onupdate`, not a database
  trigger. A migration that updates rows in place (`op.execute("UPDATE ...")`,
  a raw `text("UPDATE ...")`) bypasses the ORM and must set
  `updated_at = now()` itself in that same statement — otherwise the row's
  `updated_at` goes stale with no way to fix it after the fact.
- A migration runs with `bot` and `worker` stopped: stop them, run the one-shot
  `migrate` service, start the new image. There is no rolling window in which
  old code meets the new schema.
- Autogenerate never emits a `CHECK` constraint on an existing table, so such
  a constraint is written into the revision by hand.
- A `NOT NULL` column added to a table that already holds rows takes three
  steps in one revision, written out per table: add it nullable, backfill it with
  an `UPDATE` that also sets `updated_at = now()`, then `ALTER COLUMN ... SET NOT NULL`;
  a `server_default` is not used to get past the constraint.
- `track.duration_ms` is what the downloaded audio is
  checked against, so it has to outlive the resolve that found it: it is in
  `merge.STORED_SCALAR_FIELDS`, and without that entry the first `/retry` with
  a failed source would null it: `merge_stored_scalars` keeps a stored value
  only for the fields listed there.
- `post.failed_from_status` records **which stage failed**,
  written by `set_post_status()` next to `last_error` and cleared on the way
  out of `FAILED`. The stage cannot be inferred from `draft_message_id`,
  because a post that failed while downloading also has a draft.
- `post.preview_chat_id`/`preview_message_id` are the video preview's own
  reference, shaped exactly like the draft pair — a `CHECK` against a
  one-sided pair plus a partial unique index.
- A schema change always goes into a new revision, never into an applied
  one: `upgrade head` skips a revision that changed after it ran, and
  `alembic check` then reports drift.
- `audio_asset` is keyed to a **track** (`ON DELETE CASCADE`, unlike
  `post.track_id`'s `RESTRICT`: an artifact is not a reason to refuse deleting
  its track, and a file without a row is the safe side). `duration_ms` there is
  measured by `ffprobe` on the file itself, never copied from the resolve —
  that is what makes it evidence. `ytdlp_version` is stored so a broken
  extractor — every download failing at once — stays tellable from a missing
  track; a reused artifact keeps
  the version that actually produced it.
- `render` is keyed to a **post**. `params_hash` covers every input the frames
  depend on, `text_version` included, so a render never reuses a file with
  other text or none. Both tables refuse a zero size or a zero duration at the
  CHECK level — that is the shape a silent ffmpeg or yt-dlp failure takes.
- `lyric` is one line per row, keyed to a **track**
  (`ON DELETE CASCADE`, like `audio_asset`), so posts of one recording share
  the text. It holds **only synced lines** (`start_ms` is `NOT NULL`); plain
  text without timecodes is never stored and is visible only as the reason
  class `lyrics_unsynced` in the log and the admin's notice. `line_index` is
  the only order, `start_ms` orders nothing, and there is no `end_ms` — the
  render derives it. The render path reads the table only through
  `models.renderable_lyrics_statement`, which spells `start_ms IS NOT NULL`
  out, so the render code does not rely on the column's `NOT NULL`. `text` is raw
  as the source gave it (the ASS substitution happens at render time) and
  bounded at 500 characters by a CHECK; a longer line is cut, never dropped,
  because a `line_index` gap would read like a line deleted by hand. `source`
  is `'lrclib'` or `'manual'`.
- **Lines are written only onto a track that has none, of any `source`.** The
  writer locks the track `FOR NO KEY UPDATE` (the lock the resolve takes, which
  `INSERT INTO post` still passes), counts its lines and inserts only when the
  count is zero, in one short transaction. The lock is what makes that guard
  atomic: under READ COMMITTED a second writer counts only after the first
  commits, while `UNIQUE (track_id, line_index)` stops a duplicate index, not a
  mix of two writers' lines. **Every writer of `lyric` must take the same
  lock.** An insert that lands short raises and
  rolls back rather than committing a partial set.
- `track.manual_fields` lists the columns a human set, drawn from
  `models.MANUAL_FIELD_NAMES` (`title`, `artists`, `year`, `release_type`,
  `release_title`, `album_id_spotify`); a CHECK refuses any other name. A manually
  emptied field is its name in the list plus `NULL` in its own column — there is no
  separate marker. `track.removed_platforms` lists the `platform_links` keys a human
  removed, so a re-resolve does not bring them back. Both are `JSONB NOT NULL
  DEFAULT '[]'` with an array CHECK.
- `post.channel_message_kind` (`photo`, `video` or `text`) says what
  `channel_message_id` points at, which decides how that message can be edited.
  `publish_to_channel` writes it in the same assignment as the message id, and a
  CHECK keeps the two NULL together.

## The flow end to end

An admin sends a link; the bot classifies it, the worker resolves it across
six sources ISRC-first, builds the caption at the 1024 media limit and
delivers a draft with `/retry` on failure. «Опублікувати в канал» under the
draft sends the cover with that caption as `send_photo` and records
`published_by_user_id`.

«Зробити відео» starts the media chain instead: the worker downloads the
audio from `youtubeMusic`, picks the fragment, takes the track's stored
lyric lines or fetches them from LRCLIB, renders a verified 720x1280 file
with previous, current and next line under the cover, and delivers it as a
**preview with a publish button**. A video without text still reaches the
preview, with a notice under it saying why. Publication sends that same file
with that same caption as `send_video`, and the file is deleted afterwards.

Revoking a published post is deliberately not implemented — a wrong post is
deleted by hand in Telegram, and the row goes on saying `published`.
