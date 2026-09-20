# AGENTS.md

## What this is

`pisniar` — a Telegram bot that automates posting music tracks to a channel:
resolve a link's metadata, prepare a post, render a vertical video fragment
(step 4A: cover plus audio, no text; step 4B adds the karaoke text), publish.
Steps 2-4A are implemented; step 4A is awaiting manual acceptance.

## Stack and runtime

- Python 3.13 (`>=3.13,<3.14`), dependency/venv management via `uv`.
- src-layout, import package `pisniar` (`src/pisniar/`).
- aiogram 3 (long polling) + FastAPI (health checks), Celery + Redis
  (worker), SQLAlchemy 2.0 async + asyncpg, Alembic.
- `yt-dlp` (pinned in `uv.lock`, **never self-updating** — `--no-update` is
  passed explicitly) plus `ffmpeg`/`ffprobe`, both run as subprocesses and both
  present **only in the worker image**. The `bot` image has neither, and the
  `media-data` volume (`/data`) is mounted only into `worker` (D-1).
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
  the health/readiness API extractable into its own service later without a
  rewrite.
- `pisniar/worker/` never imports `pisniar/bot/`, and `pisniar/bot/` never
  imports `pisniar/worker/tasks` directly. The bot dispatches Celery tasks by
  name through `pisniar.worker.celery_app`, not by importing task functions.
- `pisniar/bot/` imports only `pisniar.resolve.urls` (syntactic
  classification, no network) and `pisniar.telegram.keyboards` (the
  `callback_data` grammar, `aiogram` only) — the full resolve pipeline
  (`resolve.service`/`resolve.registry`, the six source adapters, `post/`
  caption building) runs only in `pisniar/worker/`.
- `pisniar/resolve/` (six source adapters + ISRC-first matching/merge) and
  `pisniar/post/` (caption/hashtags/sanitize/render) never import `bot/` or
  `worker/` — they are pure libraries the worker orchestrates.
- The one edge back the other way is `post.sanitize`, which `resolve/` may
  import (`musicbrainz` logs a source-supplied URL through it) — and it is
  the **only** `post/` module allowed there. That works because
  `post.sanitize` is a leaf: it imports `pisniar.resolve.urls` and the
  stdlib, nothing else. Keep it one, and keep it a leaf — a second import
  or a heavier dependency turns `resolve/` <-> `post/` into a cycle.
- A post's status changes **only** through `set_post_status()`
  (`pisniar.db.states`). Never assign `post.status = ...` directly — it skips
  transition validation and the `last_error` bookkeeping that function owns.
- `RESOLVED` means **the draft is already in the admin's chat** (D-38), so
  `deliver_draft()` runs *before* `set_post_status(RESOLVED)` and the commit.
  There is deliberately no `RESOLVED -> FAILED` edge; a delivery failure
  happens while the post is still `RESOLVING`, which can legally fail.
- A Celery dispatch failure moves the post straight to `FAILED` (D-53):
  it was committed as `RESOLVING` before `send_task`, and the bot's own
  "спробуйте /retry" advice was then refused by `/retry` itself.
- The `RESOLVING -> RESOLVING` self-edge exists for Celery redelivery only.
  Manual `/retry` from `RESOLVING` is refused in `bot.handlers` (D-43) —
  unless `post.updated_at` is older than `TASK_TIME_LIMIT_SECONDS`, which
  means the worker died mid-task and nothing else can free the post (R2-2).
  That branch restamps `updated_at` by hand: the self-edge changes no column,
  so the ORM `onupdate` never fires and the post would stay stale (R3-4).
- The worker locks `post` `FOR UPDATE` for the whole task — that read-then-
  write pair is the delivery idempotency guard — but the `track` lock
  (`FOR NO KEY UPDATE`) lives in a **separate short transaction of its own**
  (`_persist_track_in_own_transaction`): lock, write, commit, nothing else.
  `INSERT INTO post` takes `FOR KEY SHARE` on its parent track, so the
  stronger lock froze the bot for the whole resolve (R2-3); holding the
  weaker one across the network part made the second post of the same track
  time out deterministically (D-54); and leaving `deliver_draft` inside that
  window kept it open for up to aiogram's 60-second timeout, which is the
  same failure again. The main transaction re-reads the track without a lock
  afterwards. Both the worker and the bot engines carry a connection-level
  `lock_timeout` (`db.session`), so every lock acquisition can raise and each
  one is wrapped.
