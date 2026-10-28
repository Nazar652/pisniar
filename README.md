# pisniar

A Telegram bot that automates posting music tracks to a channel: resolve a
link's metadata, prepare a post, render a vertical video fragment, publish.
See `AGENTS.md` for a terse, code-facing map of the stack and the
architectural boundaries that are easy to break without knowing them.

An admin sends a link, the bot classifies
it, the worker resolves it across six external sources (song.link, Spotify,
Deezer, iTunes, `ytmusicapi`, MusicBrainz) with an ISRC-first matching
discipline, builds an HTML caption, and delivers a draft back with `/retry`
on failure. The draft carries **Зробити відео** and, in a private chat, the
Mini App's **Редагувати**; nothing in the chat publishes the cover alone.

**Зробити відео** starts the media chain: the worker downloads the
track's audio from YouTube Music, takes a 45-second fragment from the start,
renders a 720x896 (4:5) video, and sends it
back as a **preview with its own publish button**. Publishing from there
puts that exact file, with that exact caption, into the channel as a video.
The preview is not a convenience — a published post cannot be revoked, so
it is the one place a wrong fragment can still be caught.

A post without a video can still go out with the cover and that exact caption:
the Mini App's **Опублікувати без відео** (with a confirmation, once the caption
is saved) publishes it and records who approved it. It needs `WEBAPP_PUBLIC_URL`
and a private chat; without them there is no cover-only path.

