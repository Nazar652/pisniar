# AGENTS.md

## What this is

`pisniar` — a Telegram bot that automates posting music tracks to a channel:
resolve a link's metadata, prepare a post, render a vertical video fragment
(the cover on a turning record, the title on a display, the audio and timed lyrics on a glass screen),
publish.

## Stack and runtime

- Python 3.13 (`>=3.13,<3.14`), dependency/venv management via `uv`.
- src-layout, import package `pisniar` (`src/pisniar/`).
- aiogram 3 (long polling) + FastAPI (health checks), Celery + Redis
  (worker), SQLAlchemy 2.0 async + asyncpg, Alembic.
- `yt-dlp` (pinned in `uv.lock`, **never self-updating** — `--no-update` is
  passed explicitly) plus `ffmpeg`/`ffprobe`, both run as subprocesses and both
  present **only in the worker image**, as is Pillow (the `worker` uv group). The `bot` image has none of
  them, and the `media-data` volume (`/data`) is writable only in `worker` and `worker-render` (one image);
  `webapp` mounts it
  read-only (`/data:ro`, same uid `app`) to serve the fragment audio.
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
make check       # uv sync --frozen + uv lock --check + ruff format --check + ruff check + ty check + unit tests,
                 # then the frontend: vue-tsc, ESLint, Prettier, Vitest, API type drift, bundle check
