"""`deezer`: the HTTP-200-with-`error`-body pitfall, `limit=5` search, and
album genre extraction — via `httpx.MockTransport`, trimmed real payloads."""

from __future__ import annotations

import httpx

from pisniar.resolve.sources import deezer

ISRC_FOUND_BODY: dict[str, object] = {
    "id": 138547415,
    "title": "Creep",
    "isrc": "GBAYE9200070",
    "duration": 238,
    "link": "https://www.deezer.com/track/138547415",
    "artist": {"id": 399, "name": "Radiohead"},
    "album": {"id": 14880711, "title": "Pablo Honey"},
}
ISRC_NOT_FOUND_BODY = {"error": {"type": "DataException", "message": "no data", "code": 800}}
SEARCH_FOUND_BODY: dict[str, object] = {
    "data": [
        {
            "id": 1,
            "title": "Покаяння",
            "duration": 224,
            "link": "https://www.deezer.com/track/1",
            "artist": {"name": "Burned Time Machine"},
        }
    ],
    "total": 1,
}
ALBUM_FOUND_BODY: dict[str, object] = {
    "id": 14880711,
    "release_date": "1993-02-22",
    "record_type": "album",
    "genres": {"data": [{"id": 85, "name": "Alternative"}, {"id": 464, "name": "Metal"}]},
}


def make_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_fetch_by_isrc_treats_200_with_error_body_as_not_found() -> None:
    """The pitfall: a not-found ISRC still answers HTTP 200."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=ISRC_NOT_FOUND_BODY))

    async with make_client(transport) as client:
        payload = await deezer._request(client, "https://api.deezer.com/track/isrc:X", {})

    assert deezer._is_error_body(payload) is True


def test_fetch_by_isrc_extracts_fields_and_converts_seconds_to_ms() -> None:
    result = deezer._track_result_from_payload({"found": True, "data": ISRC_FOUND_BODY})

    assert result.found is True
    assert result.isrc == "GBAYE9200070"
    assert result.album_id == 14880711
    assert result.duration_ms == 238000
    assert result.artist_name == "Radiohead"


def test_track_result_from_not_found_payload() -> None:
    result = deezer._track_result_from_payload({"found": False})

    assert result.found is False
    assert result.album_id is None


def test_search_candidates_are_capped_at_five() -> None:
    many_results: list[dict[str, object]] = [
        {"id": i, "title": "Song", "duration": 200, "link": "u", "artist": {"name": "A"}}
        for i in range(20)
    ]

    candidates = [
        deezer._candidate_from_search_item(item) for item in many_results[: deezer.SEARCH_LIMIT]
    ]

    assert deezer.SEARCH_LIMIT == 5
    assert len(candidates) == 5


def test_search_candidate_maps_fields_correctly() -> None:
    search_results = SEARCH_FOUND_BODY["data"]
    assert isinstance(search_results, list)
    candidate = deezer._candidate_from_search_item(search_results[0])

    assert candidate.platform == "deezer"
    assert candidate.source == "deezer"
    assert candidate.artist_name == "Burned Time Machine"
    assert candidate.title == "Покаяння"
    assert candidate.duration_ms == 224000


def test_album_result_extracts_genre_ids_and_english_names() -> None:
    result = deezer._album_result_from_payload({"found": True, "data": ALBUM_FOUND_BODY})

    assert result.found is True
    assert result.genre_ids == [85, 464]
    assert result.genre_english_names == {85: "Alternative", 464: "Metal"}
    assert result.record_type == "album"
    assert result.release_date == "1993-02-22"


def test_album_result_from_not_found_payload() -> None:
    result = deezer._album_result_from_payload({"found": False})

    assert result.found is False
    assert result.genre_ids == []
