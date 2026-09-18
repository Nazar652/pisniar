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

import logging
import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("BOT_TOKEN", "000000000:test-token-not-a-real-secret")
os.environ.setdefault("ADMIN_IDS", "111111111,222222222")
os.environ.setdefault("ALERT_CHAT_ID", "111111111")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar")
os.environ.setdefault("REDIS_URL", "redis://redis:6379/0")
os.environ.setdefault("HTTP_HOST", "0.0.0.0")
os.environ.setdefault("HTTP_PORT", "8080")


@pytest.fixture
def preserve_root_logging_state() -> Iterator[None]:
    """`configure_logging()` replaces `logging.getLogger().handlers`
    wholesale and does not restore whatever was there before — fine in a
    real process (it runs once, at startup), but a test that calls it must
    put the root logger back the way it found it, or every test that runs
    afterward in the same process inherits whatever handler/level the test
    left behind.
    """
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        yield
    finally:
        root_logger.handlers = original_handlers
        root_logger.setLevel(original_level)