make test         # unit tests in parallel (pytest-xdist -n 4, one worker per file); `pytest path::test` stays serial
make up           # docker compose up -d --wait
make ps            # docker compose ps
make test-int      # integration tests and the Linux-only bench test inside the bot container
make test-fonts    # the subtitle font and the glyph-width table, inside the worker container
make test-media    # the turntable render with the real ffmpeg: ffprobe facts and frame pixels, inside the worker container
make migrate       # alembic upgrade head inside the bot container
make revision m="add lyric table"   # alembic revision --autogenerate
make alembic-check # alembic check: model/migration drift
make api-types     # regenerate frontend/openapi.json and frontend/src/api/schema.ts from the application
make frontend-build # vite build into frontend/dist, which the running webapp serves
make frontend-watch # vite build --watch into frontend/dist: the Telegram dev loop
make dev-token u=<id> # print a session token for an admin, for a browser outside Telegram (a secret)
make peaks          # write the waveform file for every stored audio lacking the current version, in the worker
make bench-render   # time a 45 s and a 120 s render in the isolated `bench` service; prints the .env time limits
make funnel-init    # create the external volume holding the Tailscale node state (once)
make funnel-up      # start the Tailscale Funnel service
make funnel-down    # remove the Funnel container; the node state stays on its volume
make shell         # shell in the bot container
make verify        # full local + Docker verification in one entrypoint
```

The type checker is **`ty`** (Astral), not `mypy` and not `pyright` — do not
suggest or introduce either.

Tests are hermetic. The autouse `outside_access_guard` (root `tests/conftest.py`) refuses and records a
connect, name lookup, datagram or `Celery.send_task` beyond the `outside_access_allowed_hosts` fixture
(loopback; `tests/integration/conftest.py` adds the database host) and fails the test at teardown even
when the code swallowed the error; a test widens the list by overriding that fixture. `make test` runs
`-n 4 --dist loadfile`. The migration tests call `alembic.command` in-process (`run_alembic`, in a thread,
against a disposable database). `make test-int` skips the `fonts` and `media` markers, which need the
worker image.

## Architectural boundaries

- `pisniar/api/` never imports `pisniar/bot/handlers` — this is what keeps
  the health/readiness API extractable into its own service without a
  rewrite.
- `pisniar/worker/` never imports `pisniar/bot/`, and `pisniar/bot/` never
  imports `pisniar/worker/tasks` directly. The bot dispatches Celery tasks by
  name through `pisniar.worker.celery_app`, not by importing task functions.
- `pisniar/bot/` reaches `resolve/`, `post/`, `telegram/` and `worker/` only
  through six modules: `resolve.urls` (syntactic classification, no network),
  `resolve.types` (the `USER_INPUT_SOURCE` constant), `telegram.client`,
  `telegram.keyboards` (the `callback_data` grammar), `telegram.menu` and
  `worker.celery_app` (task dispatch by name, through `pisniar.dispatch`, the leaf the
  Mini App shares: `dispatch_task` and `mark_dispatch_failure`; it imports neither `bot/`,
  `webapp/` nor `worker/`). The resolve pipeline
  (`resolve.service`/`resolve.registry`, the six source adapters) and `post/`
  caption building run only in `pisniar/worker/`.
  `tests/unit/webapp/test_package_boundaries.py` scans the AST imports of
  every `bot/` module and fails on any other module of those four packages.
- `pisniar/resolve/` (six source adapters + ISRC-first matching/merge, plus
  the LRCLIB adapter and the LRC parser) and `pisniar/post/`
  (caption/hashtags/sanitize/render) never import `bot/` or `worker/` — they
  are pure libraries the worker orchestrates. LRCLIB, the YouTube Music lyrics
  and Genius are not among the six: `resolve.service` never calls them. They are asked
  through `resolve.lyrics_lookup`, which opens no session, writes no row and imports
  neither `bot/`, `webapp/` nor `worker/`; `worker.lyrics` (the first two at render time,
  Genius in its own task) and the Mini App's lookup routes share it.
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
- **Every Telegram client is built by `telegram.client.build_bot`, which registers
  `RetryUnsentRequestMiddleware`**: a call is repeated (3 attempts, 1 s and 3 s apart, each logged as
  `telegram_request_retried`) only when `is_unsent_connect_error` proves no request was sent — a
  `TelegramNetworkError` caused by `aiohttp.ClientConnectorError` (SSL and certificate errors included). Timeouts, disconnects and API answers
  are never repeated, since the request may have arrived and a second `sendPhoto`/`sendVideo` would publish
  twice; `GetUpdates` is left to aiogram's own polling backoff. Each attempt has the call's full
  `request_timeout`, so a caller holding a lock across the call (the Mini App's apply holds the post
  `FOR UPDATE NOWAIT`) may hold it across all attempts; the last error propagates unchanged.
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
- **There are two inline buttons and one callback grammar, and the draft carries one of them.**
  «Зробити відео» (`render:<id>`) lives under the draft (`draft_keyboard`, with «Редагувати»);
  «Опублікувати в канал» (`publish:<id>`) lives under the video preview alone.
  `telegram.keyboards.parse_callback` is the only
  thing that reads a payload, and an unknown prefix is refused rather than
  falling through to publication. Both taps follow the same order —
  **database first (lock, guard, status, commit), keyboard second, dispatch
  last** — so a tap the database declined never loses its own button, and a
  double tap has nothing left to press. A publish tap on a post with no preview (an old
  draft's button) is refused with `COVER_IN_EDITOR_ANSWER` (reason `cover_publication_in_editor`)
  and the post is untouched (a `failed` post from `downloaded`/`rendering` gets the `/retry` pointer
  instead, reason `media_stage_failed`); with a preview it must come from exactly that preview message
  (chat id and message id, an inaccessible message counts), else it is refused as outdated;
  a tap on a `failed` post whose failed stage is a media one is refused with a
  pointer to `/retry`. A tap under a **stale preview** (the post's bounds differ from the ones recorded
  on the `render` row at `preview_params_hash`) is refused with `STALE_PREVIEW_ANSWER`, a pointer to
  `/edit {post_id}` (reason `stale_preview`, the log carries both pairs of bounds); the keyboard stays, nothing
  is written or dispatched. The claim reads that row under the post lock through `db.previews`
  (`preview_fragment_bounds`, `preview_is_stale`), the bot's only read of `render`, a leaf the Mini App shares;
  a row with NULL bounds is never stale. «Зробити відео» on a `PUBLISHING` post answers that it is publishing.
  The cover alone is published only from the Mini App (`POST /api/posts/{id}/publish-cover`),
  so without `WEBAPP_PUBLIC_URL` or in a group chat no path publishes it.
- **`RESOLVED -> PUBLISHING` stays legal** (the Mini App's cover publication writes it), and an
  old draft's publish button may still be tapped while a render is running. Refusing that tap
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
  URLs). `parse_mode="HTML"` is passed explicitly, at `deliver_draft()`,
  `publish_to_channel()` and `caption_edit.edit_message_caption_by_kind()`, and nowhere else.
- **The «Редагувати» `web_app` button exists only in a private chat with
  `WEBAPP_PUBLIC_URL` set** (`keyboards.edit_button_rows`: `chat_id > 0` and a URL). Telegram
  answers `400` to a `web_app` button in a group, and that would cost the whole draft, preview or
  publication confirmation, so the rule lives where every keyboard is built. `/edit [post_id]`
  offers the editor of one post, or the list without an id; `telegram.menu` sets the «Пости»
  menu button for each admin's own chat at start (a reset to the default without a URL), and a
  failure for one admin is logged and never stops polling.
- **Editing a sent message is classified by two measured phrases and nothing else**
  (`caption_edit`): `message is not modified` is a success, `message to edit not found` means the
  message is gone, and every other answer propagates. An edit without `reply_markup` removes the
  keyboard, so the caller always passes the one the state calls for (`keyboard_for_state`). The
  existence probe is `editMessageReplyMarkup(reply_markup=None)`: on a live message without a
  keyboard (a channel post) it answers «not modified», on a deleted one «not found»; it is only
  ever aimed at messages that carry no keyboard.
- **The Mini App server (`pisniar/webapp/`) never imports `bot/`, and from `worker/` only four leaf
  modules**: `worker.fragment`, `worker.storage`, `worker.peaks_format` (the peaks file format and
  its validating `read_peaks`, which `worker.peaks` imports too) and `worker.celery_app` (imports
  only celery). They import no other worker module, so the webapp never loads ffmpeg, the tasks, the
  download or the render; the boundary test pins both lists. Of the lyrics sources it reaches only
  `resolve.lyrics_lookup`: never `worker.lyrics`, `resolve.plain_lyrics` or `resolve.sources.genius`
  directly, and its `httpx` client is what those lookups use. The webapp is a producer of **three tasks
  only**: `RENDER_POST_VIDEO_TASK_NAME`, `PREPARE_POST_AUDIO_TASK_NAME` and
  `PUBLISH_POST_TASK_NAME`, through
  `pisniar.dispatch.dispatch_task`; the same test fails on any other `*_TASK_NAME`
  (`DOWNLOAD_POST_AUDIO_TASK_NAME`, which chains a render, included), a direct `send_task` or a spelled-out `pisniar.worker.` name in
  `webapp/`. The limit is enforced by the code and that test, not by the broker credential: `webapp` gets the
  bot's authenticated `REDIS_URL` in `compose.yaml` but no `depends_on: redis`. Its Celery client
  (`webapp.main`, closed on shutdown) connects lazily, so the app serves while Redis is down and only
  this route answers 503. Every route but the three public ones and the signed audio route sits behind
  `require_admin` with a `response_model`. Its reads are
  plain SELECTs through `database.read_from_database`: no lock, one session per call, and a
  database failure is a Ukrainian 503, never a 401. The post list hides `archived` and `draft`,
  pages by id with a cursor (`before_id`, at most 50) and costs one query. Whether a post can be
  edited is `status.editability_of`, a sentence that never names the state; an id beyond BIGINT is
  a 422 before the database sees it. The platform hosts in `/api/meta` are `link_policy`'s own
  table, not a copy.
- **The fragment view (`GET /api/posts/{id}/fragment`, `webapp.fragment_view`) is a plain read**:
  `post_updated_at`, the bounds a render would use (stored ones while they fit the file, else the
  default), the ffprobe-measured `audio_duration_ms` of the **current** `youtubeMusic` videoId,
  `max_length_ms` and `peaks` (`null` with a warning for a missing or corrupt file). It is offered for
  `EDITING`, `RENDERED` and for `FAILED` from `downloaded`/`rendering`; any other status is 409
  `not_choosable` with a sentence of its own (`status.choosability_of`, never a state name), and no
  audio for that videoId is 409 `audio_missing`. `PostDetail.fragment` carries the same verdict from
  the status alone: `{choosable, preparable, preparing, cancellable, reason}`, at most one of the
  first three true (`RESOLVED` and `FAILED(downloading)` are preparable, `DOWNLOADING` is preparing;
  `cancellable` is `EDITING` alone). `PostDetail.cover_publication {allowed, reason}` is
  `status.cover_publication_of(status, failed_from_status, has_preview)`: allowed for `RESOLVED` and
  `FAILED` from `downloading`/`publishing`, only without a preview; `FAILED` from `downloaded`/`rendering`
  answers the `/retry` sentence with or without one.
- **`POST /api/posts/{id}/fragment/audio` prepares the audio before the first render**
  (`webapp.audio_preparation`), in the order database, queue, draft keyboard: post `FOR UPDATE
  NOWAIT`, the preparable verdict (409 `not_preparable`; «Аудіо вже підготовлено.» when choosable),
  the track `FOR NO KEY UPDATE`, a valid `youtubeMusic` videoId (409 `youtube_music_missing`),
  `set_post_status(DOWNLOADING)`, commit; no Telegram or broker call inside. A refused NOWAIT or
  `lock_timeout` is 409 `post_busy`, other database failures 503, a missing queue 503
  `queue_unavailable`. After the commit `PREPARE_POST_AUDIO_TASK_NAME` is enqueued; a refusal runs
  `dispatch.mark_dispatch_failure(expected_status=DOWNLOADING, DOWNLOAD_DISPATCH_FAILED_ERROR)` and
  answers 503 `dispatch_failed`. Only after a successful enqueue the draft loses its keyboard
  (`telegram.preview.remove_draft_keyboard`, 15 s `request_timeout`, best effort). Logs:
  `audio_preparation_requested`, `audio_preparation_refused` (`reason`), `audio_preparation_dispatched`,
  `audio_preparation_dispatch_failed`, `draft_keyboard_removal_failed`. The cover alone cannot be published while the audio is
  prepared (see the cancel route below), which the preparable sentence says beforehand. The route has no
  rate limit: a stolen admin session could loop link edit -> prepare on the posts it can reach,
  bounded by the number of posts, `ensure_free_space` and one download per post at a time.
  The `youtubeMusic` -> videoId parsing is `resolve.urls.stored_link_url` + `track_video_id_of`.
- **`POST /api/posts/{id}/fragment/cancel` gives the prepared audio up** (`webapp.audio_preparation`,
  body `{expected_post_updated_at}` with extra fields refused, no task): post `FOR UPDATE NOWAIT`, the
  verdict `status.cancellation_refusal_of` (only `EDITING`, which a previewed post never reaches; else 409
  `not_cancellable` with a sentence, «Рендер уже почався…» once a render began; `choosability_of`
  derives `cancellable` from the same function), 409 `stale_data` on a stamp mismatch, the track `FOR NO KEY UPDATE`, `set_post_status(RESOLVED)` through the existing
  `EDITING -> RESOLVED` edge, commit. After it `telegram.preview.restore_draft_keyboard` puts the
  draft's keyboard back (`keyboard_for_state(RESOLVED, DRAFT)`: «Зробити відео» + «Редагувати»; 15 s,
  «not modified» is a success, a refusal is logged as `draft_keyboard_restore_failed`). The audio stays
  on the volume, so preparing again reuses it. `DOWNLOAD_ENTRY_STATUSES` is `{DOWNLOADING}`, so a late
  prepare or download on the cancelled post exits without work. Logs `audio_preparation_cancel_requested`,
  `audio_preparation_cancel_refused` (`reason`), and `audio_preparation_cancelled` from the route after the commit.
- **`POST /api/posts/{id}/publish-cover` publishes the cover without a video**
  (`webapp.cover_publication`), in the order database, queue, draft keyboard. Body
  `{expected_post_updated_at}` (extra fields refused). One transaction: post `FOR UPDATE NOWAIT`, the
  verdict `status.cover_publication_of` (409 `not_publishable`: no preview, and `RESOLVED` or `FAILED`
  from `downloading`/`publishing`), the stamp (409 `stale_data`), the track read without a lock, the
  rebuilt caption equal to the stored `post.caption` (409 `caption_unavailable` / `caption_not_applied`),
  `set_post_status(PUBLISHING)`. After the commit `PUBLISH_POST_TASK_NAME` goes out with
  `published_by_user_id` of the session admin; a refusal runs `mark_dispatch_failure(expected_status=PUBLISHING,
  PUBLISH_DISPATCH_FAILED_ERROR)` and answers 503 `dispatch_failed` (`/retry N`). `publish_post` is unchanged
  (no preview: `send_photo`, `kind=photo`; its barrier 1 refuses a second request). Logs
  `cover_publication_requested`, `cover_publication_refused` (`reason`), `cover_publication_dispatched`,
  `cover_publication_dispatch_failed`, `draft_keyboard_removal_failed`.
- **The audio route (`GET /api/posts/{id}/audio?token=`) is the one signed route**, hidden from the
  schema, because `<audio>` sends no `Authorization` header. The token is
  `base64url(json{uid,pid,tid,vid,iat,exp}).base64url(hmac)` under `HMAC(bot_token,
  "pisniar-webapp-media-v1")`, a key of its own beside the session one (`auth.sign_claims` /
  `verify_signed_claims`), so neither token kind verifies as the other. Each request checks the
  signature, `exp`, `pid` equal to the path, a valid `vid` and `uid` in `ADMIN_IDS`, and reads no
  database; `Settings.webapp_media_url_ttl_seconds` (900) is its life. The file is found only from the
  ids by `find_existing_audio`, must resolve inside `audio/`, and goes out through `FileResponse`
  (Range: 206/416) with `Content-Type` from a fixed table (`webm`, `m4a`/`mp4`, `opus`/`ogg`, `mp3`;
  anything else 409 `audio_format_unsupported`), no filename, `nosniff` and `no-store`; a vanished
  file is 404 `audio_gone`. The admin list is read on every request, but removing an admin means editing
  `ADMIN_IDS` and recreating `bot`/`worker`/`webapp`; a signed URL stays valid for a current admin
  for its whole TTL. Logs carry `fragment_audio_served` (`http_status`) and `media_token_rejected`, never the
  token, and a filter on `uvicorn.access` (`webapp.access_log`) cuts the query off every request line.
  The CSP gains `media-src 'self'`.
- **`POST /api/posts/{id}/fragment` starts a render with new bounds, in the order database, queue,
  old keyboard** (`webapp.fragment_choice`). One `write_to_database` transaction: the post `FOR
  UPDATE NOWAIT`, the choosability verdict (`RENDERED`, or `FAILED` from `downloaded`/`rendering`),
  `expected_post_updated_at` (409 `stale_data`), then the track `FOR NO KEY UPDATE`, the `audio_asset`
  of the **current** `youtubeMusic` videoId (409 `audio_missing`), `worker.fragment.validate_chosen_bounds`
  against the measured duration (422 `invalid_fragment`: the sentence with the numbers; the bounds the
  post already has render again), `assign_if_changed` for both bounds and
  `set_post_status(RENDERING)`; no Telegram call and no broker call inside it. A refused NOWAIT or a hit
  `lock_timeout` is 409 `post_busy`, any other database failure 503. After the commit the task is
  enqueued; a refused enqueue runs `dispatch.mark_dispatch_failure(expected_status=RENDERING)`, so the
  post is `FAILED` from `rendering` with a `last_error`, and answers 503 `dispatch_failed` with
  `/retry N` in the sentence (`/retry` then renders the stored bounds; a publish tap under the old
  preview is already refused for a post that is not `RENDERED`). Only after a successful enqueue does
  the old preview lose its keyboard (`telegram.preview.remove_preview_keyboard`, best effort, 15 s
  `request_timeout`): a Telegram refusal is logged there as
  `preview_superseded_keyboard_removal_failed` with the `post_id`, any other exception as
  `fragment_preview_keyboard_removal_failed`; neither fails the request. The request model refuses extra fields, `bool` and values
  beyond BIGINT; the response is `FragmentRenderStarted`. Logs: `fragment_render_requested`,
  `fragment_render_refused` (`reason`), `fragment_render_dispatched`, never a token. Its head is
  `claim_fragment_bounds` (the locks, the verdict, the stamp, the audio and the rule), shared with the save.
- **`PUT /api/posts/{id}/fragment` stores the bounds without a render** (`fragment_choice.save_fragment`,
  body `FragmentRequest`, answer `FragmentSaved {post_id, post_updated_at, start_ms, end_ms, preview_stale}`):
  `claim_fragment_bounds`, then `assign_if_changed` for both bounds (equal bounds write nothing and leave
  `updated_at` alone), flush and refresh for the new stamp; no status change, no Telegram, no broker. Refusals
  are the render route's, logged as `fragment_save_refused` (`reason`); also `fragment_save_requested` and
  `fragment_saved` (`changed`, `preview_stale`). The post's bounds may then differ from the video's: a
  `render` row records the bounds it was cut with (`fragment_start_ms`/`fragment_end_ms`, both or none, written
  by `upsert_render_statement`; NULL on rows that predate them), and `PostDetail.fragment.preview_stale` is
  `db.previews.preview_is_stale` over the row at `preview_params_hash`. After such a save `post.fragment_*`
  describes the next render, not the preview. Publication from the chat is refused under a stale preview (see
  the buttons above); the next render replaces the preview.
- **The lyrics routes (`webapp.lyrics_editing`, `lyrics_proposals`, `lyrics_rules`) are offered like the
  fragment view**: `status.choosability_of` (else 409 `not_choosable`) and the audio of the current
  `youtubeMusic` videoId (409 `audio_missing`), since every time is checked against its measured duration.
  `GET …/lyrics` is a plain read: lines, `edited_by_admin`, `lyrics_version`. `PUT …/lyrics` replaces the
  whole text in one transaction with no Telegram or broker call: the track `FOR NO KEY UPDATE`
  (`lock_track_for_write`, the lock every `lyric` writer takes), 409 `media_in_progress` while a post of the
  track is downloading, downloaded or rendering, `plan_lyrics` (422 `invalid_lyrics` naming the line: the
  count and size limits, times in `[0, duration)` and strictly rising, an empty line only with a time, which
  is a pause), 409 `stale_data` unless `expected_lyrics_version` (sha256 of the stored lines and marker)
  matches, then delete and insert with `source='manual'` and `track.lyrics_edited_at = now()` (an empty list
  removes the text and sets the marker too). This is the one route with a bigger body ceiling: 96 KiB
  (`app.LYRICS_MAX_BODY_BYTES`), 16 KiB elsewhere. `GET …/lyrics/synced` and `GET …/lyrics/plain?source=`
  (`ytmusic`, `lrclib`, `genius`) are proposals, never writes: a plain SELECT for the subject, then
  `resolve.lyrics_lookup` under one 20 s `wait_for` (`LYRICS_LOOKUP_TIMEOUT_SECONDS`), with no session open;
  the only row written is the `source_fetch` cache, in its own short session. Synced order is YouTube Music,
  then LRCLIB; times are kept strictly rising (a tie moves 10 ms), lines past the audio are counted and
  dropped, an over-long line is cut. 409 `synced_lyrics_not_found`, `plain_lyrics_not_found`,
  `lyrics_over_limits`; a source that fails or exceeds the 20 s is 503 `lyrics_source_unavailable`.
  Logs: `lyrics_viewed`, `lyrics_saved`, `lyrics_lookup_requested`, `lyrics_lookup_found`, and
  `lyrics_view_refused`, `lyrics_save_refused`, `lyrics_lookup_refused` with a `reason`.
- **Saving fields from the Mini App is one short transaction with no Telegram call**
  (`webapp.editing.save_fields`): the post `FOR UPDATE NOWAIT`, then the track `FOR NO KEY UPDATE`
  (`db.locks.lock_post_without_waiting` / `lock_track_for_write`, the lock the resolve and the
  `lyric` writers take), then a comparison of both `updated_at` values with the
  `expected_*_updated_at` the editor was opened with; a mismatch is a 409 `stale_data`. A refused
  NOWAIT or a hit `lock_timeout` (both `55P03`) or a cancelled statement (`57014`), read from
  `error.orig.sqlstate`, is a 409 `post_busy`; any other database failure is a 503. Validation is the pure `webapp.fields.plan_fields`,
  shared by the save and by `POST /api/posts/{id}/preview` (which writes nothing), and a bad edit is a
  422 `invalid_fields` listing every field in `errors[{field, message}]`. `assign_if_changed` skips an
  equal value, so a save without changes writes nothing and leaves `updated_at` alone. A name joins
  `manual_fields` only when its value changed (a hashtag alone does not freeze the artist names);
  «back to automatic» removes it, and changing and reverting one field together is a 422. Editing the
  `youtubeMusic` link is refused (409 `media_in_progress`) while any post of the track is
  downloading, downloaded, rendering or editing. The response carries the rebuilt caption and its
  `sha256`, which is what applying the caption to a message later compares against.
- **Applying a caption (`POST /api/posts/{id}/apply`) edits Telegram first and stores second**
  (`webapp.applying`): the post is locked `FOR UPDATE NOWAIT` through exactly one Telegram call and
  the track is read with a plain `get`, never locked. The caption is rebuilt from the rows, its
  sha256 must equal `expected_caption_sha256` (409 `caption_changed`), and more than 1024 visible
  characters is a 422 with the number. The first message is edited with a 15 s `request_timeout`
  through `caption_edit.edit_message_caption_by_kind`: `RESOLVED` the draft, `RENDERED` the preview,
  `FAILED` the draft, `PUBLISHED` the channel message by `channel_message_kind`; the keyboard comes
  only from `keyboard_for_state`, because an edit without `reply_markup` removes it. «not modified»
  is a success; «not found» (409 `channel_message_missing`), a 429 (names the seconds), a 403 and
  any other answer roll back with nothing stored. The remaining messages (the draft under a preview,
  the preview of a failed post) are edited after the commit, best effort, and named in
  `other_messages_failed`. The webapp owns a `Bot` client with `parse_mode=None`, closed on shutdown.
- **`POST /api/posts/{id}/archive` archives a published post only on a measured deletion**: under
  the post lock, `probe_message` on the channel message; the verbatim «not found» runs
  `set_post_status(ARCHIVED)` and logs `post_archived_channel_message_missing`, a live message is a
  409, and any other answer refuses with the post untouched.
- **Every edit and archive action logs one line with `user_id` and `post_id`**: `fields_saved`,
  `fields_previewed`, `caption_applied`, `post_archived_channel_message_missing` on success, and
  `fields_save_refused`, `fields_preview_refused`, `caption_apply_refused`, `post_archive_refused`
  with a `reason` on every refusal. The layer that decides a refusal logs it; `posts.refusals_logged`
  adds only the four it cannot (`post_not_found`, `post_busy`, `database_unavailable`,
  `telegram_unavailable`) and `invalid_fields`, which carries the field names and no sentences. No
  line carries `initData`, a session token, the bot token or `TS_AUTHKEY`.
- **The Mini App frontend (`frontend/`) is Vue 3 + TypeScript, built by Vite, checked on the host.**
  Node is pinned to 24.16.0 (`.node-version`, `engines`, `engine-strict`, and the one `NODE_IMAGE`
  in the `Dockerfile`); `.npmrc` sets `ignore-scripts`, `save-exact`; dependencies are installed with
  `npm ci` from the committed lock and the lock changes only through `npm install`. `make check`
  runs `vue-tsc` (strict, `noUncheckedIndexedAccess`), ESLint (`--max-warnings 0`, `vue/no-v-html`,
  no explicit `any`, no suppressions, `reportUnusedDisableDirectives`), Prettier (`lf`), Vitest, the
  generated-types drift check and the bundle check (no inline script or style, no `eval` or
  `new Function`, no dev marker, no `data:` asset, initial JS at most 120 KB gzip). `openapi.json`
  and `src/api/schema.ts` are generated by `make api-types` and never edited by hand. The SDK types
  are the hand-written `src/telegram/webapp.d.ts`. Nothing under `src/dev/` is imported except by the
  `import.meta.env.DEV` branch of `src/main.ts`; the dev shim only supplies an empty `initData` and a
  token a developer stored from `make dev-token`. Node exists only in the `frontend-build` stage:
  `webapp` copies its `dist` and `bot` and `worker` never see it. The `webapp` healthcheck is strict
  (`/` must answer 200), and `webapp_init_data_received` logs the documented `initData` field names
  and whether `signature` is present, never a value.
  The screens are `PostList` and `PostEditor` (no router, no store): the logic lives in
  `usePostList` and `usePostEditor`, which Vitest drives against a fake server. Before publication
  «Зберегти» is `PUT …/fields` followed at once by `apply` with the hash the save returned; for a
  published post the save stops there and «Застосувати в каналі» is a separate `showConfirm` step.
  A 409 `stale_data` or `caption_changed` reloads the post, `channel_message_missing` is the only
  thing that shows the archive button, and every server sentence is shown as it came. The caption
  preview renders segments by interpolation, links only for `http(s)`, and never `v-html`.
  The «Фрагмент» section (`FragmentSection`, in `PostEditor`) offers the fragment editor when
  `PostDetail.fragment.choosable`; on `preparable` or `preparing` the button starts
  `useAudioPreparation` (`idle -> requesting -> preparing -> ready | failed | timed_out`, a read of
  `GET /api/posts/{id}` every 3 s for at most 210 s, stopped on unmount), `ready` opens the editor and
  emits `prepared`, which `PostEditor` answers with `reloadIfClean()`; otherwise the reason sentence.
  With `fragment.cancellable` a quiet «Скасувати підготовку» (`useCancelPreparation`, no `showConfirm`:
  the step is reversible) posts the server's stamp (`postStamp` prop) and emits `cancelled`, again
  answered with `reloadIfClean()`; `stale_data` and `not_cancellable` reload too. `FragmentEditor` has a
  quiet «Зберегти фрагмент» (`useFragmentEditor.save`, state `saving`, `canSave` = moved and within the rules,
  no discard question): it keeps the view, so the waveform is not rebuilt, moves the stored bounds and the
  stamp, and says «Фрагмент збережено.» or, with `preview_stale`, that the preview is out of date and the
  video must be made again. `FragmentSection` then shows «Обраний фрагмент» and a notice instead of «Зараз у
  відео»; `PostEditor` answers `saved` with `reloadIfClean()` or, over unsaved fields, `adoptFragmentSave`
  (only the fragment data and the stamp, only while the stamp is the one the save was sent with), and bumps
  `fragmentRevision`, which makes an open `useLyricsEditor` call `refreshFragment()` (bounds and stamp; text
  and audio stay). `client.ts` answers a proxy's 502/504 with «Сервер перезапускається або недоступний…» unless
  the body carries a server sentence. A quiet card at
  the bottom of `PostEditor` offers «Опублікувати без відео» (`usePostEditor.publishWithoutVideo`)
  when `cover_publication.allowed`; it needs a saved caption (`stored_caption_matches`, nothing
  dirty), asks `showConfirm` and posts the server's `expected_post_updated_at` string. `FragmentEditor` loads as its own
  async chunk (wavesurfer 7.12.12, MediaElement backend, the server's peaks and duration so nothing is
  decoded, one `RegionsPlugin` region); the instance lives in a `shallowRef` + `markRaw`, is created
  after the view arrives and destroyed on unmount, and vertical swipes are off while it is open
  (`isVersionAtLeast('7.7')`). The logic is pure `fragment/bounds.ts` (100 ms grid, the server's
  limits, ±1 s / ±0.1 s steps that move one edge) and `useFragmentEditor` (`loading -> ready ->
  submitting -> submitted | failed`); `expected_post_updated_at` is the server's string, never a
  `Date`. The bundle check also fails when an initial script contains wavesurfer, when no lazy
  chunk does, or when the chunk carrying it exceeds 40 KB gzip. wavesurfer writes one `<style>` into a shadow root, so the prod CSP is
  `style-src 'self' 'sha256-…'` (`webapp.app.WAVESURFER_STYLE_HASH`, valid for
  `WAVESURFER_VERSION` at `waveform.WAVEFORM_HEIGHT_PX`); `tests/scripts/csp.test.ts` recomputes it
  from the installed package and a Python test ties the version to `package.json`. Never
  `'unsafe-inline'`.
  The «Текст» section (`LyricsSection`, in `PostEditor` when `fragment.choosable`) opens `LyricsEditor`, its
  own async chunk without wavesurfer (the bundle check fails when no lazy chunk holds it, when an initial
  script does, or when it contains or loads wavesurfer). `useLyricsEditor` (`loading -> ready <-> timing ->
  saving -> saved | failed`) reads `GET …/lyrics` and `…/fragment` and saves with `PUT …/lyrics`; «Зберегти й
  зробити відео» then posts the fragment route with the server's bounds. The rules are pure
  `lyrics/timing.ts`, mirroring `plan_lyrics`: a tap (pointerdown while playing, Space; Backspace undoes)
  times the cursor line at the position minus `TAP_LATENCY_MS` × playback rate (provisional 0, never
  measured on a device); a tap not after the previous time is refused with a sentence, a tap that passes
  later times clears them, and undo is the exact inverse of a splice. «♪» inserts a timed line «♪» and
  «Програш» a timed empty line (a pause) at the playhead; times nudge on a 10 ms grid between their
  neighbours. «Знайти синхронізацію» and «Знайти просто текст» (a source `<select>`, Genius first) fetch a
  proposal that replaces the buffer, after `showConfirm` when the buffer is not empty. Each editor with
  unsaved work (the fields form, lyrics, fragment) reports to `app/leaveGuard.ts`: the Telegram back button
  asks once, and the closing confirmation follows «any source dirty».

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
  to that platform (Bandcamp by suffix). A candidate whose platform key is not listed at all
  is dropped silently and logged at info as `platform_link_ignored`; a known platform's link
  that breaks the rules is refused: it is not stored; it is logged as `platform_link_rejected_by_allowlist` and
  carried in `ResolvedTrack.link_rejections`, which the worker's alert under the
  draft lists next to `source_failures`. Manual and already-stored links skip this
  check — a manual link is validated where it is entered (`check_manual_link`: track
  link of that platform, valid `videoId` for YouTube), and the album field has its
  own parser (`parse_spotify_album_link`, stores the id).
- **A caption is built from stored rows only** (`post.caption_input`): the worker
  and the Mini App read `track`/`post` JSONB through the same
  `build_caption_from_rows`, which also returns what did not fit
  (`CaptionDegradation`). Keep it that way — a second conversion drifts.
- **The caption has four lines, no blank between them** (`post.caption`): `Artists — Title`; `«Release title»`
  (album/ep with a title only, linked to the Spotify album when the id is valid, no type word anywhere);
  `#artists · year`; `#languages · #genres`. Then a blank, the links, a blank, the channel link. Shown platforms are
  `PLATFORM_DISPLAY_ORDER` (spotify, youtubeMusic, appleMusic, deezer, youtube, soundcloud, bandcamp), a manual
  `youtube` clip first as «YouTube (кліп)»; every other stored link stays in `platform_links` but is filtered before
  degradation (never in `dropped_platforms`). The Mini App lists only shown platforms (`/api/meta`,
  `PlatformLinkDetail.shown_in_caption`), and `plan_platform_links` keeps a hidden stored link the input omits.
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
- **The render has its own queue and service.** `build_celery_app` routes `RENDER_POST_VIDEO_TASK_NAME`
  alone to `render`; `worker` takes `celery`, `worker-render` (`-c 1`, same image, env and volume) takes
  `render`, each healthcheck pings its own node with `-d`, and `compose.override.yml` spells the render command
  out because the dev image's CMD is the default queue. `claim_post_for_rendering` restamps `updated_at`, so
  the `/retry` window counts from the real start, not from the time spent waiting in the queue.
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
- **The default fragment rule has no heuristics**: start at 0, length 45 s,
  a shorter track taken whole. `post.fragment_start_ms`/`fragment_end_ms` are
  absolute from the start of the **full** track. Bounds already on the post win
  as long as they still fit the downloaded file. `fragment.validate_chosen_bounds`
  is the rule for a chosen fragment: `0 <= start < end <= audio duration`, at most
  `MAX_FRAGMENT_LENGTH_MS` (120 s), no minimum.
