"""song.link: parses `__NEXT_DATA__` from the landing page, which Odesli's docs
recommend for personal use without an API key (their `v1-alpha.1` API answers 401).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

import httpx

from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    Throttler,
    parse_retry_after_seconds,
)
from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_str
from pisniar.resolve.urls import normalize_url

logger = get_logger(__name__)

THROTTLE = Throttler(min_interval_seconds=1.0)
MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 2.0

# The only adapter that follows redirects: `song.link` answers 308/302 to its
# own relative paths, and the `?url=` form lands on `odesli.co`.
ODESLI_HOST_SUFFIXES = ("song.link", "odesli.co")

REQUEST_HEADERS = {"User-Agent": "pisniar-v2 (personal Telegram bot)"}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


class SonglinkParseError(RuntimeError):
    """A 200 page that does not parse: structure drift, not a miss, so the
    caller alerts instead of caching it."""


class _RateLimited(RuntimeError):
    """429 from a single attempt — carries the `Retry-After` delay up to the
    retry loop, which owns the sleeping."""

    def __init__(self, delay: float | None) -> None:
        super().__init__("song.link rate-limited")
        self.delay = delay


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
    # submitted with a tracking suffix becomes a second cache row.
    cache_key = build_request_key("songlink:url:", normalize_url(external_url), casefold=False)

    async def do_fetch() -> dict[str, object]:
        return await _fetch_and_parse(client, external_url)

    payload = await cache.get_or_fetch("songlink", cache_key, do_fetch)
    return _result_from_payload(payload)


async def _fetch_and_parse(client: httpx.AsyncClient, external_url: str) -> dict[str, object]:
    request_url = httpx.URL(f"https://song.link/{external_url}")

    for attempt in range(MAX_RETRIES):
        # One wait per attempt, not per hop: the hops of a redirect chain are
        # the continuation of one logical request, exactly as httpx sent them.
        await THROTTLE.wait()
        try:
            return await _fetch_one_attempt(client, request_url)
        except _RateLimited as rate_limit:
            delay = (
                rate_limit.delay if rate_limit.delay is not None else DEFAULT_RETRY_DELAY_SECONDS
            )
            logger.warning("songlink_rate_limited", attempt=attempt, delay=delay)
            await asyncio.sleep(delay)

    raise SonglinkParseError(f"song.link kept rate-limiting after {MAX_RETRIES} attempts")


async def _fetch_one_attempt(client: httpx.AsyncClient, start_url: httpx.URL) -> dict[str, object]:
    """Redirects are walked by hand: `follow_redirects=True` contacts every hop
    inside `send()`, before any check could refuse it."""
    current_url = start_url

    for _hop in range(MAX_REDIRECTS + 1):
        async with client.stream(
            "GET", current_url, follow_redirects=False, headers=REQUEST_HEADERS
        ) as response:
            if response.has_redirect_location:
                current_url = _validated_redirect_target(response)
                continue

            if response.status_code == 429:
                raise _RateLimited(parse_retry_after_seconds(response.headers.get("Retry-After")))

            if response.status_code == 404:
                return {"found": False}

            response.raise_for_status()
            html = await _read_limited(response)

        return _parse_page(html)

    raise SonglinkParseError(f"song.link redirected more than {MAX_REDIRECTS} times")


async def _read_limited(response: httpx.Response) -> str:
    """Counts decoded chunks: song.link's chunked CDN response sends no
    `Content-Length`, and where one is sent it counts compressed bytes."""
    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in response.aiter_bytes():
        total_bytes += len(chunk)
        if total_bytes > MAX_RESPONSE_BYTES:
            raise ResponseTooLargeError(
                f"song.link response exceeded {MAX_RESPONSE_BYTES} bytes while streaming"
            )
        chunks.append(chunk)

    return b"".join(chunks).decode(response.charset_encoding or "utf-8", errors="replace")


def _validated_redirect_target(response: httpx.Response) -> httpx.URL:
    """Validated before it is requested, so a refused hop is never contacted.
    Fail-closed: answering "not found" instead would cache the refusal."""
    next_request = response.next_request
    if next_request is None:
        raise SonglinkParseError(f"song.link sent {response.status_code} without a Location")

    target = next_request.url
    host = (target.host or "").lower()
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ODESLI_HOST_SUFFIXES):
        logger.warning("songlink_redirect_left_odesli", host=host, status=response.status_code)
        raise SonglinkParseError(f"song.link redirected outside Odesli: {target}")

    if target.scheme != "https":
        logger.warning("songlink_redirect_downgraded_scheme", scheme=target.scheme, host=host)
        raise SonglinkParseError(f"song.link redirected to a non-https URL: {target}")

    return target


def _parse_page(html: str) -> dict[str, object]:
    match = NEXT_DATA_RE.search(html)
    if match is None:
        raise SonglinkParseError("__NEXT_DATA__ script tag not found on song.link page")

    try:
        next_data = json.loads(match.group(1))
        page_props = as_dict(next_data["props"]["pageProps"])
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise SonglinkParseError(f"unexpected song.link page structure: {error}") from error

    # A quota refusal arrives as HTTP 200 carrying `pageProps.error` beside a
    # stub `pageData`: transport, never a miss, so it must not be cached.
    embedded_error = page_props.get("error")
    if embedded_error is not None:
        status_code = as_dict(embedded_error).get("statusCode")
        raise SonglinkParseError(f"song.link answered with an in-page error: {status_code}")

    page_data = as_dict(page_props.get("pageData"))
    _require_entity_data(page_data)

    return {"found": True, "page_data": page_data}


def _require_entity_data(page_data: dict[str, object]) -> dict[str, object]:
    """Only HTTP 404 is a verified miss. A 200 page with no entity is an
    unconfirmed shape, so it raises instead of being kept as an empty hit."""
    entity_data = as_dict(page_data.get("entityData"))
    if not entity_data:
        raise SonglinkParseError("song.link page carried no entityData")

    return entity_data


def _result_from_payload(payload: dict[str, object]) -> SonglinkResult:
    if not payload.get("found"):
        return SonglinkResult(found=False)

    # Guards the read path too: a cached row without an entity must not be
    # served as a valid, completely empty answer.
    page_data = as_dict(payload.get("page_data"))
    entity_data = _require_entity_data(page_data)
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
