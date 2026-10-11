"""`ytmusic`: search via `asyncio.to_thread` (mocked, no real `ytmusicapi`
network call), `limit=5`, and candidate field mapping."""

from __future__ import annotations

import pytest
import requests

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.lyrics_lookup import LYRICS_LOOKUP_TIMEOUT_SECONDS
from pisniar.resolve.sources import ytmusic

SEARCH_RESULTS: list[dict[str, object]] = [
    {
        "videoId": "n4bPzdIs_nc",
        "title": "Покаяння",
        "duration_seconds": 225,
        "artists": [{"name": "Burned Time Machine", "id": "a1"}, {"name": "0%Mercury", "id": "a2"}],
    }
]


async def test_search_by_name_returns_candidates(monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache) -> None:
    monkeypatch.setattr(ytmusic, "search_sync", lambda query: SEARCH_RESULTS)

    candidates = await ytmusic.search_by_name(fake_cache, "Burned Time Machine", "Покаяння")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.platform == "youtubeMusic"
    assert candidate.url == "https://music.youtube.com/watch?v=n4bPzdIs_nc"
    assert candidate.artist_name == "Burned Time Machine, 0%Mercury"
    assert candidate.title == "Покаяння"
    assert candidate.duration_ms == 225000


async def test_search_by_name_caps_at_five(monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache) -> None:
    many_results = [
        {
            "videoId": f"id{index}",
            "title": "Song",
            "duration_seconds": 200,
            "artists": [{"name": "A"}],
        }
        for index in range(20)
    ]
    monkeypatch.setattr(ytmusic, "search_sync", lambda query: many_results)

    candidates = await ytmusic.search_by_name(fake_cache, "A", "Song")

    assert ytmusic.SEARCH_LIMIT == 5
    assert len(candidates) == 5


async def test_search_by_name_returns_empty_list_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache
) -> None:
    monkeypatch.setattr(ytmusic, "search_sync", lambda query: [])

    candidates = await ytmusic.search_by_name(fake_cache, "Nobody", "Nothing")

    assert candidates == []


def test_the_request_timeout_fits_two_calls_inside_the_lookup_limit() -> None:
    assert 2 * ytmusic.REQUEST_TIMEOUT_SECONDS <= LYRICS_LOOKUP_TIMEOUT_SECONDS


@pytest.mark.parametrize(("given", "expected"), [(None, 9.0), (3, 3)])
def test_every_request_of_the_session_has_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
    given: float | None,
    expected: float,
) -> None:
    sent: list[dict[str, object]] = []

    def record(self: requests.Session, request: object, **kwargs: object) -> str:
        sent.append(kwargs)
        return "response"

    monkeypatch.setattr(requests.Session, "send", record)
    kwargs: dict[str, object] = {} if given is None else {"timeout": given}

    ytmusic.TimeoutSession().send(requests.Request("GET", "https://example.test").prepare(), **kwargs)

    assert sent == [{"timeout": expected}]


def test_the_client_is_built_on_the_timeout_session(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []

    def fake_client(*, requests_session: object) -> object:
        built.append(requests_session)
        return object()

    monkeypatch.setattr(ytmusic, "cached_client", None)
    monkeypatch.setattr(ytmusic, "YTMusic", fake_client)

    ytmusic.get_client()

    assert len(built) == 1 and isinstance(built[0], ytmusic.TimeoutSession)
