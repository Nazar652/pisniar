# `sh` (Git Bash on Windows) must be on PATH: `help` needs `grep`/`sed`/`sort`, and
# pinning SHELL turns a confusing failure under cmd.exe into an explicit one.
SHELL := sh

COMPOSE := docker compose

.DEFAULT_GOAL := help

.PHONY: help sync lock-check fmt fmt-check lint typecheck test check \
	build up down ps logs restart clean test-int test-fonts migrate revision downgrade \
	alembic-check psql shell verify funnel-init funnel-up funnel-down dev-token

help: ## Show this list of targets
	@grep -h -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sort

## --- local (host, no Docker; DATABASE_URL/REDIS_URL not needed here) ---

sync: ## uv sync --frozen
	uv sync --frozen

lock-check: ## uv lock --check
	uv lock --check

fmt: ## ruff format (writes)
	uv run ruff format .

fmt-check: ## ruff format --check
	uv run ruff format --check .

lint: ## ruff check
	uv run ruff check .

typecheck: ## ty check, strict rules from pyproject.toml
	uv run ty check src tests

test: ## unit tests only, no DB
	uv run pytest -m "not integration" -q

# `check`'s prerequisites stop at the first failure (like `&&`); run
# `make -k check` (a make-invocation flag, not something `verify` sets for you) to see all of them.
check: sync lock-check fmt-check lint typecheck test ## sync + lock-check + fmt-check + lint + typecheck + test; use `make -k check` to see every failure in one go

## --- docker (compose.yaml + compose.override.yml pick up .env automatically) ---

build: ## docker compose build
	$(COMPOSE) build

up: ## docker compose up -d --wait
	$(COMPOSE) up -d --wait

down: ## docker compose down
	$(COMPOSE) down

ps: ## docker compose ps
	$(COMPOSE) ps

logs: ## docker compose logs -f
	$(COMPOSE) logs -f

restart: ## restart bot + worker (hot-reload sanity check)
	$(COMPOSE) restart bot worker

clean: ## docker compose down -v (also drops the postgres volume)
	$(COMPOSE) down -v

test-int: ## full test suite (unit + integration) inside the bot container
	$(COMPOSE) run --rm bot pytest -q -m "not fonts"

# Separate from `test-int`: the fonts live only in the worker image, and a missing font family
# falls back silently, so this is what stops a typo in `ass.FONT_FAMILY` reaching a video.
test-fonts: ## the ASS font family against `fc-list`, inside the worker container
	$(COMPOSE) run --rm worker pytest -q -m fonts

# `upgrade head` skips a revision that changed after it was applied, and says nothing;
# `alembic check` is the only thing that notices the missing schema.
migrate: ## alembic upgrade head inside the bot container, then alembic check
	$(COMPOSE) run --rm bot alembic upgrade head
	$(COMPOSE) run --rm bot alembic check

revision: ## autogenerate a migration: make revision m="add lyric table"
	$(COMPOSE) run --rm bot alembic revision --autogenerate -m "$(m)"

downgrade: ## alembic downgrade -1 inside the bot container
	$(COMPOSE) run --rm bot alembic downgrade -1

alembic-check: ## alembic check: model/migration drift
	$(COMPOSE) run --rm bot alembic check

psql: ## interactive psql in the postgres container
	$(COMPOSE) exec postgres psql -U pisniar -d pisniar

shell: ## shell in the bot container
	$(COMPOSE) exec bot sh

## --- Mini App entry (Tailscale Funnel; see `.env.tailscale.example`) ---

# External volume: `clean`/`verify` (`down -v`) cannot remove it, so the node and its URL survive.
funnel-init: ## create the external volume that holds the Tailscale node state (once)
	docker volume create pisniar-tailscale-state

funnel-up: ## start the Funnel (needs `make funnel-init` and `.env.tailscale` the first time)
	$(COMPOSE) --profile funnel up -d --wait tailscale

funnel-down: ## stop and remove the Funnel container; the node state stays on its volume
	$(COMPOSE) --profile funnel rm -sf tailscale

dev-token: ## print a session token for an admin, for a browser outside Telegram: make dev-token u=<user id>
	$(COMPOSE) exec webapp python -m pisniar.webapp.dev_token --user-id $(u)

verify: check build up ps alembic-check test-int ## check, build, up, ps, alembic-check, test-int, then down -v and re-up from a clean slate
	$(COMPOSE) down -v
	$(COMPOSE) up -d --wait
	$(COMPOSE) ps