- **The peaks file** `/data/audio/<track_id>/peaks/<video_id>.json` is derived from the
  audio, written (`worker/peaks.py`) after each download, and sits outside the
  `<video_id>.*` mask of `find_existing_audio`. A failure is a warning, never a failed download.
  The 20 values a second are the **RMS level smoothed over about a second** (10 buckets each side
  of 50 ms), scaled so the 99th percentile is 255 and clipped: a sample peak on a compressed
  master touches the ceiling in nearly every bucket and shows no structure. A file carries
  `format_version` (`peaks_format.FORMAT_VERSION`, 2); `read_peaks` treats a well-formed file of
  another version as absent (info `audio_peaks_other_version`, not a corrupt-file warning), so the
  next download or `make peaks` recomputes it. A post with a preview never downloads again, so
  `make peaks` (`python -m pisniar.worker.peaks`, in the worker) walks every stored audio
  (`storage.stored_audio_files`), skips current files, stages in `/data/tmp/peaks-backfill/` and
  is safe to run twice; its exit code is 1 when any file failed.
- **The frame is a turntable: Pillow draws the still layers, ffmpeg turns the vinyl** (`worker/layout.py` holds
  every coordinate and imports nothing, `worker/turntable.py` is the only module that imports PIL and
  `pillow` lives in the `worker` uv group alone, so `bot` and `webapp` never carry it). `write_layers` paints five
  PNGs per render from fixed seeds (wood body, vinyl, label mask, hub, and the overlay with the tonearm);
  the cover is cropped into the 300 px label, the vinyl layer rotates at 33⅓ rpm
  (`render.DISC_REVOLUTIONS_PER_SECOND`), and the title and artists are always drawn on the display
  pane (`ass.build_display_subtitles`), even for a song without text. The lyrics sit on the **glass screen**
  (`layout.SCREEN_BOX_PX`, 44…676 x 842…1228): three slots, previous, current, next, centred by `\an5` plus an
  explicit `\pos` at `ass.PREVIOUS_SLOT_Y`/`CURRENT_SLOT_Y`/`NEXT_SLOT_Y` (905/1035/1165, `SLOT_SPACING_PX`
  130). The current line is 52 px white and wraps to two lines, the neighbours 38 px grey and one line; the
  rest is cut with `…`. `test_ass.py` pins the slots inside the screen and measures a two-line current line
  against its neighbours. The neighbours are the nearest lines that draw something. An entry lasts until
  the next one starts, with no ceiling (`ass.LAST_LINE_MAX_MS`, 8 s, is the last entry's alone, and the fragment
  bounds everything), so a long gap never blanks the glass. A pause empties the middle slot and keeps the sides;
  windows that draw, pauses included, count toward `MAX_RENDERED_LINES`.
