"""song.link (ядро, contract p.1): parses `__NEXT_DATA__` from the landing
page — Odesli's own docs recommend this exact page for personal/casual use
without an API key (their `v1-alpha.1` API returns 401 as of 2026, D-27).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

import httpx

from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler, parse_retry_after_seconds
from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_str
from pisniar.resolve.urls import normalize_url

logger = get_logger(__name__)

THROTTLE = Throttler(min_interval_seconds=1.0)
MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 2.0

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


class SonglinkParseError(RuntimeError):
    """The page returned 200 but `__NEXT_DATA__` couldn't be parsed — a
    page-structure drift (risk R2), not "track not found". The caller
    should alert, not silently treat this as a miss."""


@dataclass(frozen=True)
class SonglinkResult:
    found: bool
    provider: str | None = None
    isrc: str | None = None
    title: str | None = None
    artist_name: str | None = None
    duration_ms: int | None = None
    cover_url: str | None = None
    album_id_spotify: str | None = None
    year: int | None = None
    platform_links: dict[str, str] = field(default_factory=dict)


async def fetch(client: httpx.AsyncClient, cache: SourceCache, external_url: str) -> SonglinkResult:
    # Through the normalizer, not the raw string: otherwise the same track
    # submitted with a tracking suffix becomes a second cache row (F-17).
    cache_key = build_request_key("songlink:url:", normalize_url(external_url), casefold=False)

    async def do_fetch() -> dict[str, object]:
        return await _fetch_and_parse(client, external_url)

    payload = await cache.get_or_fetch("songlink", cache_key, do_fetch)
    return _result_from_payload(payload)


async def _fetch_and_parse(client: httpx.AsyncClient, external_url: str) -> dict[str, object]:
    request_url = f"https://song.link/{external_url}"

    for attempt in range(MAX_RETRIES):
        await THROTTLE.wait()
        response = await client.get(
            request_url,
            follow_redirects=True,
            headers={"User-Agent": "pisniar-v2 (personal Telegram bot)"},
        )

        if response.status_code == 429:
            delay = parse_retry_after_seconds(response.headers.get("Retry-After"))
            logger.warning("songlink_rate_limited", attempt=attempt, delay=delay)
            await asyncio.sleep(delay if delay is not None else DEFAULT_RETRY_DELAY_SECONDS)
            continue

        if response.status_code == 404:
            return {"found": False}

        response.raise_for_status()
        return _parse_page(response.text)

    raise SonglinkParseError(f"song.link kept rate-limiting after {MAX_RETRIES} attempts")


def _parse_page(html: str) -> dict[str, object]:
    match = NEXT_DATA_RE.search(html)
    if match is None:
        raise SonglinkParseError("__NEXT_DATA__ script tag not found on song.link page")

    try:
        next_data = json.loads(match.group(1))
        page_data = next_data["props"]["pageProps"]["pageData"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise SonglinkParseError(f"unexpected song.link page structure: {error}") from error

    return {"found": True, "page_data": page_data}


def _result_from_payload(payload: dict[str, object]) -> SonglinkResult:
    if not payload.get("found"):
        return SonglinkResult(found=False)

    page_data = as_dict(payload.get("page_data"))
    entity_data = as_dict(page_data.get("entityData"))
    release_date = as_dict(entity_data.get("releaseDate"))
    provider = as_str(entity_data.get("provider"))

    return SonglinkResult(
        found=True,
        provider=provider,
        isrc=as_str(entity_data.get("isrc")),
        title=as_str(entity_data.get("title")),
        artist_name=as_str(entity_data.get("artistName")),
        duration_ms=as_int(entity_data.get("duration")),
        cover_url=as_str(entity_data.get("thumbnailUrl")),
        album_id_spotify=as_str(entity_data.get("albumId")) if provider == "spotify" else None,
        year=as_int(release_date.get("year")),
        platform_links=_extract_platform_links(as_list(page_data.get("sections"))),
    )


def _extract_platform_links(sections: list[object]) -> dict[str, str]:
    wanted_section_ids = {"section|auto|links|listen", "section|auto|links|buy"}
    links: dict[str, str] = {}
    for raw_section in sections:
        section = as_dict(raw_section)
        if section.get("sectionId") not in wanted_section_ids:
            continue
        for raw_link in as_list(section.get("links")):
            link = as_dict(raw_link)
            url = as_str(link.get("url"))
            platform = as_str(link.get("platform"))
            if url and platform:
                links[platform] = url
    return links
