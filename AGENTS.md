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
  classification, no network) — the full resolve pipeline
  (`resolve.service`/`resolve.registry`, the six source adapters, `post/`
  caption building) runs only in `pisniar/worker/`.
- `pisniar/resolve/` (six source adapters + ISRC-first matching/merge) and
  `pisniar/post/` (caption/hashtags/sanitize/render) never import `bot/` or
  `worker/` — they are pure libraries the worker orchestrates.
- A post's status changes **only** through `set_post_status()`
  (`pisniar.db.states`). Never assign `post.status = ...` directly — it skips
  transition validation and the `last_error` bookkeeping that function owns.
- `RESOLVED` means **the draft is already in the admin's chat** (D-38), so
  `deliver_draft()` runs *before* `set_post_status(RESOLVED)` and the commit.
  There is deliberately no `RESOLVED -> FAILED` edge; a delivery failure
  happens while the post is still `RESOLVING`, which can legally fail.
- The `RESOLVING -> RESOLVING` self-edge exists for Celery redelivery only.
  Manual `/retry` from `RESOLVING` is refused in `bot.handlers` (D-43).
- A Telegram alert delivery failure (`pisniar.telegram.notifier.send_alert`)
  never raises into business logic and never causes a Celery task retry.
  Draft delivery (`pisniar.telegram.draft.deliver_draft`) is the opposite on
  purpose: a draft is the product, so its failures propagate to `failed`.
- The bot's shared `Bot` client stays `parse_mode=None` globally — the same
  client sends alerts, and alert text regularly contains `<` (tracebacks,
  URLs). `parse_mode="HTML"` is passed explicitly, only at `deliver_draft()`.

## Resolver conventions (`pisniar/resolve/`)

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
  missing from the fresh result is kept, and a fresh result wins a tie.
- `track.artists` = `[{"name", "hashtag", "spotify_id"}]`; `hashtag` is
  `null` until a human overrides it (`post.hashtags.default_hashtag()`
  computes the display default at caption-build time, it is not stored).
- The `source_fetch` cache is written through `resolve.cache.SourceCache`,
  which opens **its own short session** per call — never the caller's
  business transaction. Rows go stale after `SOURCE_FETCH_TTL_SECONDS`
  (1 hour, D-40) and are refreshed with `INSERT ... ON CONFLICT DO UPDATE`.
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
  (`pisniar.post.render.render_html`). Whoever sends it (step 3's channel
  publish included) must pass it through as-is with `parse_mode="HTML"` —
  escaping it again would double-encode `&amp;` into `&amp;amp;` etc.

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

Step 1 (infrastructure skeleton) and step 2 (link -> post draft: URL
classification, ISRC-first resolution across six sources, caption building,
idempotent draft delivery, `/retry`) are implemented. Step 3 (channel
publish, 1024-char caption limit) is next.
