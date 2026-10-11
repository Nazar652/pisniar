"""YouTube Music via `ytmusicapi` (unauthenticated search): sync, so every call runs in
`asyncio.to_thread` under `wait_for`. The client's session carries a request timeout, so a
hung socket frees its executor thread after `REQUEST_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests
from ytmusicapi import YTMusic

from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import TIMEOUT_SECONDS, Throttler
from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_str
from pisniar.resolve.types import TrackCandidate

THROTTLE = Throttler(min_interval_seconds=1.0)
SEARCH_LIMIT = 5
SOURCE_NAME = "ytmusic"

# two calls in a row (lyrics) stay within the 20 s lookup limit
REQUEST_TIMEOUT_SECONDS = 9.0

cached_client: YTMusic | None = None


class TimeoutSession(requests.Session):
    """a session whose every request has a timeout unless the caller sets one."""

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT_SECONDS
        return super().send(request, **kwargs)


def get_client() -> YTMusic:
    global cached_client
    if cached_client is None:
        cached_client = YTMusic(requests_session=TimeoutSession())
    return cached_client


def search_sync(query: str) -> list[dict[str, object]]:
    return get_client().search(query, filter="songs", limit=SEARCH_LIMIT)


async def search_by_name(
    cache: SourceCache,
    artist: str,
    title: str,
) -> list[TrackCandidate]:
    cache_key = build_request_key("ytmusic:search:", artist, title)

    async def do_fetch() -> dict[str, object]:
        await THROTTLE.wait()
        # `wait_for` frees this task, not the thread — but a hung socket
        # then costs one thread, not the worker's only concurrency slot.
        results = await asyncio.wait_for(asyncio.to_thread(search_sync, f"{artist} {title}"), timeout=TIMEOUT_SECONDS)
        return {"results": results}

    payload = await cache.get_or_fetch("ytmusic", cache_key, do_fetch)
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    candidates = [candidate_from_result(as_dict(item)) for item in results[:SEARCH_LIMIT]]
    return [candidate for candidate in candidates if candidate.url]


def candidate_from_result(item: dict[str, object]) -> TrackCandidate:
    artists = [as_dict(artist) for artist in as_list(item.get("artists"))]
    artist_name = ", ".join(name for artist in artists if (name := as_str(artist.get("name"))))
    video_id = as_str(item.get("videoId")) or ""
    duration_seconds = as_int(item.get("duration_seconds"))

    return TrackCandidate(
        platform="youtubeMusic",
        # empty `videoId` would yield a `?v=` URL that passes every scheme
        # check and permanently fails the post on download.
        url=f"https://music.youtube.com/watch?v={video_id}" if video_id else "",
        source=SOURCE_NAME,
        artist_name=artist_name,
        title=as_str(item.get("title")) or "",
        duration_ms=duration_seconds * 1000 if duration_seconds is not None else None,
    )
