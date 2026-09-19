"""Spotify Web API (Client Credentials): ISRC-exact search (no `matching`
needed), name-search fallback (needs `matching`), and `/v1/tracks/{id}` for
year/album/release type. The access token lives in process memory (D-13),
never Redis — that broker has no password.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field

import httpx

from pisniar.logging import get_logger, register_runtime_secret
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler, parse_retry_after_seconds
from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_str
from pisniar.resolve.matching import is_valid_isrc, is_valid_spotify_id
from pisniar.resolve.types import TrackCandidate

logger = get_logger(__name__)

THROTTLE = Throttler(min_interval_seconds=1.0)
SEARCH_LIMIT = 5
SOURCE_NAME = "spotify"
MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 2.0
TOKEN_EXPIRY_MARGIN_SECONDS = 60
RUNTIME_SECRET_NAME = "spotify_access_token"

_cached_access_token: str | None = None
_cached_token_expires_at: float = 0.0


@dataclass(frozen=True)
class SpotifyTrackResult:
    found: bool
    spotify_id: str | None = None
    url: str | None = None
    isrc: str | None = None
    title: str | None = None
    artist_names: list[str] = field(default_factory=list)
    artist_ids: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    year: int | None = None
    release_type: str | None = None
    release_title: str | None = None
    album_id: str | None = None


async def _get_access_token(client: httpx.AsyncClient, client_id: str, client_secret: str) -> str:
    global _cached_access_token, _cached_token_expires_at

    now = time.monotonic()
    if _cached_access_token is not None and now < _cached_token_expires_at:
        return _cached_access_token

    basic_auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = await client.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic_auth}"},
    )
    response.raise_for_status()
    data = response.json()

    _cached_access_token = data["access_token"]
    _cached_token_expires_at = now + data["expires_in"] - TOKEN_EXPIRY_MARGIN_SECONDS

    register_runtime_secret(RUNTIME_SECRET_NAME, _cached_access_token)
    return _cached_access_token


async def _request(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str | int],
    client_id: str,
    client_secret: str,
) -> httpx.Response:
    for attempt in range(MAX_RETRIES):
        token = await _get_access_token(client, client_id, client_secret)
        await THROTTLE.wait()
        response = await client.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"}
        )

        if response.status_code == 429:
            delay = parse_retry_after_seconds(response.headers.get("Retry-After"))
            logger.warning("spotify_rate_limited", attempt=attempt, delay=delay)
            await asyncio.sleep(delay if delay is not None else DEFAULT_RETRY_DELAY_SECONDS)
            continue

        response.raise_for_status()
        return response

    raise httpx.HTTPError(f"Spotify kept rate-limiting after {MAX_RETRIES} attempts")


async def search_by_isrc(
    client: httpx.AsyncClient, cache: SourceCache, isrc: str, client_id: str, client_secret: str
) -> SpotifyTrackResult:
    if not is_valid_isrc(isrc):
        return SpotifyTrackResult(found=False)

    cache_key = build_request_key("spotify:search:isrc:", isrc, casefold=False)

    async def do_fetch() -> dict[str, object]:
        response = await _request(
            client,
            "https://api.spotify.com/v1/search",
            {"q": f"isrc:{isrc}", "type": "track", "limit": 1},
            client_id,
            client_secret,
        )
        items = as_list(as_dict(as_dict(response.json()).get("tracks")).get("items"))
        if not items:
            return {"found": False}
        return {"found": True, "data": items[0]}

    payload = await cache.get_or_fetch("spotify", cache_key, do_fetch)
    return _track_result_from_payload(payload)


async def search_by_name(
    client: httpx.AsyncClient,
    cache: SourceCache,
    artist: str,
    title: str,
    client_id: str,
    client_secret: str,
) -> list[TrackCandidate]:
    cache_key = build_request_key("spotify:search:name:", artist, title)

    async def do_fetch() -> dict[str, object]:
        response = await _request(
            client,
            "https://api.spotify.com/v1/search",
            {"q": f'artist:"{artist}" track:"{title}"', "type": "track", "limit": SEARCH_LIMIT},
            client_id,
            client_secret,
        )
        items = as_list(as_dict(as_dict(response.json()).get("tracks")).get("items"))
        return {"results": items}

    payload = await cache.get_or_fetch("spotify", cache_key, do_fetch)
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    candidates = [_candidate_from_item(item) for item in results[:SEARCH_LIMIT]]
    return [candidate for candidate in candidates if candidate.url]


async def get_track(
    client: httpx.AsyncClient,
    cache: SourceCache,
    spotify_track_id: str,
    client_id: str,
    client_secret: str,
) -> SpotifyTrackResult:
    if not is_valid_spotify_id(spotify_track_id):
        # No caller today; the check goes in now so steps 3-5 cannot paste a
        # source-supplied id straight into the path (R2-15).
        return SpotifyTrackResult(found=False)

    cache_key = build_request_key("spotify:track:", spotify_track_id, casefold=False)

    async def do_fetch() -> dict[str, object]:
        response = await _request(
            client,
            f"https://api.spotify.com/v1/tracks/{spotify_track_id}",
            {},
            client_id,
            client_secret,
        )
        return {"found": True, "data": response.json()}

    payload = await cache.get_or_fetch("spotify", cache_key, do_fetch)
    return _track_result_from_payload(payload)


def _track_result_from_payload(payload: dict[str, object]) -> SpotifyTrackResult:
    if not payload.get("found"):
        return SpotifyTrackResult(found=False)

    data = as_dict(payload.get("data"))
    artists = [as_dict(artist) for artist in as_list(data.get("artists"))]
    album = as_dict(data.get("album"))
    external_ids = as_dict(data.get("external_ids"))
    release_date = as_str(album.get("release_date"))

    return SpotifyTrackResult(
        found=True,
        spotify_id=as_str(data.get("id")),
        url=as_str(as_dict(data.get("external_urls")).get("spotify")),
        isrc=as_str(external_ids.get("isrc")),
        title=as_str(data.get("name")),
        artist_names=[name for artist in artists if (name := as_str(artist.get("name")))],
        artist_ids=[artist_id for artist in artists if (artist_id := as_str(artist.get("id")))],
        duration_ms=as_int(data.get("duration_ms")),
        year=int(release_date[:4]) if release_date and len(release_date) >= 4 else None,
        release_type=as_str(album.get("album_type")),
        release_title=as_str(album.get("name")),
        album_id=as_str(album.get("id")),
    )


def _candidate_from_item(item: dict[str, object]) -> TrackCandidate:
    artists = [as_dict(artist) for artist in as_list(item.get("artists"))]
    artist_name = ", ".join(as_str(artist.get("name")) or "" for artist in artists)
    external_ids = as_dict(item.get("external_ids"))
    return TrackCandidate(
        platform="spotify",
        url=as_str(as_dict(item.get("external_urls")).get("spotify")) or "",
        source=SOURCE_NAME,
        artist_name=artist_name,
        title=as_str(item.get("name")) or "",
        duration_ms=as_int(item.get("duration_ms")),
        isrc=as_str(external_ids.get("isrc")),
    )
