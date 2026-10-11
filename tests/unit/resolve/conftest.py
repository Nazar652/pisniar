"""Zeroes every source adapter's module-level `Throttler` for the test session, and provides the database-free
`SourceCache` stand-in every adapter test resolves through."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

import pisniar.resolve.sources
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.http import Throttler
from pisniar.resolve.sources import lrclib


@pytest.fixture(autouse=True)
def zero_throttle_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_info in pkgutil.iter_modules(pisniar.resolve.sources.__path__):
        module = importlib.import_module(f"pisniar.resolve.sources.{module_info.name}")
        throttle = getattr(module, "THROTTLE", None)
        if isinstance(throttle, Throttler):
            monkeypatch.setattr(throttle, "min_interval_seconds", 0.0)

    # the backoff too, not only the throttle: `lrclib` retries the "did not answer" class three times, and the sleeps
    # are the test suite's time.
    monkeypatch.setattr(lrclib, "RETRY_BASE_DELAY_SECONDS", 0.0)


@pytest.fixture
def fake_cache() -> SourceCache:
    """keeps adapter tests off the database while the request keys stay readable in
    `fake_cache.get_or_fetch.await_args_list`."""
    cache = AsyncMock(spec=SourceCache)

    async def call_through(
        source: str,
        request_key: str,
        fetch: Callable[[], Awaitable[dict[str, object]]],
    ) -> dict[str, object]:
        return await fetch()

    cache.get_or_fetch.side_effect = call_through
    return cache
