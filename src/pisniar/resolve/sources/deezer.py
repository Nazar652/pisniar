"""Deezer (contract p.1/p.30): ISRC-exact track lookup, name-search
fallback, and album genres. **Pitfall**: a not-found ISRC lookup answers
`HTTP 200` with `{"error": {...}}` in the body — never trust the status
code alone here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler
from pisniar.resolve.matching import is_valid_isrc
from pisniar.resolve.types import TrackCandidate

THROTTLE = Throttler(min_interval_seconds=1.0)
SEARCH_LIMIT = 5
SOURCE_NAME = "deezer"


@dataclass(frozen=True)
class DeezerTrackResult:
    found: bool
    deezer_id: int | None = None
    url: str | None = None
    isrc: str | None = None
    album_id: int | None = None
    title: str | None = None
    artist_name: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class DeezerAlbumResult:
    found: bool
    genre_ids: list[int] = field(default_factory=list)
    genre_english_names: dict[int, str] = field(default_factory=dict)
    release_date: str | None = None
    record_type: str | None = None


def _is_error_body(data: object) -> bool:
    """The pitfall check: Deezer answers 200 with `{"error": {...}}` for a
    miss, so the body — not the status code — decides "found"."""
    return isinstance(data, dict) and "error" in data


async def _request(
    client: httpx.AsyncClient, url: str, params: dict[str, str | int]
) -> dict[str, object]:
    await THROTTLE.wait()
    response = await client.get(url, params=params)
    response.raise_for_status()
    data: dict[str, object] = response.json()
    return data


async def fetch_by_isrc(
    client: httpx.AsyncClient, cache: SourceCache, isrc: str
) -> DeezerTrackResult:
    if not is_valid_isrc(isrc):
        return DeezerTrackResult(found=False)

    cache_key = build_request_key("deezer:isrc:", isrc, casefold=False)

    async def do_fetch() -> dict[str, object]:
        data = await _request(client, f"https://api.deezer.com/track/isrc:{isrc}", {})
        if _is_error_body(data):
            return {"found": False}
        return {"found": True, "data": data}

    payload = await cache.get_or_fetch("deezer", cache_key, do_fetch)
    return _track_result_from_payload(payload)


async def search_by_name(
    client: httpx.AsyncClient, cache: SourceCache, artist: str, title: str
) -> list[TrackCandidate]:
    cache_key = build_request_key("deezer:search:", artist, title)

    async def do_fetch() -> dict[str, object]:
        data = await _request(
            client,
            "https://api.deezer.com/search",
            {"q": f'artist:"{artist}" track:"{title}"', "limit": SEARCH_LIMIT},
        )
        return {"results": data.get("data") or []}

    payload = await cache.get_or_fetch("deezer", cache_key, do_fetch)
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    candidates = [_candidate_from_search_item(item) for item in results[:SEARCH_LIMIT]]
    return [candidate for candidate in candidates if candidate.url]


async def fetch_album(
    client: httpx.AsyncClient, cache: SourceCache, album_id: int
) -> DeezerAlbumResult:
    cache_key = build_request_key("deezer:album:", str(album_id), casefold=False)

    async def do_fetch() -> dict[str, object]:
        data = await _request(client, f"https://api.deezer.com/album/{album_id}", {})
        if _is_error_body(data):
            return {"found": False}
        return {"found": True, "data": data}

    payload = await cache.get_or_fetch("deezer", cache_key, do_fetch)
    return _album_result_from_payload(payload)


def _track_result_from_payload(payload: dict[str, object]) -> DeezerTrackResult:
    if not payload.get("found"):
        return DeezerTrackResult(found=False)

    data = payload["data"]
    assert isinstance(data, dict)
    album = data.get("album")
    artist = data.get("artist")
    duration_seconds = data.get("duration")
    return DeezerTrackResult(
        found=True,
        deezer_id=data.get("id") if isinstance(data.get("id"), int) else None,
        url=data.get("link") if isinstance(data.get("link"), str) else None,
        isrc=data.get("isrc") if isinstance(data.get("isrc"), str) else None,
        album_id=album.get("id")
        if isinstance(album, dict) and isinstance(album.get("id"), int)
        else None,
        title=data.get("title") if isinstance(data.get("title"), str) else None,
        artist_name=artist.get("name")
        if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        else None,
        duration_ms=duration_seconds * 1000 if isinstance(duration_seconds, int) else None,
    )


def _album_result_from_payload(payload: dict[str, object]) -> DeezerAlbumResult:
    if not payload.get("found"):
        return DeezerAlbumResult(found=False)

    data = payload["data"]
    assert isinstance(data, dict)
    genres_block = data.get("genres") or {}
    genre_entries = genres_block.get("data") or [] if isinstance(genres_block, dict) else []

    genre_ids: list[int] = []
    genre_english_names: dict[int, str] = {}
    for entry in genre_entries:
        genre_id = entry.get("id")
        genre_name = entry.get("name")
        if isinstance(genre_id, int) and isinstance(genre_name, str):
            genre_ids.append(genre_id)
            genre_english_names[genre_id] = genre_name

    return DeezerAlbumResult(
        found=True,
        genre_ids=genre_ids,
        genre_english_names=genre_english_names,
        release_date=data.get("release_date"),
        record_type=data.get("record_type"),
    )


def _candidate_from_search_item(item: dict[str, object]) -> TrackCandidate:
    artist = item.get("artist") or {}
    duration_seconds = item.get("duration")
    duration_ms = duration_seconds * 1000 if isinstance(duration_seconds, int) else None
    return TrackCandidate(
        platform="deezer",
        url=str(item.get("link", "")),
        source=SOURCE_NAME,
        artist_name=str(artist.get("name", "")) if isinstance(artist, dict) else "",
        title=str(item.get("title", "")),
        duration_ms=duration_ms,
    )
