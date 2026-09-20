# AGENTS.md

## What this is

`pisniar` — a Telegram bot that automates posting music tracks to a channel:
resolve a link's metadata, prepare a post, render a karaoke-style video
fragment, publish. Step 2 (link -> post draft) is implemented: URL
classification, six external source adapters, ISRC-first resolution,
caption building, delivery. Rendering/publishing (steps 3-4) are next.

## Stack and runtime

- Python 3.13 (`>=3.13,<3.14`), dependency/venv management via `uv`.
- src-layout, import package `pisniar` (`src/pisniar/`).
- aiogram 3 (long polling) + FastAPI (health checks), Celery + Redis
  (worker), SQLAlchemy 2.0 async + asyncpg, Alembic.
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
- `/retry` on a `failed` post routes by `draft_message_id`: set means the
  draft was already delivered, so the failure came later — on publication,
  and `/retry` re-publishes (`FAILED -> PUBLISHING`). Null means the resolve
  failed before delivery, and `/retry` re-resolves. That distinction is what
  replaced the `publish_attempted_at` column D-47 declined to add.
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

## Where things stand

Steps 1-3 are implemented: the infrastructure skeleton; link -> post draft
(URL classification, ISRC-first resolution across six sources, caption
building at the 1024 media limit, idempotent draft delivery, `/retry`); and
channel publication (an inline button under the draft, a channel preflight,
an idempotent `send_photo` with the cover, `published_by_user_id`). Step 4
(audio download, fragment, ffmpeg/ASS render) is next; step 5 adds the Mini
App, which edits a published caption through the stored
`channel_chat_id`/`channel_message_id` (D-66). Revoking a published post is
deliberately not implemented (D-65) — a wrong post is deleted by hand in
Telegram, and the row goes on saying `published`.
