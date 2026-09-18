"""Shared test setup: deterministic fake `Settings` values (some modules call
`get_settings()` at import time). `os.environ` outranks `.env`, so these
never leak real secrets on the host; inside Docker `.env` is already real
process env, so `setdefault` is a no-op there — which is what V12 needs.
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
    """`configure_logging()` replaces root handlers wholesale; a test
    calling it must restore them or later tests inherit its state.
    """
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        yield
    finally:
        root_logger.handlers = original_handlers
        root_logger.setLevel(original_level)