- **Only the disc is composed per frame.** The stills are single frames that `loop` repeats (no input carries
  `-loop 1`); the floor is converted to YUV once with the display drawn on it, and only `render.disc_square`,
  everything that moves, is composed in `gbrp` and converted per frame. `scale=...:in_range=pc` reads the
  full-range JPEG cover, every YUV conversion is `out_range=tv` and `-color_range tv` is set, because a naive
  `format=yuv420p` yields `yuvj420p`; `subtitles=` takes `yuv420p` directly and changes neither. The video
  is 720x1280, H.264 high 4.0 + AAC, 30 fps, `+faststart`, `-maxrate`/`-bufsize`; its size is asserted against
  the 50 MB Telegram ceiling with exactly one lower-bitrate retry (`REDUCED_QUALITY`) before the post fails.
  A 120 s fragment renders to ~22 MB, so that branch guards a render defect, not normal input.
- `render.RenderParameters.subtitles_path` is an **optional path passed in from
  outside**, not a flag decided inside: `None` means the filtergraph has no lyrics
  `subtitles=` step at all, never an empty file. The display's `.ass` is a required path of its own.
- **`render.RENDERER_VERSION` (`turntable-1`) is bumped with any change to the graph, the encoder, `layout`
  or the drawing**; it is an input to `params_hash`, and `test_video_render.py` pins it to a digest of the
  graph and the layout, so a forgotten bump fails.
