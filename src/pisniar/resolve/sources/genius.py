"""Genius, a plain-text lyrics source read by scraping the song page (its API serves no text). The URL is built from a
checked path on `genius.com`, redirects are not followed and the body is capped while it streams. A stdlib parser keeps
only the lyrics containers' text; any other shape raises and is never cached.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from html.parser import HTMLParser

import httpx

from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler
from pisniar.resolve.json_utils import as_dict, as_list, as_str
from pisniar.resolve.matching import is_match
from pisniar.resolve.sources.lrclib import (
    LyricsLine,
    LyricsOutcome,
    LyricsResult,
    join_artist_names,
)

logger = get_logger(__name__)

THROTTLE = Throttler(min_interval_seconds=1.0)
SOURCE_NAME = "genius"

PAGE_ORIGIN = "https://genius.com"
SEARCH_URL = f"{PAGE_ORIGIN}/api/search/song"
REQUEST_HEADERS = {"User-Agent": "pisniar-v2/0.1 (personal Telegram music bot)"}

# a song page is ~270 KB; decoded bytes past this are refused, whatever Content-Length says.
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_CANDIDATES = 10

# the only path shape of a lyrics page; a translation page has the same one, hence the markers below.
LYRICS_PATH_PATTERN = re.compile(r"/[A-Za-z0-9-]+-lyrics")
TRANSLATION_MARKERS = ("translation", "переклад", "traduc")
LYRICS_COMPLETE_STATE = "complete"

# «Zlypni (UKR)», «Стеля (Ceiling)»: the transliterations Genius appends to a name.
ANNOTATION_PATTERN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
SECTION_LABEL_PATTERN = re.compile(r"^\[[^\]]*\]$")

LYRICS_CONTAINER_ATTRIBUTE = "data-lyrics-container"
EXCLUDED_ATTRIBUTE = "data-exclude-from-selection"
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
SKIPPED_TAGS = frozenset({"script", "style", "svg"})


class GeniusApiError(RuntimeError):
    """a response whose shape proves nothing, an empty page included. Raised rather
    than cached, like `LrclibApiError`."""

    outcome = LyricsOutcome.SOURCE_UNAVAILABLE


class LyricsHtmlParser(HTMLParser):
    """collects the text of `data-lyrics-container` elements, `<br>` as a line break, skipping the UI Genius nests
    inside them. Nothing it reads is executed or fetched."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.container_depth: int | None = None
        self.skipped_depth: int | None = None
        self.chunks: list[str] = []

    @property
    def collecting(self) -> bool:
        return self.container_depth is not None and self.skipped_depth is None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_TAGS:
            if tag == "br" and self.collecting:
                self.chunks.append("\n")
            return

        self.depth += 1
        attributes = dict(attrs)
        if self.container_depth is None:
            if attributes.get(LYRICS_CONTAINER_ATTRIBUTE) == "true":
                self.container_depth = self.depth
        elif self.skipped_depth is None and (EXCLUDED_ATTRIBUTE in attributes or tag in SKIPPED_TAGS):
            self.skipped_depth = self.depth

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return

        if self.skipped_depth == self.depth:
            self.skipped_depth = None
        if self.container_depth == self.depth:
            self.container_depth = None
            self.chunks.append("\n")
        self.depth = max(self.depth - 1, 0)

    def handle_data(self, data: str) -> None:
        if self.collecting:
            self.chunks.append(data)


def lyrics_lines_from_html(page: str) -> tuple[str, ...]:
    """section labels such as `[Куплет 1: …]` are not sung, so they are dropped."""
    parser = LyricsHtmlParser()
    parser.feed(page)
    parser.close()

    lines = (" ".join(raw_line.split()) for raw_line in "".join(parser.chunks).split("\n"))
    return tuple(line for line in lines if line and not SECTION_LABEL_PATTERN.match(line))


def strip_annotations(value: str) -> str:
    return " ".join(ANNOTATION_PATTERN.sub(" ", value).split())


def looks_like_translation(*fields: str) -> bool:
    return any(marker in field.lower() for field in fields for marker in TRANSLATION_MARKERS)


