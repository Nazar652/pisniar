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
from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_str
from pisniar.resolve.matching import is_valid_isrc
from pisniar.resolve.types import TrackCandidate

THROTTLE = Throttler(min_interval_seconds=1.0)
SEARCH_LIMIT = 5
SOURCE_NAME = "deezer"

# Whitelist, verified live against api.deezer.com on 2026-09-19: a missing
# ISRC and a missing album both answer `type: DataException, code: 800`.
# Anything else (quota, busy service, bad parameters) is an outage, not a
# miss — it raises instead of being cached as "not found" (R2-5).
NOT_FOUND_ERROR_CODES = frozenset({800})
NOT_FOUND_ERROR_TYPES = frozenset({"DataException"})


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


class DeezerApiError(RuntimeError):
    """A 200 response whose error body is not the documented "no data" miss.
    Raised rather than cached, the same way `SonglinkParseError` is: caching
    a quota failure as `{"found": false}` blanks the row for a whole TTL."""


def _is_not_found_body(data: object) -> bool:
    """The pitfall check: Deezer answers 200 with `{"error": {...}}` for both
    a miss and an outage, so the body — not the status code — decides."""
    body = as_dict(data)
    if "error" not in body:
        return False

    error = as_dict(body.get("error"))
    error_type = as_str(error.get("type"))
    error_code = as_int(error.get("code"))
    # `and`, not `or`: the live miss carries both marks together, so either
    # one alone is an unverified combination we must not cache as a miss.
    if error_code in NOT_FOUND_ERROR_CODES and error_type in NOT_FOUND_ERROR_TYPES:
        return True

    raise DeezerApiError(
        f"deezer error response: type={error_type}, code={error_code}, "
        f"message={as_str(error.get('message'))}"
    )


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
        if _is_not_found_body(data):
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
        # Checked here too: a quota error body would otherwise be cached as
        # an empty result list, which looks exactly like "nothing found".
        if _is_not_found_body(data):
            return {"results": []}
        return {"results": as_list(data.get("data"))}

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
        if _is_not_found_body(data):
            return {"found": False}
        return {"found": True, "data": data}

    payload = await cache.get_or_fetch("deezer", cache_key, do_fetch)
    return _album_result_from_payload(payload)


def _track_result_from_payload(payload: dict[str, object]) -> DeezerTrackResult:
    if not payload.get("found"):
        return DeezerTrackResult(found=False)

    data = as_dict(payload.get("data"))
    duration_seconds = as_int(data.get("duration"))
    return DeezerTrackResult(
        found=True,
        deezer_id=as_int(data.get("id")),
        url=as_str(data.get("link")),
        isrc=as_str(data.get("isrc")),
        album_id=as_int(as_dict(data.get("album")).get("id")),
        title=as_str(data.get("title")),
        artist_name=as_str(as_dict(data.get("artist")).get("name")),
        duration_ms=duration_seconds * 1000 if duration_seconds is not None else None,
    )


def _album_result_from_payload(payload: dict[str, object]) -> DeezerAlbumResult:
    if not payload.get("found"):
        return DeezerAlbumResult(found=False)

    data = as_dict(payload.get("data"))
    genre_entries = as_list(as_dict(data.get("genres")).get("data"))

    genre_ids: list[int] = []
    genre_english_names: dict[int, str] = {}
    for raw_entry in genre_entries:
        entry = as_dict(raw_entry)
        genre_id = as_int(entry.get("id"))
        genre_name = as_str(entry.get("name"))
        if genre_id is not None and genre_name is not None:
            genre_ids.append(genre_id)
            genre_english_names[genre_id] = genre_name

    return DeezerAlbumResult(
        found=True,
        genre_ids=genre_ids,
        genre_english_names=genre_english_names,
        release_date=as_str(data.get("release_date")),
        record_type=as_str(data.get("record_type")),
    )


def _candidate_from_search_item(raw_item: object) -> TrackCandidate:
    """Every field through `json_utils`: a `null` link used to become the
    string `"None"`, and a non-dict entry an `AttributeError` (R2-8)."""
    item = as_dict(raw_item)
    duration_seconds = as_int(item.get("duration"))
    return TrackCandidate(
        platform="deezer",
        url=as_str(item.get("link")) or "",
        source=SOURCE_NAME,
        artist_name=as_str(as_dict(item.get("artist")).get("name")) or "",
        title=as_str(item.get("title")) or "",
        duration_ms=duration_seconds * 1000 if duration_seconds is not None else None,
    )