- **`make bench-render` runs in its own `bench` service** (`compose.yaml`, profile `bench`, the `worker`
  image target): no `env_file`, no volume and `network_mode: none`, so synthetic renders cannot reach the
  secrets, the media volume or the broker. `pisniar.worker.bench` imports only the render pipeline, works under
  `/tmp`, removes it in a `finally` and prints the 45 s and 120 s timings, memory and the `.env` lines for
  `RENDER_TASK_*`; run it on the production host before setting them.
- **The chain is started by a button and continues by itself.** «Зробити
  відео» dispatches `download_post_audio`; that task dispatches
  `render_post_video` **after** its own commit, so a redelivered render can
  never outrun the `DOWNLOADED` it needs. A failed internal dispatch fails
  the post, exactly like a failed resolve dispatch, rather than leaving it in
  a state no task will move it out of.
- **The preview is the only safeguard before publication.** Publication cannot be
  revoked, so `RENDERED` means «прев'ю вже в чаті адміна»: the render
  task sends the video, writes `preview_chat_id`/`preview_message_id`/`preview_params_hash`, sets
  the status and commits, in that order — the draft's `RESOLVED` rule,
  mirrored. A delivery failure
  therefore happens while the post is still `RENDERING`, which is the state
  that can legally fail — `RENDERED` deliberately has no edge to `FAILED`.
  The text draft is a separate message (`editMessageMedia` cannot turn text
  into media), so it loses its keyboard when the preview arrives: a new draft
  has no publish button, but an older draft or its render button would still start
  a second path beside the preview.