The video is a **turntable**: the cover is the label of a record that turns at
33⅓ rpm, the title and artists glow on a display, and the track's **timed
lyrics** show on a glass screen below, the previous, current and next line
synced to the audio (an instrumental gap shows a note, ♪). The still layers are
drawn with Pillow and only the record is composed per frame, so a 120-second
fragment renders in under a minute on two shared vCPUs. Lyrics come from
YouTube Music and [LRCLIB](https://lrclib.net) and are stored per track, so a
second post of the same recording reuses them without asking again; plain text
without times is fetched afterwards (Genius, YouTube Music, LRCLIB) and kept for
the editor. When there is no usable timed text (none found, none with
timecodes, an instrumental, no answer from the sources, or no line inside the
fragment), the video is rendered without it, and a short notice under the
preview says why.

In the Mini App, **Текст** → **Редагувати текст** opens the lyrics editor for a
post whose audio is downloaded (it has a preview, or its audio was prepared). Play the audio (0.5×, 0.75×
or 1×) and tap **Тап** (or Space; Backspace undoes) on each line as it is sung;
**♪** and **Програш** mark an instrumental gap at the playhead, a line's time nudges
by 0.1 or 1 s, and a pasted text becomes untimed lines to tap through.
**Знайти синхронізацію** asks YouTube Music, then LRCLIB, for timed text, and
**Знайти просто текст** asks the source you pick (Genius by default) for the
words alone; either only proposes, and replaces the editor's text once you
confirm. **Зберегти** stores the text (it is never overwritten by an automatic
lookup afterwards), **Зберегти й зробити відео** also renders with it. The
editors of the fields, the lyrics and the fragment warn before the back button
discards unsaved work. The tap delay compensation (`TAP_LATENCY_MS` in
`frontend/src/lyrics/timing.ts`) is 0 until measured on a device.

A **Mini App** (Vue 3, served by the `webapp` container) edits a post after the
fact. It opens from the **Редагувати** button under the draft, the preview and
the publication confirmation in a private chat, from `/edit [post_id]` (one
post, or a list without an id) and from the **Пости** menu button. It edits
the title, the artists and their hashtags, the languages and genres, the year,
the release type and title, the album link and the platform links, and shows
the caption that results before anything is sent. **Зберегти** saves and
applies the new caption to the draft or preview message; for a published post
the save stops there and **Застосувати в каналі** is a separate step with a
confirmation. A published post whose channel message was deleted by hand can be
archived from the same screen. The buttons exist only when `WEBAPP_PUBLIC_URL`
is set; without it the bot works exactly as before.

On a draft, **Фрагмент** → **Обрати фрагмент** first prepares the audio (up to
a minute; the draft's buttons go), then opens the editor below, and the first
preview is rendered with the chosen part. **Скасувати підготовку** under the
editor gives the audio up: the post is a draft again, its buttons come back and
the cover can be published without a video. Once a post has a video preview, the **Фрагмент** section of the same editor
shows the track's waveform with the current fragment as a region. Drag it or
its edges (or step them by ±1 s / ±0.1 s), listen with **Програти фрагмент**,
and **Зробити відео з цим фрагментом** renders the post again with those bounds
(up to 2 minutes, no minimum). A new preview arrives in the chat and the old
one loses its buttons: only the newest preview can be published. The waveform
is computed by the worker after each download; `make peaks` writes it for audio
that was downloaded before (a post with a preview is never downloaded again).

## Prerequisites

- Docker Desktop, running.
- `uv` (dependency/venv manager) and GNU Make on the host — both optional
  for pure Docker workflows, but needed for `make check` (lint/type/unit
  tests, which run faster on the host than inside a Windows bind mount).
  On Windows, the Makefile hard-requires `sh` on PATH (`SHELL := sh`) —
  Git for Windows (Git Bash) already provides this if `uv`/Make themselves
  are usable from the same shell.
- Node 24.16.0 on the host (`frontend/.node-version`), also for `make check`,
  which runs the frontend checks. The Docker images build the frontend with
  the same pinned version.
- A Tailscale account, only if you want the Mini App: it is reached through a
  Tailscale Funnel (see [Mini App and Tailscale Funnel](#mini-app-and-tailscale-funnel)).
- A Telegram bot token, two admin `user_id`s, a chat id for alerts and the
  numeric chat id of the channel to publish into, plus a Spotify Web API
  client id/secret (Client Credentials flow) — see
  [Configuration](#configuration).
- The bot added to that channel **as an administrator with "post messages"
  enabled**. Without it every publication fails the post with a Ukrainian
  explanation instead of a raw `400`/`403`.

## Configuration

Copy `.env.example` to `.env` and fill in the values only you know: bot
token, admin ids, alert chat id, `CHANNEL_ID`,
`SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`, and a generated
`REDIS_PASSWORD`. `ITUNES_COUNTRY` defaults to `UA`;
`MUSICBRAINZ_CONTACT` is optional — the `musicbrainz` source stays disabled
without it. **Never commit `.env`** — it is git-ignored. `DATABASE_URL` /
`REDIS_URL` are deliberately not in `.env.example`: they are the in-network
DNS names of the `postgres` / `redis` compose services, fixed in
`compose.yaml`, not something you fill in.

`CHANNEL_ID` has no default either: it is the numeric chat id of the
channel, not an `@username` (a username can be pointed at another chat, an id
cannot). It is the only source of the publish target — the channel link
printed in every caption lives in code (`pisniar/channel.py`) and nothing
derives one from the other, so both are cross-checked at runtime. Two
warnings, never refusals, come out of that: `alert_chat_is_the_publish_channel`
when `CHANNEL_ID == ALERT_CHAT_ID` (fine while you are testing against your
own channel, a leak of error texts and other tracks' URLs to subscribers once
it is the real one) and `publish_channel_is_not_the_caption_channel` when the
configured channel is not the one captions link to.

`REDIS_PASSWORD` has no default and no fallback: `compose.yaml` starts Redis
with `--requirepass` and builds `REDIS_URL` as
`redis://:<password>@redis:6379/0`, so `docker compose` refuses to start
until it is set. It is required because `compose.override.yml` publishes the
broker on `127.0.0.1:56379`, and anything able to enqueue a task there makes
the worker act without ever passing `AdminAllowlistMiddleware`. The
container's own healthcheck authenticates through `REDISCLI_AUTH` rather
than `redis-cli -a`, which would put the password in the process list. After
changing any `.env` value, recreate the containers
(`docker compose up -d --force-recreate`) — `restart` reuses the
environment the containers were created with.

`WEBAPP_PUBLIC_URL` is the public origin of the Mini App: a bare
`https://host` with no path and no port, normally the Funnel URL. Unset (or
empty) keeps every Mini App button off. Three optional windows can be set
(`WEBAPP_AUTH_MAX_AGE_SECONDS`, `WEBAPP_AUTH_MAX_FUTURE_SECONDS`,
`WEBAPP_SESSION_TTL_SECONDS`); `.env.example` lists their defaults.
Removing an admin means editing `ADMIN_IDS` and recreating `bot`, `worker` and
`webapp`; a signed audio URL stays valid for a current admin for its TTL
(`WEBAPP_MEDIA_URL_TTL_SECONDS`, 900 s).
`.env.tailscale` (copy `.env.tailscale.example`) holds the one-time Tailscale
auth key; only the `tailscale` service reads it, and it is git-ignored too.

## Running

```
make build   # docker compose build
make up      # docker compose up -d --wait — brings up postgres, redis,
              # runs the one-shot `migrate` service, then starts bot, worker,
              # worker-render and webapp
make ps      # confirm everything is healthy
```

`worker` serves the default queue; `worker-render` serves the `render` queue
alone, one render at a time, so a minutes-long ffmpeg never holds up resolving
or downloading. They share one image, so after pulling a change that touches the
queues run `docker compose up -d --build worker worker-render` (`make up` does not
build), with the default queue drained first. The render time limits
(`RENDER_TASK_*`, 600/660 s by default) depend on the host: `make bench-render`
renders a 45 s and a 120 s fragment in an isolated container (no `.env`, no
volume, no network) and prints the values to put in `.env`.

`compose.override.yml` is picked up automatically and adds, for local
development only: bind-mounting `src/`/`alembic/`/`tests/` into the
containers with a `watchfiles`-based hot-reload wrapper, publishing
`postgres`/`redis`/the bot's health API on `127.0.0.1` only (never on
`0.0.0.0`), and switching logs to a human-readable console format
(`LOG_FORMAT=console`, independent of `ENVIRONMENT`). To see the real JSON
output the docker/prod configuration produces (e.g. to confirm every line is
valid JSON and none carries the bot token), run
without the override: `docker compose -f compose.yaml up -d --wait`.

Stop everything: `make down`. Stop and also drop the Postgres volume (a
clean-slate restart): `make clean`.

## Mini App and Tailscale Funnel

Telegram opens a Mini App only from a public `https` address. The `tailscale`
compose service publishes one through a Tailscale Funnel: it proxies to
`webapp:8081` and nothing else, and the Funnel is the only thing exposed to the
internet. The console wording below may differ slightly. `<tailnet>` stands for
your tailnet name.

1. Create a Tailscale account (the Personal plan is enough).
2. In the admin console, open **DNS** and turn on **MagicDNS** and **HTTPS
   Certificates**.
3. In **Access controls** (the policy file) add:
   ```
   "tagOwners": {"tag:pisniar": ["autogroup:admin"]},
   "nodeAttrs": [{"target": ["tag:pisniar"], "attr": ["funnel"]}]
   ```
4. In **Settings → Keys** generate an auth key with the tag `tag:pisniar`,
   pre-approved, **not** reusable and **not** ephemeral. Put it in
   `.env.tailscale` as `TS_AUTHKEY=...` (from `.env.tailscale.example`) and
   nowhere else.
5. `make funnel-init` creates the external volume `pisniar-tailscale-state`
   (once), then `make funnel-up` starts the service.
6. In **Machines** check that a node named `pisniar` appeared with the tag, and
   that key expiry is off for it. Its address is
   `https://pisniar.<tailnet>.ts.net`.
7. Put that address in `.env` as `WEBAPP_PUBLIC_URL` and recreate the services
   that read it: `docker compose up -d --force-recreate bot worker webapp`.

The state volume is external on purpose: `make clean` and `make verify` (both
`down -v`) cannot delete it, so the node keeps its identity and its URL. If the
volume is lost, the next start registers a new node under a new URL, needs a new
auth key, and every button already sent keeps pointing at the old address.
`make funnel-down` removes the container and leaves the state in place; the bot
and the worker do not depend on it.

### Frontend workflow

The sources are in `frontend/`. With `compose.override.yml`, the running
`webapp` serves the host's `frontend/dist`, read on every request.

- `make frontend-build` builds into `frontend/dist`.
- `make frontend-watch` rebuilds on every change: edit, then reopen the Mini App
  in Telegram.
- After any change to the API, run `make api-types`. `frontend/openapi.json` and
  `frontend/src/api/schema.ts` are generated and never edited by hand; `make
  check` fails when they drift.
- To work in a browser outside Telegram, start the Vite dev server in
  `frontend/` (`npm run dev`, `http://127.0.0.1:5173`, `/api` proxied to the
  published `webapp` on `127.0.0.1:18081`). The dev build installs a stand-in for
  the Telegram SDK and reads a session token from `sessionStorage` under the key
  `pisniar.devToken`. Print one with `make dev-token u=<your user id>`, set it in
  the browser console with `sessionStorage.setItem('pisniar.devToken', '<token>')`
  and reload. The token is a secret, valid for an hour by default: do not paste it
  anywhere else, and it never reaches a production build.

## Everyday commands

Run `make help` for the full list. The ones you will use most. Every test is
hermetic: a connect, name lookup or task send beyond loopback (and, in the
integration layer, the database) fails the test.

Plain `make check` still stops at the first failing step (sequential
prerequisites behave like `&&`). Run `make -k check` to see every failure —
lint, type, and test — from one invocation instead of fixing them one
round-trip at a time.

```
make check        # sync + lock-check + fmt-check + lint + typecheck + test, then the frontend checks (host, no Docker); use `make -k check` to see every failure at once
make test          # unit tests, no database, in parallel (pytest-xdist, 4 workers)
make test-int      # integration tests and the Linux-only bench test inside the bot container
make test-fonts    # the subtitle font and its glyph-width table, inside the worker container
make test-media    # the turntable render with the real ffmpeg, inside the worker container
make bench-render  # time a 45 s and a 120 s render in an isolated container, print the .env limits
make migrate       # alembic upgrade head
make revision m="add lyric table"   # alembic revision --autogenerate
make alembic-check # confirm models and migrations agree
make api-types     # regenerate the frontend API types after an API change
make frontend-build # build the Mini App into frontend/dist
make frontend-watch # rebuild it on every change
make dev-token u=<user id>   # session token for a browser outside Telegram (a secret)
make peaks         # write missing or outdated waveform files for stored audio, in the worker
make funnel-init   # create the Tailscale state volume (once)
make funnel-up     # start the Funnel
make funnel-down   # stop it; the node and its URL stay
make shell         # shell in the bot container
make psql          # interactive psql in the postgres container
make logs          # follow logs from every service
```

`make verify` runs the full local + Docker verification chain in one go,
ending with a clean
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
   by hand; never script a check that echoes the token itself. The same
   holds for `REDIS_PASSWORD` — `redaction_secrets()` covers it.
10. Connect to `127.0.0.1:56379` with no password → `NOAUTH Authentication
    required.`
11. `git status` stays clean of `.env` and any `__pycache__`/cache
    directories — both are git-ignored.
12. A new draft carries **Зробити відео** and no publish button. In its editor
    (**Редагувати**), save the caption and tap **Опублікувати без відео**, then
    confirm → the draft's buttons disappear, the post appears in `CHANNEL_ID` as
    a cover with the same caption, and a confirmation with a link to it comes
    back. Do this against a **test** channel first.
13. An older draft that still shows **Опублікувати в канал** → the tap is
    refused with a pointer to the editor and nothing changes. Repeating
    **Опублікувати без відео** on the published post → refused, and no second
    post in the channel.
14. Remove the bot's "post messages" right and publish again → the post goes
    `failed` with a Ukrainian explanation, no traceback in the chat, and
    `channel_message_id` stays `NULL` (`make psql`).
15. `/retry <post_id>` on that failed post → it re-publishes, not re-resolves
    ("Повторюю публікацію поста N").
16. Tapping the button from a non-admin account → nothing at all, plus a
    `update_rejected_non_admin` line from `bot`.
17. With `WEBAPP_PUBLIC_URL` set, a new draft in a private chat carries
    **Редагувати**; it opens the editor. Change the title and tap **Зберегти** →
    «Збережено й застосовано.» and the draft's caption changes.
18. Open the same post in two tabs and save in both → the second save is
    refused and the post is reloaded.
19. Edit a published post → the save does not touch the channel; **Застосувати в
    каналі** asks for confirmation first. Do this against a **test** channel.
20. Delete that channel message by hand, then apply → the archive button appears
    and archives the post.
21. `make funnel-down` → the bot, the worker and publication keep working; only
    the Mini App is unreachable.
22. `docker compose logs | grep` for a real `initData`, session token or
    `TS_AUTHKEY` → empty output. Do this by hand; never script a check that
    echoes the secret.
23. On a post with a video preview, open **Фрагмент** → **Обрати фрагмент** → a
    waveform with one region; Telegram Desktop devtools show no CSP violation and
    the audio arrives as Range requests.
24. Drag the region to another part, **Програти фрагмент** plays only it; tap
    **Зробити відео з цим фрагментом** → a new preview with that part (and its
    lyric lines) arrives, the old preview loses its buttons, and publishing from
    the new one sends the new video. Do this against a **test** channel.
25. A publish tap under an old preview → refused as outdated, nothing published.
26. A fragment longer than 2 minutes cannot be chosen, or is refused with a
    sentence that names the numbers.
27. Two tabs on the same fragment: the second submit reloads with a sentence
    instead of starting a second render.
28. On a draft that was never rendered, **Фрагмент** → **Обрати фрагмент** →
    «Готуємо аудіо…» → the editor opens; choose 1:10–1:50 → the first preview is
    exactly that part. In the chat, the publish and render taps of such a post
    answer that the audio is ready, and `/retry` changes nothing.
29. With the audio ready, **Скасувати підготовку** → «Підготовку скасовано.
    Пост знову чернетка.», the draft in the chat gets **Зробити відео** and
    **Редагувати** back, the YouTube Music link is editable again, and preparing
    again is quick (the file is on the volume). Then **Опублікувати без відео**
    works (test channel).
30. A track without a YouTube Music link: **Обрати фрагмент** is refused with a
    sentence; after a failed download the post is still publishable without a video.
31. A new video shows the turntable: the cover as the record's label, the record
    turning, the tonearm, the title and artists on the display and the lyrics on
    the glass screen, none of it outside its frame (check a long title and a long
    line). An instrumental gap shows ♪, not a box.
32. **Текст** → **Редагувати текст** on a post with prepared audio: play, tap
    each line, **Зберегти й зробити відео** → the new preview has the lyrics at
    the tapped times. Tapping at a time not after the previous line is refused
    with a sentence.
33. **Знайти синхронізацію** on a track YouTube Music or LRCLIB knows → a proposal
    that replaces the text after a confirmation; **Знайти просто текст** with each
    of the three sources → untimed lines, or a sentence when the source has none.
34. Edit the text, then tap the Telegram back button → one question; confirm
    leaves, cancel stays. The same on the fields form and the fragment editor.
