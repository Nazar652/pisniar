"""Zeroes every source adapter's module-level `Throttler` for the test
session, and provides the database-free `SourceCache` stand-in every
adapter test resolves through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import (
    deezer,
    genius,
    itunes,
    lrclib,
    musicbrainz,
    songlink,
    spotify,
    ytmusic,
)


@pytest.fixture(autouse=True)
def zero_throttle_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(songlink.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(deezer.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(itunes.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(spotify.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(ytmusic.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(genius.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(musicbrainz.THROTTLE, "min_interval_seconds", 0.0)

    # the backoff too, not only the throttle: `lrclib` retries the "did not
    # answer" class three times, and the sleeps are the test suite's time.
    monkeypatch.setattr(lrclib, "RETRY_BASE_DELAY_SECONDS", 0.0)


@pytest.fixture
def fake_cache() -> SourceCache:
    """keeps adapter tests off the database while the request keys stay readable
    in `fake_cache.get_or_fetch.await_args_list`."""
    cache = AsyncMock(spec=SourceCache)

    async def call_through(
        source: str, request_key: str, fetch: Callable[[], Awaitable[dict[str, object]]]
    ) -> dict[str, object]:
        return await fetch()

    cache.get_or_fetch.side_effect = call_through
    return cache