- Every terminal branch of `resolve_post` alerts the admin **whether or not
  the database write succeeded** — Telegram does not depend on Postgres, and
  a restart of it is a documented manual-acceptance step here. `_fail_post`
  (R3-1) and `_clean_up_non_track` both report what they could not write
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
  is no longer `PUBLISHING`; (2) the D-38 order — send, write the reference,
  commit, with **no network call in between**; (3) `uq_post_channel_message`,
  which is a sanity check and nothing more: two sends produce two *different*
  `message_id`s and both rows would insert cleanly. The window between the
  send and the commit is an accepted risk (D-47) — there is no
  `publish_attempted_at` column.
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
  канал» (`publish:<id>`, D-46) and «Зробити відео» (`render:<id>`, D-77)
  both live under the draft; `telegram.keyboards.parse_callback` is the only
  thing that reads a payload, and an unknown prefix is refused rather than
  falling through to publication. Both taps follow the D-68 order —
  **database first (lock, guard, status, commit), keyboard second, dispatch
  last** — so a tap the database declined never loses its own button, and a
  double tap has nothing left to press.
- **`RESOLVED -> PUBLISHING` stays legal while a render is running**, which
  means the old draft's publish button is live during it. Refusing that tap
  with the generic "wrong state" sentence would name `downloading` and read
  to the admin like a breakage, so `MEDIA_IN_PROGRESS_STATUSES` gets a
  sentence that says what is actually happening (S4-R14).
- **`/retry` on a `failed` post routes by `post.failed_from_status`**, the
  column `set_post_status()` writes — four branches, one per stage
  (`resolve`, `download`, `render`, `republish`). The old D-47 rule inferred
  the stage from `draft_message_id`, and step 4 broke it: a post that failed
  while downloading has a draft too, so `/retry` would have published the
  cover photo of a post whose video never rendered (S4-R7). Rows that failed
  before migration `0004` carry no stage and still follow the old rule —
  correctly, because step 4 did not exist when they failed.
- **Every in-progress state has an exit when the worker dies mid-task**
  (R2-2), and each is measured against **its own** Celery hard limit:
  `RESOLVING` against `worker.celery_app.TASK_TIME_LIMIT_SECONDS`,
  `DOWNLOADING`/`RENDERING` against the `Settings` limits of their tasks.
  `DOWNLOADED` gets one too — nothing runs in it, so a post left there means
  the worker died between the download's commit and the render dispatch.
  Each reclaim restamps `updated_at` by hand (R3-4) and each refusal has a
  sentence of its own, never a state name the admin has to decode.
- The bot's shared `Bot` client stays `parse_mode=None` globally — the same
  client sends alerts, and alert text regularly contains `<` (tracebacks,
  URLs). `parse_mode="HTML"` is passed explicitly, at `deliver_draft()` and
  `publish_to_channel()`, and nowhere else.

## Resolver conventions (`pisniar/resolve/`)

- Every adapter call goes through `registry.isolated_source_call()`: one
  source raising must not fail a post the other five could fill (D-55). The
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
  The *bridged* call (`_bridge_youtube_to_spotify`, after the ISRC fan-out)
  is isolated: by then the track is already filled, and raising threw all
  of it away.
- A `user_input` link (the clip the admin submitted) is written into the
  result before any network call, so it never counts as proof that a source
  answered, and a resolve without `title` is never delivered as a draft —
  both used to turn a completely failed resolve into a `RESOLVED` post (R3-2).
- **ISRC-first**: a link found via a direct ISRC lookup (Deezer
  `/track/isrc:`, Spotify `q=isrc:`, MusicBrainz) never goes through
  `matching.is_match()` — the identifier is the proof. A link found via
  name search always does, and a rejected candidate is never written to
  `platform_links` at all (no "low confidence" tier exists).
