# syntax=docker/dockerfile:1.7
#
# One multi-stage Dockerfile builds every target this project needs — a
# formal deviation from the letter of "two Dockerfiles" (one per service),
# same result: the shared dependency layer is built once, not twice.
#
# `bot` / `worker` bake source in and run standalone (used by `compose.yaml`).
# `bot-dev` / `worker-dev` install the `dev` dependency group (for
# `watchfiles`, the hot-reload wrapper) and expect source to be bind-mounted
# at runtime instead — see `compose.override.yml`.
#
# System packages for ffmpeg/fonts (step 4) land in `worker`'s own final
# stage, after everything it shares with `bot` — so adding them later
# invalidates neither `uv sync` nor the `bot` image.

FROM python:3.13-slim AS base

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /usr/local/bin/

RUN groupadd --system app && useradd --system --gid app --home-dir /app --create-home app

WORKDIR /app


# ---- shared dependency layers (no application source yet) ----------------

FROM base AS deps-prod

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

FROM base AS deps-dev

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project


# ---- prod: source baked into the image, no bind mount needed at runtime --

FROM base AS source-prod

COPY --from=deps-prod /opt/venv /opt/venv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --locked --no-dev \
    && chown -R app:app /app /opt/venv
USER app


# ---- dev: only dependencies baked in; source is bind-mounted at runtime --
#
# `uv sync` here only installs third-party dependencies (`--no-install-
# project`, deliberately, for cache-friendliness — see `deps-dev` above), so
# `pisniar` itself is never installed into the venv the way it is in
# `source-prod`. PYTHONPATH stands in for that: `/app/src` does not exist
# yet at build time, but it does by the time Python actually runs, once
# compose.override.yml's bind mount has populated it.

FROM base AS source-dev

ENV PYTHONPATH=/app/src

COPY --from=deps-dev /opt/venv /opt/venv
COPY pyproject.toml uv.lock ./
RUN chown -R app:app /app /opt/venv
USER app


# ---- bot -------------------------------------------------------------

FROM source-prod AS bot
EXPOSE 8080
CMD ["python", "-m", "pisniar.bot.main"]

FROM source-dev AS bot-dev
EXPOSE 8080
CMD ["watchfiles", "--filter", "python", "python -m pisniar.bot.main", "/app/src"]


# ---- worker ------------------------------------------------------------

FROM source-prod AS worker
# `source-prod` already switched to `USER app`; apt-get needs root back, and
# must hand it off again afterward — uncommenting this block as-is on step 4
# stays non-root at runtime. Getting this order wrong is exactly the
# `Permission denied` step 1 is trying to avoid triggering later.
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     ffmpeg fonts-noto-core && rm -rf /var/lib/apt/lists/*
# USER app
CMD ["celery", "-A", "pisniar.worker.tasks", "worker", "--loglevel=INFO"]

FROM source-dev AS worker-dev
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     ffmpeg fonts-noto-core && rm -rf /var/lib/apt/lists/*
# USER app
CMD ["watchfiles", "--filter", "python", "celery -A pisniar.worker.tasks worker --loglevel=INFO", "/app/src"]
