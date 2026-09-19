"""`spotify`: in-memory token caching (D-13), ISRC/name search, `/v1/tracks/{id}`
parsing, and 429/Retry-After backoff — via `httpx.MockTransport`."""

from __future__ import annotations

import httpx
import pytest

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import spotify

TOKEN_BODY = {"access_token": "token-abc", "token_type": "Bearer", "expires_in": 3600}
SEARCH_ISRC_BODY = {
    "tracks": {
        "items": [
            {
                "id": "70LcF31zb1H0PyJoS1Sx1r",
                "name": "Creep",
                "duration_ms": 238640,
                "external_ids": {"isrc": "GBAYE9200070"},
                "external_urls": {
                    "spotify": "https://open.spotify.com/track/70LcF31zb1H0PyJoS1Sx1r"
                },
                "artists": [{"id": "a1", "name": "Radiohead"}],
                "album": {
                    "id": "alb1",
                    "name": "Pablo Honey",
                    "album_type": "album",
                    "release_date": "1993-02-22",
                },
            }
        ]
    }
}
SEARCH_EMPTY_BODY = {"tracks": {"items": []}}
TRACK_BODY = SEARCH_ISRC_BODY["tracks"]["items"][0]


@pytest.fixture(autouse=True)
def reset_token_cache() -> None:
    spotify._cached_access_token = None
    spotify._cached_token_expires_at = 0.0


async def test_search_by_isrc_returns_found_result(fake_cache: SourceCache) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json=SEARCH_ISRC_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await spotify.search_by_isrc(
            client, fake_cache, "GBAYE9200070", "client-id", "client-secret"
        )

    assert result.found is True
    assert result.isrc == "GBAYE9200070"
    assert result.artist_names == ["Radiohead"]
    assert result.year == 1993
    assert result.release_type == "album"


async def test_search_by_isrc_returns_not_found_on_empty_items(fake_cache: SourceCache) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json=SEARCH_EMPTY_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await spotify.search_by_isrc(
            client, fake_cache, "NOPE00000000", "client-id", "client-secret"
        )

    assert result.found is False


async def test_get_track_extracts_year_album_and_isrc(fake_cache: SourceCache) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json=TRACK_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await spotify.get_track(
            client, fake_cache, "70LcF31zb1H0PyJoS1Sx1r", "client-id", "client-secret"
        )

    assert result.year == 1993
    assert result.release_title == "Pablo Honey"
    assert result.isrc == "GBAYE9200070"


async def test_search_by_name_caps_results_at_five(fake_cache: SourceCache) -> None:
    many_items = [
        {
            "id": f"id{i}",
            "name": "Song",
            "duration_ms": 200000,
            "external_ids": {},
            "external_urls": {"spotify": "https://open.spotify.com/track/x"},
            "artists": [{"id": "a", "name": "Artist"}],
            "album": {"id": "alb", "name": "Album", "album_type": "single", "release_date": "2020"},
        }
        for i in range(20)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json={"tracks": {"items": many_items}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        candidates = await spotify.search_by_name(
            client, fake_cache, "Artist", "Song", "client-id", "client-secret"
        )

    assert spotify.SEARCH_LIMIT == 5
    assert len(candidates) == 5


async def test_access_token_is_reused_across_calls(fake_cache: SourceCache) -> None:
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if "accounts.spotify.com" in str(request.url):
            token_requests += 1
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json=SEARCH_ISRC_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await spotify.search_by_isrc(
            client, fake_cache, "GBAYE9200070", "client-id", "client-secret"
        )
        await spotify.search_by_isrc(
            client, fake_cache, "GBAYE9200071", "client-id", "client-secret"
        )

    assert token_requests == 1


async def test_request_retries_after_429_then_succeeds(fake_cache: SourceCache) -> None:
    search_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_attempts
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_BODY)
        search_attempts += 1
        if search_attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=SEARCH_ISRC_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await spotify.search_by_isrc(
            client, fake_cache, "GBAYE9200070", "client-id", "client-secret"
        )

    assert search_attempts == 2
    assert result.found is True
