"""MusicBrainz recording ISRC lookup: EXACT_ISRC class, no `matching` needed.
Disabled without a contact: MusicBrainz's usage policy requires an identifiable
`User-Agent`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from pisniar.logging import get_logger
from pisniar.post.sanitize import sanitize_metadata_text
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler
from pisniar.resolve.json_utils import as_dict, as_list, as_str
from pisniar.resolve.matching import is_valid_isrc

logger = get_logger(__name__)

THROTTLE = Throttler(min_interval_seconds=1.0)
SOURCE_NAME = "musicbrainz"

URL_REL_HOST_TO_PLATFORM = {
    "open.spotify.com": "spotify",
    "www.deezer.com": "deezer",
    "deezer.com": "deezer",
    "music.apple.com": "appleMusic",
    "tidal.com": "tidal",
    "listen.tidal.com": "tidal",
    "www.qobuz.com": "qobuz",
}


@dataclass(frozen=True)
class MusicBrainzRecordingResult:
    found: bool
    recording_id: str | None = None
    platform_links: dict[str, str] = field(default_factory=dict)


async def fetch_by_isrc(
    client: httpx.AsyncClient, cache: SourceCache, isrc: str, contact: str | None
) -> MusicBrainzRecordingResult:
    if not contact or not is_valid_isrc(isrc):
        return MusicBrainzRecordingResult(found=False)

    cache_key = build_request_key("musicbrainz:isrc:", isrc, casefold=False)

    async def do_fetch() -> dict[str, object]:
        await THROTTLE.wait()
        response = await client.get(
            f"https://musicbrainz.org/ws/2/isrc/{isrc}",
            params={"inc": "url-rels", "fmt": "json"},
            headers={"User-Agent": f"pisniar-v2/0.1 ( {contact} )"},
        )
        if response.status_code == 404:
            return {"found": False}
        response.raise_for_status()

        recordings = as_list(as_dict(response.json()).get("recordings"))
        if not recordings:
            return {"found": False}
        return {"found": True, "data": recordings[0]}

    payload = await cache.get_or_fetch("musicbrainz", cache_key, do_fetch)
    return result_from_payload(payload)


def result_from_payload(payload: dict[str, object]) -> MusicBrainzRecordingResult:
    if not payload.get("found"):
        return MusicBrainzRecordingResult(found=False)

    data = as_dict(payload.get("data"))
    relations = [as_dict(relation) for relation in as_list(data.get("relations"))]

    platform_links: dict[str, str] = {}
    for relation in relations:
        url = as_str(as_dict(relation.get("url")).get("resource"))
        if url is None:
            continue
        host = host_or_none(url)
        if host is None:
            continue
        platform = URL_REL_HOST_TO_PLATFORM.get(host)
        if platform is not None:
            platform_links[platform] = url

    return MusicBrainzRecordingResult(
        found=True,
        recording_id=as_str(data.get("id")),
        platform_links=platform_links,
    )


def host_or_none(url: str) -> str | None:
    """MusicBrainz is publicly editable, and one bad url-rel must not fail the post.
    `ValueError` covers idna's `InvalidCodepoint`, which httpx does not wrap."""
    try:
        return httpx.URL(url).host
    except (httpx.InvalidURL, ValueError):
        # sanitized: a control character in the url-rel is exactly what
        # gets it here, and the raw value would go into the log line.
        logger.warning("musicbrainz_url_rel_unparsable", url=sanitize_metadata_text(url))
        return None
