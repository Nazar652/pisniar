# pisniar

A Telegram bot that automates posting music tracks to a channel: resolve a
link's metadata, prepare a post, render a karaoke-style video fragment,
publish. See `AGENTS.md` for a terse, code-facing map of the stack and the
architectural boundaries that are easy to break without knowing them.

This repository is at **step 2**: an admin sends a link, the bot classifies
it, the worker resolves it across six external sources (song.link, Spotify,
Deezer, iTunes, `ytmusicapi`, MusicBrainz) with an ISRC-first matching
discipline, builds an HTML caption, and delivers a draft back with `/retry`
on failure. Rendering and channel publishing (steps 3-4) are next.

## Prerequisites

- Docker Desktop, running.
- `uv` (dependency/venv manager) and GNU Make on the host — both optional
  for pure Docker workflows, but needed for `make check` (lint/type/unit
  tests, which run faster on the host than inside a Windows bind mount).
  On Windows, the Makefile hard-requires `sh` on PATH (`SHELL := sh`) —
  Git for Windows (Git Bash) already provides this if `uv`/Make themselves
  are usable from the same shell.
- A Telegram bot token, two admin `user_id`s, and a chat id for alerts, plus
  a Spotify Web API client id/secret (Client Credentials flow) — see
  [Configuration](#configuration).

## Configuration

Copy `.env.example` to `.env` and fill in the values only you know: bot
token, admin ids, alert chat id, `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`.
`ITUNES_COUNTRY` defaults to `UA`; `MUSICBRAINZ_CONTACT` is optional — the
`musicbrainz` source stays disabled without it. **Never commit `.env`** — it
is git-ignored. `DATABASE_URL` / `REDIS_URL` are deliberately not in
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
development only: bind-mounting `src/`/`alembic/`/`tests/` into the
containers with a `watchfiles`-based hot-reload wrapper, publishing
`postgres`/`redis`/the bot's health API on `127.0.0.1` only (never on
`0.0.0.0`), and switching logs to a human-readable console format
(`LOG_FORMAT=console`, independent of `ENVIRONMENT`). To see the real JSON
output the docker/prod configuration produces (e.g. to check M11), run
without the override: `docker compose -f compose.yaml up -d --wait`.

Stop everything: `make down`. Stop and also drop the Postgres volume (a
clean-slate restart): `make clean`.

## Everyday commands

Run `make help` for the full list. The ones you will use most:

Plain `make check` still stops at the first failing step (sequential
prerequisites behave like `&&`). Run `make -k check` to see every failure —
lint, type, and test — from one invocation instead of fixing them one
round-trip at a time.

```
make check        # sync + lock-check + fmt-check + lint + typecheck + test (host, no Docker); use `make -k check` to see every failure at once
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
2. Send a real track link (e.g. an `open.spotify.com/track/...` URL) from
   an admin → a receipt with `post_id`; a draft with an HTML caption
   arrives back within seconds; `platform_links`/`isrc` land on the `track`
   row (`make psql`).
3. Send an album/artist/playlist link → a Ukrainian rejection reply, no
   `track`/`post` row created.
4. Force a `post` into `failed` (e.g. stop `postgres` mid-resolve) →
   `/retry <post_id>` re-dispatches it; a second draft is not duplicated.
5. `curl http://127.0.0.1:18080/healthz` → `200`, even if Postgres is down.
6. `curl http://127.0.0.1:18080/readyz` → `200` normally, `503` with a
   generic `{"status": "unavailable"}` body (no DSN, host, or stack trace)
   if Postgres or Redis is stopped.
7. Edit a `.py` file under `src/` while the stack is up → `bot`/`worker`
   restart on their own within a few seconds (`WATCHFILES_FORCE_POLLING=1`
   is required on Windows bind mounts; without it, hot reload silently does
   not work).
8. `UPDATE post SET status = 'nonsense';` via `make psql` → rejected by the
   `ck_post_status_valid` CHECK constraint.
9. `docker compose logs | grep <the real bot token>` → empty output. Do this
   by hand; never script a check that echoes the token itself.
10. `git status` stays clean of `.env` and any `__pycache__`/cache
    directories — both are git-ignored.