- **That transaction holds the post lock across one Telegram upload**, and
  that is deliberate — it is the same delivery guard `publish_post`'s barrier
  1 is, and the `RENDERING` status read under that lock is what makes it
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
- **A post with a preview is never downloaded again**, and «Зробити відео»
  refuses it with a sentence of its own before any status changes. It can be
  rendered again only from `RENDERING`, reached by the `RENDERED -> RENDERING`
  edge (the one way back; `RENDERED -> FAILED` and `/retry` on `RENDERED` stay
  refused). The render claim does not look at `preview_message_id`; a redelivered
  render finding `RENDERED`, `PUBLISHING`, `PUBLISHED` or `FAILED` exits without work.
  `finish_render` then sends a new preview under the post lock, writes the new
  reference, `preview_params_hash` and `RENDERED` in one commit, and afterwards
  removes the old preview's keyboard best-effort. `video_for_publication` picks
  the `render` row `(post.id, post.preview_params_hash)`, never the newest, so
  publication sends the file the admin last saw.
- **A media task never acts on a `failed` post**: it can only get there by
  redelivery. Every bot path (the button, each `/retry` branch, a stale
  reclaim) writes the target status before it dispatches, and the download
  commits `DOWNLOADED` before it chains the render. So `FAILED` is absent from
  `DOWNLOAD_ENTRY_STATUSES`/`RENDER_ENTRY_STATUSES`, though the transition
  table allows it.
