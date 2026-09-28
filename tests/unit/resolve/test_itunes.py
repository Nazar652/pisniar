"""`itunes`: `country` is always sent explicitly, throttle is >=3s, and
results cap at 5 — via `httpx.MockTransport`, trimmed real payloads."""

from __future__ import annotations

import httpx

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import itunes

SEARCH_BODY: dict[str, object] = {
    "resultCount": 1,
    "results": [
        {
            "artistName": "Krovna",
            "trackName": "Berserker",
            "trackTimeMillis": 262105,
            "trackViewUrl": "https://music.apple.com/ua/song/berserker/1234567890",
        }
    ],
}


def test_throttle_interval_is_at_least_three_seconds() -> None:
    assert itunes.MIN_INTERVAL_SECONDS >= 3.0


async def test_search_by_name_sends_explicit_country(fake_cache: SourceCache) -> None:
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json=SEARCH_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await itunes.search_by_name(client, fake_cache, "Krovna", "Berserker", country="UA")

    assert captured_params["country"] == "UA"


def test_candidate_from_result_maps_fields() -> None:
    results = SEARCH_BODY["results"]
    assert isinstance(results, list)
    candidate = itunes._candidate_from_result(results[0])

    assert candidate.platform == "appleMusic"
    assert candidate.source == "itunes"
    assert candidate.artist_name == "Krovna"
    assert candidate.title == "Berserker"
    assert candidate.duration_ms == 262105
    assert candidate.url == "https://music.apple.com/ua/song/berserker/1234567890"


def test_search_limit_is_five() -> None:
    assert itunes.SEARCH_LIMIT == 5


def test_result_with_a_null_track_view_url_is_dropped_rather_than_stringified() -> None:
    """`str()` would turn JSON `null` into `"None"`, which passes the emptiness
    filter and trips `is_safe_url` only later, far from the cause."""
    candidate = itunes._candidate_from_result({"trackName": "X", "trackViewUrl": None})

    assert candidate.url == ""


def test_result_that_is_not_a_dict_does_not_raise() -> None:
    candidate = itunes._candidate_from_result(["unexpected"])

    assert candidate.url == ""
    assert candidate.title == ""
