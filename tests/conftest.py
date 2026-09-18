"""Shared test setup.

Provides deterministic fake values for every required `Settings` field
before any `pisniar.*` module is imported — several modules (notably
`pisniar.worker.tasks`) call `get_settings()` at import time. On the host,
`os.environ` (set here) takes precedence over `.env` in pydantic-settings'
own lookup order, so these fakes shadow whatever real values are in the
repo's `.env` and tests never depend on — or leak — real secrets. Inside
Docker, `.env` is already loaded into the real process environment by
`env_file:` in compose.yaml, so `setdefault` here is a no-op and the actual
configured environment is used instead, which is what `V12` needs.
"""

import os

os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("BOT_TOKEN", "000000000:test-token-not-a-real-secret")
os.environ.setdefault("ADMIN_IDS", "111111111,222222222")
os.environ.setdefault("ALERT_CHAT_ID", "111111111")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar")
os.environ.setdefault("REDIS_URL", "redis://redis:6379/0")
os.environ.setdefault("HTTP_HOST", "0.0.0.0")
os.environ.setdefault("HTTP_PORT", "8080")
