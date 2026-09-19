"""iTunes Search API (contract p.30): name-search only, no ISRC lookup.
Requires an explicit `country` (default storefront is US) and a slower
throttle (~20 req/min unofficial community limit -> >=3s between calls).
"""

from __future__ import annotations

import httpx

from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler
from pisniar.resolve.types import TrackCandidate

MIN_INTERVAL_SECONDS = 3.0
THROTTLE = Throttler(min_interval_seconds=MIN_INTERVAL_SECONDS)
SEARCH_LIMIT = 5
SOURCE_NAME = "itunes"


async def search_by_name(
    client: httpx.AsyncClient, cache: SourceCache, artist: str, title: str, country: str
) -> list[TrackCandidate]:
    # `country` in the key (F-11): it changes both the result set and the
    # storefront URL, so two storefronts must never share a row.
    cache_key = build_request_key("itunes:search:", country, artist, title)

    async def do_fetch() -> dict[str, object]:
        await THROTTLE.wait()
        response = await client.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{artist} {title}",
                "media": "music",
                "entity": "song",
                "country": country,
                "limit": SEARCH_LIMIT,
            },
        )
        response.raise_for_status()
        data: dict[str, object] = response.json()
        return {"results": data.get("results") or []}

    payload = await cache.get_or_fetch("itunes", cache_key, do_fetch)
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    candidates = [_candidate_from_result(item) for item in results[:SEARCH_LIMIT]]
    return [candidate for candidate in candidates if candidate.url]


def _candidate_from_result(item: dict[str, object]) -> TrackCandidate:
    duration_ms = item.get("trackTimeMillis")
    return TrackCandidate(
        platform="appleMusic",
        url=str(item.get("trackViewUrl", "")),
        source=SOURCE_NAME,
        artist_name=str(item.get("artistName", "")),
        title=str(item.get("trackName", "")),
        duration_ms=duration_ms if isinstance(duration_ms, int) else None,
    )
