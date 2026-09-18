# pisniar

A Telegram bot that automates posting music tracks to a channel: resolve a
link's metadata, prepare a post, render a karaoke-style video fragment,
publish. See `AGENTS.md` for a terse, code-facing map of the stack and the
architectural boundaries that are easy to break without knowing them.

This repository is currently at **step 1**: the infrastructure skeleton
(typed config, structured logging, the async DB layer with the first
migration, an admin-only aiogram bot with a `/diag` diagnostic command,
health checks, and a Celery worker). No track resolution, rendering, or
channel publishing exists yet.

## Prerequisites

- Docker Desktop, running.
- `uv` (dependency/venv manager) and GNU Make on the host — both optional
  for pure Docker workflows, but needed for `make check` (lint/type/unit
  tests, which run faster on the host than inside a Windows bind mount).
- A Telegram bot token, two admin `user_id`s, and a chat id for alerts — see
  [Configuration](#configuration).

## Configuration

Copy `.env.example` to `.env` and fill in the values only you know (bot
token, admin ids, alert chat id). **Never commit `.env`** — it is
git-ignored. `DATABASE_URL` / `REDIS_URL` are deliberately not in
`.env.example`: they are the in-network DNS names of the `postgres` /
`redis` compose services, fixed in `compose.yaml`, not something you fill in.

## Running

```
make build   # docker compose build
make up      # docker compose up -d --wait — brings up postgres, redis,
              # runs the one-shot `migrate` service, then starts bot + worker
make ps      # confirm everything is healthy
```

`compose.override.yml` is picked up automatically and adds, for local
development only: bind-mounting `src/`/`alembic/` into the containers with a
`watchfiles`-based hot-reload wrapper, and publishing `postgres`/`redis`/the
bot's health API on `127.0.0.1` only (never on `0.0.0.0`).

Stop everything: `make down`. Stop and also drop the Postgres volume (a
clean-slate restart): `make clean`.

## Everyday commands

Run `make help` for the full list. The ones you will use most:

```
make check        # ruff format --check + ruff check + ty check + unit tests (host, no Docker)
make test-int      # full test suite (unit + integration) inside the bot container
make migrate       # alembic upgrade head
make revision m="add lyric table"   # alembic revision --autogenerate
make alembic-check # confirm models and migrations agree
make shell         # shell in the bot container
make psql          # interactive psql in the postgres container
make logs          # follow logs from every service
```

`make verify` runs the full local + Docker verification chain in one go
(equivalent to this project's V1-V13 checks), ending with a clean
`down -v` + `up -d --wait` to confirm the stack is reproducible from
scratch.

## Manual acceptance checklist

With a real `.env` in place and the stack up (`make up`):

1. `/start` from an admin user → a reply. From anyone else → no reply at
   all, and a `WARN`-level `update_rejected_non_admin` log line from `bot`.
2. `/diag` from an admin → creates a `track` + `post` row, dispatches a
   Celery `ping` task, replies with `post_id`/`celery_task_id`; the `worker`
   log and the `bot` log both carry the same `post_id`; the worker sends its
   own alert to the configured chat.
3. `curl http://127.0.0.1:18080/healthz` → `200`, even if Postgres is down.
4. `curl http://127.0.0.1:18080/readyz` → `200` normally, `503` with a
   generic `{"status": "unavailable"}` body (no DSN, host, or stack trace)
   if Postgres or Redis is stopped.
5. Edit a `.py` file under `src/` while the stack is up → `bot`/`worker`
   restart on their own within a few seconds (`WATCHFILES_FORCE_POLLING=1`
   is required on Windows bind mounts; without it, hot reload silently does
   not work).
6. `UPDATE post SET status = 'nonsense';` via `make psql` → rejected by the
   `ck_post_status_valid` CHECK constraint.
7. `docker compose logs | grep <the real bot token>` → empty output. Do this
   by hand; never script a check that echoes the token itself.
8. `git status` stays clean of `.env` and any `__pycache__`/cache
   directories — both are git-ignored.
