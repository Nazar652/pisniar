"""LRCLIB against the real `source_fetch` table: a documented 404 leaves a negative
row behind, a 503 leaves nothing. The unit tests prove the adapter's intent; this
proves what the database holds afterwards, since an outage is never "no lyrics".
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import SourceFetch
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.sources import lrclib

pytestmark = pytest.mark.integration

TITLE = "Покаяння"
TRACK_NOT_FOUND_BODY = {
    "message": "Failed to find specified track",
    "name": "TrackNotFound",
    "statusCode": 404,
}
SERVER_OVERLOADED_BODY = {
    "message": "The server is busy, please retry in a moment",
    "name": "ServerOverloaded",
    "statusCode": 503,
}


def unique_artist_names() -> list[str]:
    """a fresh pair per test: the cache row lives for an hour, so a constant name
    would make the second run of the day a cache hit."""
    return [f"Виконавець {uuid.uuid4().hex}", "0%Mercury"]


async def stored_row(session: AsyncSession, artist_names: list[str]) -> SourceFetch | None:
    request_key = build_request_key("lrclib:lyrics:", lrclib.join_artist_names(artist_names), TITLE)
    return await session.scalar(
        select(SourceFetch).where(SourceFetch.source == lrclib.SOURCE_NAME, SourceFetch.request_key == request_key)
    )


async def test_a_documented_404_is_cached_as_a_miss(
    source_cache: SourceCache,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lrclib.THROTTLE, "min_interval_seconds", 0.0)
    artist_names = unique_artist_names()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json=TRACK_NOT_FOUND_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await lrclib.fetch_lyrics(
            client,
            source_cache,
            artist_names=artist_names,
            title=TITLE,
            duration_ms=224_000,
        )

    assert result.outcome is lrclib.LyricsOutcome.ABSENT

    row = await stored_row(db_session, artist_names)
    assert row is not None
    assert row.payload == {"found": False}


async def test_a_503_leaves_no_negative_row_behind(
    source_cache: SourceCache,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mocked, since a live 503 cannot be reproduced on demand: the admin must hear
    «джерело не відповіло», and the next render must be free to ask again."""
    monkeypatch.setattr(lrclib.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(lrclib, "RETRY_BASE_DELAY_SECONDS", 0.0)
    artist_names = unique_artist_names()

    transport = httpx.MockTransport(lambda request: httpx.Response(503, json=SERVER_OVERLOADED_BODY))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(lrclib.LrclibApiError):
            await lrclib.fetch_lyrics(
                client,
                source_cache,
                artist_names=artist_names,
                title=TITLE,
                duration_ms=224_000,
            )

    assert await stored_row(db_session, artist_names) is None
