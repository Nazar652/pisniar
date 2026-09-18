# AGENTS.md

## What this is

`pisniar` — a Telegram bot that automates posting music tracks to a channel:
resolve a link's metadata, prepare a post, render a karaoke-style video
fragment, publish. This repo currently holds only the infrastructure
skeleton (config, logging, DB layer, health checks, a diagnostic bot
command, Celery) — no track-resolution or rendering logic yet.

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
- A post's status changes **only** through `set_post_status()`
  (`pisniar.db.states`). Never assign `post.status = ...` directly — it skips
  transition validation and the `last_error` bookkeeping that function owns.
- A Telegram alert delivery failure (`pisniar.telegram.notifier.send_alert`)
  never raises into business logic and never causes a Celery task retry.

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

Step 1 (infrastructure skeleton) is implemented. Step 2 (link → post draft:
Odesli resolution, caption building) is next.
