"""`songlink.fetch()`: the cache-integrated wrapper. A second call for the
same URL must not touch the (mocked) network again."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import songlink

pytestmark = pytest.mark.integration

FIXTURE_HTML = (
    Path(__file__).resolve().parents[1] / "unit" / "resolve" / "fixtures" / "songlink_track.html"
).read_text(encoding="utf-8")


async def test_fetch_caches_result_and_skips_network_on_second_call(
    source_cache: SourceCache,
) -> None:
    request_count = 0
    # unique per run: `source_cache` commits for real now, so a fixed key
    # would still be a fresh hit from the previous run an hour ago.
    track_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, text=FIXTURE_HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await songlink.fetch(client, source_cache, track_url)
        second = await songlink.fetch(client, source_cache, track_url)

    assert request_count == 1
    assert first.isrc == second.isrc == "GBARL9300135"