async def fetch_lyrics(
    client: httpx.AsyncClient,
    cache: SourceCache,
    *,
    artist_names: Sequence[str],
    title: str,
    duration_ms: int | None,
) -> LyricsResult:
    artist_query = join_artist_names(artist_names)
    normalized_title = " ".join(title.split())

    if not artist_query or not normalized_title:
        logger.warning(
            "genius_query_incomplete",
            has_artist=bool(artist_query),
            has_title=bool(normalized_title),
        )
        return LyricsResult(outcome=LyricsOutcome.ABSENT)

    cache_key = build_request_key("genius:lyrics:", artist_query, normalized_title)
    asked_genius = False

    async def do_fetch() -> dict[str, object]:
        nonlocal asked_genius
        asked_genius = True

        path = await search_for_path(client, artist_query=artist_query, title=normalized_title, duration_ms=duration_ms)
        if path is None:
            return {"found": False}

        lines = lyrics_lines_from_html(await fetch_page(client, path))
        if not lines:
            raise GeniusApiError("genius: у сторінці пісні немає тексту")
        return {"found": True, "lines": list(lines)}

    payload = await cache.get_or_fetch(SOURCE_NAME, cache_key, do_fetch)
    result = result_from_payload(payload)

    logger.info(
        "genius_lyrics_outcome",
        outcome=result.outcome.value,
        line_count=len(result.lines),
        from_cache=not asked_genius,
    )
    return result


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    query_parameters: dict[str, str] | None = None,
) -> bytes:
    """one attempt; the body is read in chunks and refused past `MAX_PAGE_BYTES`."""
    await THROTTLE.wait()
    async with client.stream("GET", url, params=query_parameters, headers=REQUEST_HEADERS) as response:
        if response.status_code != httpx.codes.OK:
            raise GeniusApiError(f"genius відповів HTTP {response.status_code}")

        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
            raise GeniusApiError(f"genius оголосив тіло {declared} байтів")

        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_PAGE_BYTES:
                raise GeniusApiError(f"genius відповів тілом понад {MAX_PAGE_BYTES} байтів")
    return bytes(body)


async def search_for_path(
    client: httpx.AsyncClient,
    *,
    artist_query: str,
    title: str,
    duration_ms: int | None,
) -> str | None:
    """the first search hit that is our track and not its translation."""
    raw_body = await send_request(client, SEARCH_URL, {"q": f"{artist_query} {title}"})
    try:
        body = as_dict(json.loads(raw_body))
    except ValueError as error:
        raise GeniusApiError("genius: пошук відповів не-JSON тілом") from error

    sections = as_list(as_dict(body.get("response")).get("sections"))
    hits = as_list(as_dict(sections[0]).get("hits")) if sections else []
    candidates = hits[:MAX_SEARCH_CANDIDATES]

    for hit in candidates:
        result = as_dict(as_dict(hit).get("result"))
        path = path_if_ours(result, artist_query=artist_query, title=title, duration_ms=duration_ms)
        if path is not None:
            return path

    logger.info("genius_search_no_match", candidate_count=len(candidates))
    return None


def path_if_ours(
    result: dict[str, object],
    *,
    artist_query: str,
    title: str,
    duration_ms: int | None,
) -> str | None:
    path = as_str(result.get("path")) or ""
    candidate_title = as_str(result.get("title")) or ""
    all_artists = as_str(result.get("artist_names")) or ""
    primary_artists = [as_str(as_dict(artist).get("name")) or "" for artist in as_list(result.get("primary_artists"))]

    if (
        not LYRICS_PATH_PATTERN.fullmatch(path)
        or result.get("lyrics_state") != LYRICS_COMPLETE_STATE
        or result.get("instrumental") is True
        or looks_like_translation(path, candidate_title, all_artists)
    ):
        return None

    # the page credits every artist, our track may credit one: the whole credit or any one primary artist counts.
    for candidate_artist in (all_artists, *primary_artists):
        if is_match(
            reference_artist=strip_annotations(artist_query),
            reference_title=strip_annotations(title),
            reference_duration_ms=duration_ms,
            candidate_artist=strip_annotations(candidate_artist),
            candidate_title=strip_annotations(candidate_title),
            candidate_duration_ms=None,
        ):
            return path
    return None


async def fetch_page(client: httpx.AsyncClient, path: str) -> str:
    """`path` has passed `LYRICS_PATH_PATTERN`, so the host is always `genius.com`."""
    return (await send_request(client, f"{PAGE_ORIGIN}{path}")).decode("utf-8", errors="replace")


def result_from_payload(payload: dict[str, object]) -> LyricsResult:
    """a cached row is re-read through the same checks, not trusted."""
    if not payload.get("found"):
        return LyricsResult(outcome=LyricsOutcome.ABSENT)

    lines = [as_str(line) for line in as_list(payload.get("lines"))]
    if not lines or any(line is None or not line.strip() for line in lines):
        raise GeniusApiError("genius: у кешованій відповіді немає рядків тексту")

    return LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=tuple(LyricsLine(start_ms=None, text=line) for line in lines if line is not None),
    )