- `track.platform_links[platform]` = `{"url", "source", "match"}`, `match`
  in `manual | exact_isrc | aggregator | confirmed`. Priority on conflict is
  that same order (`resolve.merge.platform_link_priority`, D-39), read
  straight off `match` — `aggregator` is a song.link cluster link, where we
  trust Odesli's clustering rather than an identifier we checked ourselves,
  and `manual`/`user_input` is never overwritten by anything automatic.
- Every candidate enters through `resolve.merge.merge_platform_link()`,
  which drops a non-`http(s)` URL with a warning rather than letting one bad
  link (MusicBrainz maps platform by host from a publicly editable database)
  fail the whole post at render time.
- A re-resolve **merges into** the stored row, it does not overwrite it
  (`merge_stored_platform_links` / `merge_stored_artists`, D-42): a stored
  `manual` link and a non-empty `artists[].hashtag` always win, a platform
  missing from the fresh result is kept, and a fresh result wins a tie. The
  same rule covers scalars (`merge_stored_scalars`, R2-4): an absent fresh
  value never nulls a stored one. When `resolved.source_failures` is
  non-empty the stored `title`/`artists` win outright
  (`keep_stored_title_and_artists_after_source_failure`, D-58) — the fresh
  values are present, just lower-priority, so `merge_stored_scalars` would
  wave a raw `songlink:youtube` clip name over a clean Spotify title. A re-resolve that comes back completely
  empty fails the post instead of deleting a track that was resolved once
  before (R2-1) — deletion is only for a track that never resolved.
- `track.artists` = `[{"name", "hashtag", "spotify_id"}]`; `hashtag` is
  `null` until a human overrides it (`post.hashtags.default_hashtag()`
  computes the display default at caption-build time, it is not stored).
- The `source_fetch` cache is written through `resolve.cache.SourceCache`,
  which opens **its own short session** per call — never the caller's
  business transaction. Rows go stale after `SOURCE_FETCH_TTL_SECONDS`
  (1 hour, D-40) and are refreshed with `INSERT ... ON CONFLICT DO UPDATE`,
  except that a negative payload may never overwrite a still-fresh positive
  row (R2-5). A source error that is not a documented miss raises instead of
  being cached at all — see `deezer.DeezerApiError`, `SonglinkParseError`.
- `post.genres` = `[{"name", "hashtag", "source"}]`, sourced from Deezer
  album genres keyed by numeric id (`resolve.genres`) — never by the
  localized name string, which Deezer varies by request IP.
- **`platform_links["youtube"]` vs `["youtubeMusic"]` are different
  objects.** If the admin submitted a YouTube clip, `["youtube"]` holds
  that exact clip (`source: "user_input"`) and is never touched again.
  `["youtubeMusic"]` is always resolved separately via `ytmusicapi` (the
  canonical "— Topic" recording). **Step 4 must download audio using the
  `videoId` from `platform_links["youtubeMusic"]`, never `["youtube"]`** —
  the latter may hold a clip with intro/outro or different mastering.
- `post.caption` already contains **rendered, escaped Telegram HTML**
  (`pisniar.post.render.render_html`). Whoever sends it (the channel publish
  included) must pass it through as-is with `parse_mode="HTML"` — escaping it
  again would double-encode `&amp;` into `&amp;amp;` etc. The post carries a
  cover (D-45), so it goes out as `send_photo(caption=...)`, which is also why
  the caption is built at 1024 and never rebuilt at publish time: the admin
  has to approve the exact text subscribers see.

## Media pipeline conventions (`pisniar/worker/`, step 4A)

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
  status. Holding the lock for ~20 seconds during a resolve already produced
  three separate defects (R2-3, D-54, R3-3); a render is an order of magnitude
  longer. Every value the long phase needs is copied out as a plain dataclass
  (`DownloadClaim`/`RenderClaim`) — no ORM object survives the commit.