- **`EDITING` means «the audio is ready, a person is choosing the fragment»**, reached only by
  `prepare_post_audio` (the Mini App's task: the download body without the render chain,
  `perform_download_post_audio(post_id, *, then_render=False)`). `finish_download` writes
  `DOWNLOADED` and then `EDITING` in one commit and logs `audio_ready_for_fragment_choice`; it
  sends no task. `RENDER_ENTRY_STATUSES` is `{DOWNLOADED, RENDERING}`: a render that finds
  `EDITING` is not ours and exits. `EDITING` is not in `STALE_STAGE_BRANCH` (nobody is stuck),
  so the publish tap, «Зробити відео» and `/retry` answer `AUDIO_READY_ANSWER`
  (reason `awaiting_fragment_choice`) and leave the post alone. `POST …/fragment` takes it to
  `RENDERING` for the first preview; `DOWNLOADED` still means «render queued». `/retry` on
  `FAILED(downloading)` or a stuck `DOWNLOADING` runs the chat download, with the default render.
- **Cleanup has three levels.** `/data/tmp/<post_id>/` goes in a
  `finally`, always. Artifacts of deleted rows go **after** the commit — the
  reverse order would leave a surviving row pointing at a deleted file, and
  an orphan file is the safer half. Retention: the video is removed
  after a **successful** publication (Telegram holds the copy now), audio
  is never removed, and there is no sweeper. The `render` row stays as the
  record of what was published; only its file is gone. A new preview also
  removes, best effort after its commit, the `.mp4` and `.jpg` of the post's
  other `render` rows, keeping the file `preview_params_hash` points at.
- **The thumbnail is derived, not stored.** `<params_hash>.jpg` sits beside
  its `.mp4`, cut from the rendered file's own first frame, so it needs no
  column and the same directory removal takes both. It is cosmetic:
  `render_thumbnail()` returns `None` instead of raising, and a missing file
  is not a send failure — Telegram builds its own preview.

## Lyrics and subtitles (`worker/lyrics.py`, `worker/ass.py`, `resolve/lyrics_lookup.py`, `resolve/sources/lrclib.py`)

