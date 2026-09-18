# GNU Make on Windows looks for sh.exe on PATH and uses it automatically if
# found (Git for Windows ships one); pinning SHELL here makes that explicit
# instead of leaving it to autodetection, and keeps every recipe below to a
# single plain command line with no bash-only syntax ([[ ]], arrays, source),
# so it still works unmodified if Make ever does fall back to cmd.exe.
SHELL := sh

COMPOSE := docker compose

.DEFAULT_GOAL := help

.PHONY: help sync lock fmt fmt-check lint typecheck test check \
	build up down ps logs restart clean test-int migrate revision downgrade \
	alembic-check psql shell verify

help: ## Show this list of targets
	@grep -h -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sort

## --- local (host, no Docker; DATABASE_URL/REDIS_URL not needed here) ---

sync: ## uv sync
	uv sync

lock: ## uv lock
	uv lock

fmt: ## ruff format (writes)
	uv run ruff format .

fmt-check: ## ruff format --check (V3)
	uv run ruff format --check .

lint: ## ruff check (V4)
	uv run ruff check .

typecheck: ## ty check, strict rules from pyproject.toml (V5)
	uv run ty check src tests

test: ## unit tests only, no DB (V6)
	uv run pytest -m "not integration" -q

check: fmt-check lint typecheck test ## fmt-check + lint + typecheck + test (V3-V6 in one)

## --- docker (compose.yaml + compose.override.yml pick up .env automatically) ---

build: ## docker compose build (V8)
	$(COMPOSE) build

up: ## docker compose up -d --wait (V9)
	$(COMPOSE) up -d --wait

down: ## docker compose down
	$(COMPOSE) down

ps: ## docker compose ps (V10)
	$(COMPOSE) ps

logs: ## docker compose logs -f
	$(COMPOSE) logs -f

restart: ## restart bot + worker (hot-reload sanity check)
	$(COMPOSE) restart bot worker

clean: ## docker compose down -v (also drops the postgres volume)
	$(COMPOSE) down -v

test-int: ## full test suite (unit + integration) inside the bot container (V12)
	$(COMPOSE) run --rm bot pytest -q

migrate: ## alembic upgrade head inside the bot container
	$(COMPOSE) run --rm bot alembic upgrade head

revision: ## autogenerate a migration: make revision m="add lyric table"
	$(COMPOSE) run --rm bot alembic revision --autogenerate -m "$(m)"

downgrade: ## alembic downgrade -1 inside the bot container
	$(COMPOSE) run --rm bot alembic downgrade -1

alembic-check: ## alembic check: model/migration drift (V11)
	$(COMPOSE) run --rm bot alembic check

psql: ## interactive psql in the postgres container
	$(COMPOSE) exec postgres psql -U pisniar -d pisniar

shell: ## shell in the bot container
	$(COMPOSE) exec bot sh

verify: check build up ps alembic-check test-int ## full V1-V12 in one entrypoint, then V13 (down -v + re-up from a clean slate)
	$(COMPOSE) down -v
	$(COMPOSE) up -d --wait
	$(COMPOSE) ps