- **Idempotency therefore lives on the filesystem, not on the lock.** Work is
  written to `<final name>.partial` inside `/data/tmp/<post_id>/` and published
  with `os.replace()`, so "a non-empty file exists under the final name" means
  "it is complete". `storage.existing_artifact()` is that guard; a copy-then-
  delete would break it. The database halves are `UNIQUE (track_id, video_id)`
  on `audio_asset` and `UNIQUE (post_id, params_hash)` on `render`, both
  written with `ON CONFLICT DO UPDATE` (which must set `updated_at` itself —
  a Core statement never fires the ORM `onupdate`).
- **The exit code is never the verdict.** `ffmpeg` exiting 0 on an unusable
  file is this step's main silent failure, so `ffmpeg.verify_media()` decides:
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
- **The fragment rule is D-76 and has no heuristics**: start at 0, length 45 s,
  a shorter track taken whole. `post.fragment_start_ms`/`fragment_end_ms` are
  absolute from the start of the **full** track. Bounds already on the post win
  (step 6 will write them by hand) as long as they still fit the downloaded file.
- **`scale=...:in_range=pc:out_range=tv` plus `-color_range tv` is required,
  not a refinement (D-79).** The cover arrives from the Spotify CDN as
  full-range JPEG, and a naive `format=yuv420p` produces `yuvj420p` *every
  time* — measured on gate 4.0. The render is 720x1280, H.264 + AAC,
  `+faststart`, explicit fps, `-maxrate`/`-bufsize`, and its size is asserted
  against the 50 MB Telegram ceiling with exactly one lower-bitrate retry
  before the post fails. The measured worst realistic input is 4.44 MB, so
  that branch guards a render defect, not normal input.
- `render.RenderParameters.subtitles_path` is an **optional path passed in from
  outside**, not a flag decided inside, and `params_hash` already covers
  `text_version` (always `None` in 4A). Both are the seams that keep step 4B
  an addition to a verified filtergraph instead of a rewrite.