- **The text path runs in the render's long phase, with no session open.**
  One read returns `StoredLyrics(renderable_lines, total_count, edited_by_admin)`.
  Timed lines win (`lyrics_already_stored`); lines with none timed are
  `lyrics_untimed`; a saved text with zero lines is `lyrics_removed_by_admin`.
  Each has its own notice sentence and none asks a source. Only when nothing is stored
  and no human saved the text does `worker.lyrics.prepare_lyrics` ask the sources, persist in a
  short transaction of its own and read the lines back as plain values. The order is
  **YouTube Music timed text, LRCLIB timed text, YouTube Music plain text, LRCLIB plain
  text**; LRCLIB is not asked after a timed YouTube Music answer. The first answer with lines
  is written under its own `source` (`ytmusic`, `lrclib`); plain text is written with
  `start_ms` NULL, so the video has no text, the reason is `lyrics_untimed` and the admin's
  editor has the lines. Each source is isolated by `lyrics_lookup.ask_source` (a failure is a log line
  naming it, the soft limit and `SQLAlchemyError` pass); no source answering with lines
  leaves LRCLIB's class as the reason. A text past `MAX_LYRIC_LINES` or
  `MAX_LYRICS_TOTAL_CHARACTERS` is not written. Then
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
- **The YouTube Music lyrics (`resolve/sources/ytmusic_lyrics.py`) are asked for the
  `youtubeMusic` videoId, the recording the audio comes from.** Two `ytmusicapi` calls
  (`get_watch_playlist` for the lyrics page id, then `get_lyrics(timestamps=True)`) run in one
  thread under `wait_for`, through `SourceCache` (`ytmusic:lyrics:<videoId>`). The shape
  classifies: timed lines are synced, a string is plain, no page or `None` is absent, anything
  else raises `YtmusicLyricsApiError` and is never cached. A timed `♪` (YouTube Music's mark for an
  instrumental gap) is stored as the text «♪» and shown on the glass screen like any line, so an
  instrumental break reads as a note; Noto Sans lacks U+266A, and libass takes the glyph through fontconfig
  from a fallback Noto family. An empty line (LRC) stays a pause, plain text drops the mark, and a text of
  only gaps is absent. Lines are sorted by time.
- **Plain text of the remaining sources is a task of its own** (`fetch_plain_lyrics`, queued by
  the render after the preview and its notice, only for `lyrics_absent`,
  `lyrics_rejected_by_matching` and `lyrics_source_unavailable`; a refused enqueue is a log
  line). It reads the track, asks `resolve.plain_lyrics.PLAIN_LYRICS_SOURCES` with no session
  open, and writes under the track lock with the same guard, so it never writes over lines or a
  saved text; it has its own limits (`plain_lyrics_task_*`, 60/90 s) and no Celery retry.
  The one source is **Genius** (`resolve/sources/genius.py`), read by scraping the song page
  because its API serves no text: the search hit must pass `matching.is_match` (names stripped of
  `(…)`/`[…]` annotations; the credit as a whole or any one primary artist), must not be a
  translation, must have `lyrics_state` complete and a path matching `/<ascii>-lyrics`, and the
  page URL is built from that path on `genius.com` with redirects not followed. The HTML goes
  through a stdlib parser that keeps only the text of `data-lyrics-container` elements (not the
  `data-exclude-from-selection` ones), `[…]` section labels are dropped, a page with no such
  text raises and is never cached. The terms of use of Genius forbid scraping; the owner accepted
  that risk.
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
  which checks the family against `fc-list :lang=uk` inside the worker, and that an installed family carries
  U+266A for the note (`fc-list :charset=266a`), not necessarily `FONT_FAMILY`.
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
  fallback search add up to ~187 s, and `make bench-render` recommends 600/660 s for `render_task_*` on two
  shared vCPUs; the hard limit is also the stale-render window `/retry` waits out.

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
- Deploying the render queue: `make up` does not build, so run
  `docker compose up -d --build worker worker-render` (a stale image has the old CMD and node name).
  Drain the queue first: a render already waiting on `celery` would run on the default worker.
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
  one-sided pair plus a partial unique index. `post.preview_params_hash` is the
  `render.params_hash` of that preview, written in the same transaction as the pair; a `CHECK`
  keeps it NULL exactly when `preview_message_id` is.
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
  `fragment_start_ms`/`fragment_end_ms` (nullable BIGINT, a CHECK keeps them both NULL or a valid range) are
  the bounds the file was cut with; the migration backfilled them for the preview of each `rendered` post.
- `lyric` is one line per row, keyed to a **track**
  (`ON DELETE CASCADE`, like `audio_asset`), so posts of one recording share
  the text. `start_ms` is nullable: NULL is a line not yet timed (the `>= 0`
  CHECK stays). A synced answer writes its timed lines only; an unsynced one
  writes its plain lines with NULL. `line_index` is
  the only order, `start_ms` orders nothing, and there is no `end_ms` — the
  render derives it. The render path reads the table only through
  `models.renderable_lyrics_statement`, whose `start_ms IS NOT NULL` keeps an
  untimed line out of the `.ass`. `MAX_LYRIC_LINES` and
  `MAX_LYRICS_TOTAL_CHARACTERS` bound a stored text. `text` is raw
  as the source gave it (the ASS substitution happens at render time) and
  bounded at 500 characters by a CHECK; a longer line is cut, never dropped,
  because a `line_index` gap would read like a line deleted by hand. `source`
  is `'ytmusic'`, `'lrclib'`, `'genius'` or `'manual'`.
- **Lines are written automatically only onto a track that has none, of any
  `source`, and whose `lyrics_edited_at` is NULL.** That column marks a text a
  human saved, an empty one included, so a removed text is not refilled.
  The writer locks the track `FOR NO KEY UPDATE` (the lock the resolve takes, which
  `INSERT INTO post` still passes), counts its lines, reads the marker and inserts
  only when the count is zero and the marker is NULL, in one short transaction.
  `persist_under_track_lock` takes the `source` it writes. The lock is what makes that guard
  atomic: under READ COMMITTED a second writer counts only after the first
  commits, while `UNIQUE (track_id, line_index)` stops a duplicate index, not a
  mix of two writers' lines. **Every writer of `lyric` must take the same
  lock**, the Mini App's `PUT …/lyrics` (the one writer that replaces the lines, `source='manual'`, and sets the
  marker) included. An insert that lands short raises and
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
delivers a draft with `/retry` on failure. The draft offers «Зробити відео» and
«Редагувати»; the Mini App's «Опублікувати без відео» sends the cover with that
caption as `send_photo` and records
`published_by_user_id`.

«Зробити відео» starts the media chain instead: the worker downloads the
audio from `youtubeMusic`, picks the fragment, takes the track's stored
lyric lines or fetches them (YouTube Music, LRCLIB; plain text from Genius afterwards), renders a verified
720x1280 turntable video (the cover as the record's label, the title and artists on the display, the
previous, current and next line on the glass screen) on the `render` queue, and delivers it as a
**preview with a publish button**. A video without text still reaches the
preview, with a notice under it saying why. Publication sends that same file
with that same caption as `send_video`, and the file is deleted afterwards.

From a preview the admin can open the Mini App's fragment editor, pick other
bounds on the waveform and render again, or time the lyrics in the lyrics editor (tap each line while the
audio plays, or fetch a synced text or a plain one to start from) and render with them: the post goes back to `RENDERING`, a
new preview replaces the old one as the only publishable message, and
publication sends the file `preview_params_hash` points at.

Revoking a published post is deliberately not implemented — a wrong post is
deleted by hand in Telegram, and the row goes on saying `published`.
