"""Where lyrics come from: the rule for timed text (YouTube Music, then LRCLIB), the plain-text
sources an admin can pick, and the helpers the render, the plain-text task and the Mini App share.
Nothing here opens a session or writes a row; the cache opens its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import SQLAlchemyError

from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH, MAX_LYRICS_TOTAL_CHARACTERS
from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import genius, ytmusic_lyrics
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.resolve.sources.lrclib import fetch_lyrics as fetch_lrclib_lyrics

logger = get_logger(__name__)

LRCLIB_SOURCE = "lrclib"
YTMUSIC_SOURCE = ytmusic_lyrics.SOURCE_NAME
GENIUS_SOURCE = genius.SOURCE_NAME

# a lookup the admin waits for: one source (or the two timed ones) answers within this many seconds.
LYRICS_LOOKUP_TIMEOUT_SECONDS = 20.0

# a source had text, but too much of it to store, and no other source had any.
LYRICS_OVER_LIMITS = "lyrics_over_limits"


@dataclass(frozen=True)
class PlainTextSource:
    """`name` is what a stored line records as its `source`; `label` is how the admin reads it."""

    name: str
    label: str


# the order is the screen's: the first is the default.
PLAIN_TEXT_SOURCES = (
    PlainTextSource(name=GENIUS_SOURCE, label="Genius"),
    PlainTextSource(name=YTMUSIC_SOURCE, label="YouTube Music"),
    PlainTextSource(name=LRCLIB_SOURCE, label="LRCLIB"),
)


@dataclass(frozen=True)
class LyricsLookupQuery:
    """`video_id` is the validated `youtubeMusic` id, `None` when the lookup has none."""

    track_id: int
    artist_names: tuple[str, ...]
    title: str
    duration_ms: int | None
    video_id: str | None


@dataclass(frozen=True)
class SourceAnswer:
    source: str
    result: LyricsResult


async def ask_source(
    source: str,
    track_id: int,
    fetch: Callable[[], Awaitable[LyricsResult]],
) -> LyricsResult:
    """a failing source answers `SOURCE_UNAVAILABLE`, logged by name; the soft limit and a
    database error pass through."""
    try:
        return await fetch()
    except (SoftTimeLimitExceeded, SQLAlchemyError):
        raise
    except Exception:
        logger.warning(
            "lyrics_source_failed",
            track_id=track_id,
            source=source,
            reason=LyricsOutcome.SOURCE_UNAVAILABLE.value,
            exc_info=True,
        )
        return LyricsResult(outcome=LyricsOutcome.SOURCE_UNAVAILABLE)


async def ask_youtube_music(cache: SourceCache, query: LyricsLookupQuery) -> LyricsResult | None:
    if query.video_id is None:
        return None
    return await ask_source(
        YTMUSIC_SOURCE,
        query.track_id,
        partial(ytmusic_lyrics.fetch_lyrics, cache, video_id=query.video_id),
    )


async def ask_lrclib(
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsLookupQuery,
) -> LyricsResult:
    return await ask_source(
        LRCLIB_SOURCE,
        query.track_id,
        partial(
            fetch_lrclib_lyrics,
            http_client,
            cache,
            artist_names=query.artist_names,
            title=query.title,
            duration_ms=query.duration_ms,
        ),
    )


def lines_to_store(result: LyricsResult) -> list[LyricsLine]:
    """synced gives its timed lines, unsynced its plain ones, any other answer none."""
    if result.outcome is LyricsOutcome.SYNCED:
        return [line for line in result.lines if line.start_ms is not None]
    if result.outcome is LyricsOutcome.UNSYNCED:
        return [LyricsLine(start_ms=None, text=line.text) for line in result.lines]
    return []


def fits_stored_text_limits(line_count: int, character_count: int) -> bool:
    return line_count <= MAX_LYRIC_LINES and character_count <= MAX_LYRICS_TOTAL_CHARACTERS


def is_storable(result: LyricsResult) -> bool:
    """what the writer would write, decided without logging: it has lines and fits the limits."""
    lines = lines_to_store(result)
    characters = sum(min(len(line.text), MAX_LYRIC_TEXT_LENGTH) for line in lines)
    return bool(lines) and fits_stored_text_limits(len(lines), characters)


def has_usable_timed_text(result: LyricsResult | None) -> bool:
    return result is not None and result.outcome is LyricsOutcome.SYNCED and is_storable(result)


def choose_timed_answer(youtube: LyricsResult | None, lrclib: LyricsResult | None) -> SourceAnswer | None:
    """the first usable timed text: YouTube Music, then LRCLIB."""
    for source, result in ((YTMUSIC_SOURCE, youtube), (LRCLIB_SOURCE, lrclib)):
        if result is not None and has_usable_timed_text(result):
            return SourceAnswer(source, result)
    return None


def choose_answer(youtube: LyricsResult | None, lrclib: LyricsResult | None) -> SourceAnswer | None:
    """timed text first (YouTube Music, then LRCLIB), then plain; one not storable is skipped."""
    timed = choose_timed_answer(youtube, lrclib)
    if timed is not None:
        return timed

    answers = [
        SourceAnswer(source, result)
        for source, result in ((YTMUSIC_SOURCE, youtube), (LRCLIB_SOURCE, lrclib))
        if result is not None
    ]
    for answer in answers:
        if answer.result.outcome is LyricsOutcome.UNSYNCED and is_storable(answer.result):
            return answer
    return None


def no_text_reason(youtube: LyricsResult | None, lrclib: LyricsResult | None) -> str:
    """absent only when every source asked said so; a failure, then a too-long text, outrank it."""
    # the render always passes the validated `youtubeMusic` id: `(None, ABSENT)` is not seen there.
    asked = [result for result in (youtube, lrclib) if result is not None]
    if lrclib is not None and lrclib.outcome in (
        LyricsOutcome.INSTRUMENTAL,
        LyricsOutcome.REJECTED_BY_MATCHING,
    ):
        return lrclib.outcome.value
    if any(result.outcome is LyricsOutcome.SOURCE_UNAVAILABLE for result in asked):
        return LyricsOutcome.SOURCE_UNAVAILABLE.value
    if any(result.lines for result in asked):
        return LYRICS_OVER_LIMITS
    return lrclib.outcome.value if lrclib is not None else LyricsOutcome.ABSENT.value


def timed_miss_reason(youtube: LyricsResult | None, lrclib: LyricsResult | None) -> str:
    """why no timed text was chosen: a failure outranks a text too long to keep, which outranks
    a plain miss. Plain text found on the way is not a reason."""
    asked = [result for result in (youtube, lrclib) if result is not None]
    if any(result.outcome is LyricsOutcome.SOURCE_UNAVAILABLE for result in asked):
        return LyricsOutcome.SOURCE_UNAVAILABLE.value
    if any(result.outcome is LyricsOutcome.SYNCED and result.lines for result in asked):
        return LYRICS_OVER_LIMITS
    return LyricsOutcome.ABSENT.value


async def ask_timed_sources(
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsLookupQuery,
) -> tuple[LyricsResult | None, LyricsResult | None]:
    """YouTube Music first; LRCLIB only when it gave no usable timed text. The one place the order is written."""
    youtube = await ask_youtube_music(cache, query)
    lrclib = None if has_usable_timed_text(youtube) else await ask_lrclib(http_client, cache, query)
    return youtube, lrclib


async def look_up_timed_text(
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsLookupQuery,
) -> tuple[SourceAnswer | None, str]:
    """the timed sources in order; the second value is the reason when there is no answer."""
    youtube, lrclib = await ask_timed_sources(http_client, cache, query)

    answer = choose_timed_answer(youtube, lrclib)
    if answer is not None:
        return answer, ""
    return None, timed_miss_reason(youtube, lrclib)


def plain_lines_of(result: LyricsResult) -> tuple[str, ...]:
    """the words of an answer without its times: spaces collapsed, empty lines and a lone note
    dropped. An answer that is neither synced nor unsynced has none."""
    if result.outcome not in (LyricsOutcome.SYNCED, LyricsOutcome.UNSYNCED):
        return ()
    texts = (" ".join(line.text.split()) for line in result.lines)
    return tuple(text for text in texts if text not in ("", ytmusic_lyrics.MUSIC_NOTE))


async def fetch_plain_text(
    source_name: str,
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsLookupQuery,
) -> LyricsResult:
    """one named source; a failure is `SOURCE_UNAVAILABLE`, not an exception. YouTube Music
    without a video id has nothing to ask about."""
    if source_name == GENIUS_SOURCE:
        return await ask_source(
            source_name,
            query.track_id,
            partial(
                genius.fetch_lyrics,
                http_client,
                cache,
                artist_names=query.artist_names,
                title=query.title,
                duration_ms=query.duration_ms,
            ),
        )
    if source_name == YTMUSIC_SOURCE:
        answer = await ask_youtube_music(cache, query)
        return answer if answer is not None else LyricsResult(outcome=LyricsOutcome.ABSENT)
    if source_name == LRCLIB_SOURCE:
        return await ask_lrclib(http_client, cache, query)
    raise ValueError(f"невідоме джерело простого тексту: {source_name}")