- **The chain is started by a button and continues by itself.** «Зробити
  відео» dispatches `download_post_audio`; that task dispatches
  `render_post_video` **after** its own commit, so a redelivered render can
  never outrun the `DOWNLOADED` it needs. A failed internal dispatch fails
  the post (D-53's case one level down) rather than leaving it in a state no
  task will move it out of.
- **The preview is the only safeguard the step has.** Publication cannot be
  revoked (D-65), so `RENDERED` means «прев'ю вже в чаті адміна»: the render
  task sends the video, writes `preview_chat_id`/`preview_message_id`, sets
  the status and commits, in that order (D-38 mirrored). A delivery failure
  therefore happens while the post is still `RENDERING`, which is the state
  that can legally fail — `RENDERED` deliberately has no edge to `FAILED`.
  The text draft is a separate message (`editMessageMedia` cannot turn text
  into media), so it loses its keyboard when the preview arrives: its publish
  button would send the cover photo instead of the video.
- **That transaction holds the post lock across one Telegram upload**, and
  that is deliberate — it is the same delivery guard `publish_post`'s barrier
  1 is, and the lock is what makes the `preview_message_id IS NULL` check
  atomic against redelivery. §2's "milliseconds" is about yt-dlp and ffmpeg,
  which run with no transaction open at all.
- **`telegram.video.send_rendered_video` is the one `send_video` call** the
  preview and the publication share, which is what makes «the admin sees the
  exact file that reaches the channel» structural instead of a promise. The
  caption is passed verbatim: the video caption limit is the same 1024 the
  photo caption already fits (D-45), so nothing is rebuilt.
- **The publication branch is chosen by the status, never by a file.** A
  previous failed attempt can leave an `.mp4` behind, so `send_video` is
  reached only through `post_reached_rendered()` — `preview_message_id`, the
  durable trace of `RENDERED`, written by the transaction that set it — plus
  a `render` row whose file is still on the volume. If the post was previewed
  as a video and the video is gone, publication **fails loudly**; falling
  back to `send_photo` would put a different post in the channel than the one
  the admin approved.
- **Cleanup has three levels (§5).** `/data/tmp/<post_id>/` goes in a
  `finally`, always. Artifacts of deleted rows go **after** the commit — the
  reverse order would leave a surviving row pointing at a deleted file, and
  an orphan file is the safer half. Retention is Q8: the video is removed
  after a **successful** publication (Telegram holds the copy now), audio
  lives until step 6, and there is no sweeper. The `render` row stays as the
  record of what was published; only its file is gone.
- **The thumbnail is derived, not stored.** `<params_hash>.jpg` sits beside
  its `.mp4`, cut from the rendered file's own first frame, so it needs no
  column and the same directory removal takes both. It is cosmetic:
  `render_thumbnail()` returns `None` instead of raising, and a missing file
  is not a send failure — Telegram builds its own preview.

## Data conventions

- Every duration/offset is `BIGINT` milliseconds — never a float number of
  seconds.
- Every timestamp column is `TIMESTAMPTZ` in UTC.
- Lyric timecodes are absolute from the start of the full track, not the
  fragment. At render time: `ass_t = lyric.start_ms - post.fragment_start_ms`.
- `updated_at` is maintained by an ORM-level `onupdate`, not a database
  trigger. Any future migration that updates rows in place (`op.execute("UPDATE ...")`,
  a raw `text("UPDATE ...")`) bypasses the ORM and must set
  `updated_at = now()` itself in that same statement — otherwise the row's
  `updated_at` goes stale with no way to fix it after the fact.
- `track.duration_ms` (migration `0004`) is what the downloaded audio is
  checked against, so it has to outlive the resolve that found it: it is in
  `merge.STORED_SCALAR_FIELDS`, and without that entry the first `/retry` with
  a failed source would null it (the R2-4 mechanic).
- `post.failed_from_status` (migration `0004`) records **which stage failed**,
  written by `set_post_status()` next to `last_error` and cleared on the way
  out of `FAILED`. It replaces the D-47 rule that inferred the stage from
  `draft_message_id` — a rule that step 4 breaks, because a post that failed
  while downloading also has a draft.
- `post.preview_chat_id`/`preview_message_id` (migration `0005`, D-81) are
  the video preview's own reference, shaped exactly like the draft pair —
  a `CHECK` against a one-sided pair plus a partial unique index. They went
  into a separate revision because `0004` was already applied to a database
  holding 23 posts, and columns appended to an applied revision never reach
  it while `alembic check` starts reporting drift.
- `audio_asset` is keyed to a **track** (`ON DELETE CASCADE`, unlike
  `post.track_id`'s `RESTRICT`: an artifact is not a reason to refuse deleting
  its track, and a file without a row is the safe side). `duration_ms` there is
  measured by `ffprobe` on the file itself, never copied from the resolve —
  that is what makes it evidence. `ytdlp_version` is stored so a broken
  extractor stays tellable from a missing track (D-80); a reused artifact keeps
  the version that actually produced it.
- `render` is keyed to a **post**. `params_hash` covers every input the frames
  depend on, `text_version` included, so a 4B render never reuses a 4A file.
  Both tables refuse a zero size or a zero duration at the CHECK level — that
  is the shape a silent ffmpeg or yt-dlp failure takes.

## Where things stand

Steps 1-3 are implemented: the infrastructure skeleton; link -> post draft
(URL classification, ISRC-first resolution across six sources, caption
building at the 1024 media limit, idempotent draft delivery, `/retry`); and
channel publication (an inline button under the draft, a channel preflight,
an idempotent `send_photo` with the cover, `published_by_user_id`).

**Step 4A (audio download, fragment, static vertical video — no text) is
complete end to end and awaiting manual acceptance (M45-M56).** «Зробити
відео» under a draft starts the chain; the worker downloads the audio from
`youtubeMusic`, picks the fragment (D-76), renders a verified 720x1280 file
and delivers it to the admin as a **preview with a publish button**;
publication then sends that same file with that same caption as
`send_video`. `/retry` routes by the stage that failed, every in-progress
state has an exit, and the video is deleted after a successful publication.

Step 4B adds LRCLIB plus an ASS subtitle track over the already-verified
filtergraph. Step 5 adds the Mini App, which edits a published caption
through the stored `channel_chat_id`/`channel_message_id` (D-66). Revoking a
published post is deliberately not implemented (D-65) — a wrong post is
deleted by hand in Telegram, and the row goes on saying `published`.
