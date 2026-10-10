"""The render's lyrics: stored lines first, else the sources in order (YouTube Music, LRCLIB),
with no session open during a request. A write is a short transaction of its own, never over
lines of any `source` or a saved text; the plain-text lookup of other sources is a task.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import (
    MAX_LYRIC_LINES,
    MAX_LYRIC_TEXT_LENGTH,
    MAX_LYRICS_TOTAL_CHARACTERS,
    Lyric,
    Track,
    renderable_lyrics_statement,
)
from pisniar.db.session import session_scope
from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.plain_lyrics import PLAIN_LYRICS_SOURCES
from pisniar.resolve.sources import ytmusic_lyrics
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.resolve.sources.lrclib import fetch_lyrics as fetch_lrclib_lyrics
from pisniar.worker.ass import LyricLine

logger = get_logger(__name__)

LRCLIB_SOURCE = "lrclib"
YTMUSIC_SOURCE = ytmusic_lyrics.SOURCE_NAME

# no timed text exists and nothing is stored: only these reasons are worth a lookup of
# plain text; an instrumental track or a text that merely misses the fragment is not.
PLAIN_LOOKUP_REASONS = frozenset(
    {
        LyricsOutcome.ABSENT.value,
        LyricsOutcome.REJECTED_BY_MATCHING.value,
        LyricsOutcome.SOURCE_UNAVAILABLE.value,
    }
)

# the reason class beside the adapter's own: the lines were already stored, so LRCLIB was never asked.
LYRICS_ALREADY_STORED = "lyrics_already_stored"
# timed lines exist, yet the fragment has none of them, so the `.ass` is empty.
LYRICS_OUTSIDE_FRAGMENT = "lyrics_outside_fragment"
# lines are stored but none is timed; the text was saved by a human, or found without times.
LYRICS_UNTIMED = "lyrics_untimed"
# a human saved an empty text: no source is asked.
LYRICS_REMOVED_BY_ADMIN = "lyrics_removed_by_admin"
# a source had text, but too much of it to store, and no other source had any.
LYRICS_OVER_LIMITS = "lyrics_over_limits"

# one sentence per class: "LRCLIB did not answer" and "LRCLIB has no text" call for different things from the admin.
NO_TEXT_NOTICES = {
    LyricsOutcome.ABSENT.value: ("Тексту цього треку немає ні на YouTube Music, ні в LRCLIB — відео без тексту."),
    LyricsOutcome.UNSYNCED.value: "Текст знайдено, але без таймкодів — у відео його немає.",
    LyricsOutcome.INSTRUMENTAL.value: "Трек інструментальний — відео без тексту, так і має бути.",
    LyricsOutcome.SOURCE_UNAVAILABLE.value: ("Джерело тексту не відповіло — відео зібрано без тексту."),
    LyricsOutcome.REJECTED_BY_MATCHING.value: ("Знайдений текст належить іншому треку — не використано."),
    LYRICS_OUTSIDE_FRAGMENT: "Текст є, але жоден рядок не потрапляє у вибраний фрагмент.",
    LYRICS_UNTIMED: "Текст є, але ще не відстукано — відкрийте «Текст» у редакторі поста.",
    LYRICS_REMOVED_BY_ADMIN: "Текст прибрано вручну — відео без тексту.",
    LYRICS_OVER_LIMITS: "Знайдений текст завеликий, щоб його зберегти — відео без тексту.",
}
UNEXPLAINED_NO_TEXT_NOTICE = "Відео зібрано без тексту."


class LyricsPersistOutcome(StrEnum):
    WRITTEN = "written"
    KEPT_EXISTING = "kept_existing"
    NOTHING_TO_WRITE = "nothing_to_write"


class LyricsPersistError(RuntimeError):
    """the write did not land as a whole — never a partial set of lines."""


@dataclass(frozen=True)
class LyricsPersistResult:
    """`source_outcome` is the adapter's class, carried through so the admin can
    be told why nothing was written (e.g. `lyrics_unsynced`)."""

    outcome: LyricsPersistOutcome
    source_outcome: LyricsOutcome
    written_line_count: int = 0
    truncated_line_count: int = 0


@dataclass(frozen=True)
class LyricRow:
    line_index: int
    start_ms: int | None
    text: str


def lines_to_store(result: LyricsResult) -> list[LyricsLine]:
    """synced gives its timed lines, unsynced its plain ones, any other answer none."""
    if result.outcome is LyricsOutcome.SYNCED:
        return [line for line in result.lines if line.start_ms is not None]
    if result.outcome is LyricsOutcome.UNSYNCED:
        return [LyricsLine(start_ms=None, text=line.text) for line in result.lines]
    return []


def fits_stored_text_limits(line_count: int, character_count: int) -> bool:
    return line_count <= MAX_LYRIC_LINES and character_count <= MAX_LYRICS_TOTAL_CHARACTERS


def rows_to_write(track_id: int, result: LyricsResult) -> tuple[list[LyricRow], int]:
    """a long line is cut, not dropped; a text past the stored-text limits is not written."""
    lines = lines_to_store(result)
    rows: list[LyricRow] = []
    truncated_count = 0

    for line_index, line in enumerate(lines):
        text = line.text

        if len(text) > MAX_LYRIC_TEXT_LENGTH:
            # the length, never the text: a song line does not go to the log.
            logger.warning(
                "lyric_line_truncated",
                track_id=track_id,
                line_index=line_index,
                original_length=len(text),
            )
            text = text[:MAX_LYRIC_TEXT_LENGTH]
            truncated_count += 1

        rows.append(LyricRow(line_index=line_index, start_ms=line.start_ms, text=text))

    if not within_stored_text_limits(track_id, rows):
        return [], 0

    return rows, truncated_count


def within_stored_text_limits(track_id: int, rows: list[LyricRow]) -> bool:
    """a scraped source can hand back a book; the counts go to the log, the text never."""
    character_count = sum(len(row.text) for row in rows)
    if fits_stored_text_limits(len(rows), character_count):
        return True

    logger.warning(
        "lyrics_over_stored_text_limits",
        track_id=track_id,
        line_count=len(rows),
        character_count=character_count,
    )
    return False


async def persist_lyrics_in_own_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    track_id: int,
    result: LyricsResult,
    *,
    source: str,
) -> LyricsPersistResult:
    """the track lock makes the count guard atomic: the unique pair stops a
    duplicate index, not a mix of two writers' lines (or `lrclib` beside `manual`)."""
    rows, truncated_count = rows_to_write(track_id, result)

    if not rows:
        logger.info("lyrics_persist_nothing", track_id=track_id, source_outcome=result.outcome)
        return LyricsPersistResult(outcome=LyricsPersistOutcome.NOTHING_TO_WRITE, source_outcome=result.outcome)

    async with session_scope(session_factory) as session:
        persisted = await persist_under_track_lock(
            session, track_id, rows, result.outcome, truncated_count, source=source
        )

    return persisted


async def persist_under_track_lock(
    session: AsyncSession,
    track_id: int,
    rows: list[LyricRow],
    source_outcome: LyricsOutcome,
    truncated_count: int,
    *,
    source: str,
) -> LyricsPersistResult:
    # FOR NO KEY UPDATE, the lock `persist_track_in_own_transaction` takes:
    # `INSERT INTO post` (FOR KEY SHARE) still passes. Any other lyric writer must take it too.
    # populate_existing: a track already in the identity map must not hand back a stale marker.
    track = await session.get(Track, track_id, with_for_update={"key_share": True}, populate_existing=True)
    if track is None:
        raise LyricsPersistError(f"трек {track_id} не знайдено — текст нікуди писати")

    existing_count = await session.scalar(select(func.count()).select_from(Lyric).where(Lyric.track_id == track_id))
    if existing_count or track.lyrics_edited_at is not None:
        logger.info(
            "lyrics_persist_kept_existing",
            track_id=track_id,
            existing=existing_count,
            edited_by_admin=track.lyrics_edited_at is not None,
        )
        return LyricsPersistResult(outcome=LyricsPersistOutcome.KEPT_EXISTING, source_outcome=source_outcome)

    statement = (
        insert(Lyric)
        .values(
            [
                {
                    "track_id": track_id,
                    "line_index": row.line_index,
                    "start_ms": row.start_ms,
                    "text": row.text,
                    "source": source,
                }
                for row in rows
            ]
        )
        .on_conflict_do_nothing(index_elements=[Lyric.track_id, Lyric.line_index])
        .returning(Lyric.id)
    )
    inserted_count = len((await session.execute(statement)).all())

    # under the lock this cannot fall short; if it does, a writer skipped the
    # lock, and a partial set must roll back rather than commit quietly.
    if inserted_count != len(rows):
        raise LyricsPersistError(f"трек {track_id}: записано {inserted_count} рядків із {len(rows)}")

    logger.info("lyrics_persisted", track_id=track_id, line_count=inserted_count)
    return LyricsPersistResult(
        outcome=LyricsPersistOutcome.WRITTEN,
        source_outcome=source_outcome,
        written_line_count=inserted_count,
        truncated_line_count=truncated_count,
    )


@dataclass(frozen=True)
class LyricsQuery:
    """`video_id` is the validated `youtubeMusic` id, `None` when the lookup has none."""

    track_id: int
    artist_names: tuple[str, ...]
    title: str
    duration_ms: int | None
    video_id: str | None


@dataclass(frozen=True)
class PreparedLyrics:
    """`reason` is a class, never text: it goes to the log and to the admin."""

    lines: tuple[LyricLine, ...]
    reason: str


@dataclass(frozen=True)
class StoredLyrics:
    """`total_count` counts every stored line, timed or not; the render sees only `renderable_lines`."""

    renderable_lines: tuple[LyricLine, ...]
    total_count: int
    edited_by_admin: bool


async def load_stored_lyrics(session_factory: async_sessionmaker[AsyncSession], track_id: int) -> StoredLyrics:
    """plain values out of a session closed on return, read at REPEATABLE READ so the rows,
    the count and the marker are one snapshot."""
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        rows = (await session.scalars(renderable_lyrics_statement(track_id))).all()
        total_count = await session.scalar(select(func.count()).select_from(Lyric).where(Lyric.track_id == track_id))
        edited_at = await session.scalar(select(Track.lyrics_edited_at).where(Track.id == track_id))
        return StoredLyrics(
            renderable_lines=tuple(
                LyricLine(line_index=row.line_index, start_ms=row.start_ms, text=row.text) for row in rows
            ),
            total_count=total_count or 0,
            edited_by_admin=edited_at is not None,
        )


def reason_for_stored(stored: StoredLyrics) -> str | None:
    """`None` means nothing is stored and no human decided: a source may be asked."""
    if stored.renderable_lines:
        return LYRICS_ALREADY_STORED
    if stored.total_count > 0:
        return LYRICS_UNTIMED
    if stored.edited_by_admin:
        return LYRICS_REMOVED_BY_ADMIN
    return None


async def prepare_lyrics(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsQuery,
) -> PreparedLyrics:
    """a source that fails means a step skipped, at worst a video without text. Our own
    failures (persist, read) raise, and so do timeouts and database errors under a fetch."""
    stored = await load_stored_lyrics(session_factory, query.track_id)
    stored_reason = reason_for_stored(stored)
    if stored_reason is not None:
        return build_prepared_lyrics(query.track_id, stored.renderable_lines, stored_reason)

    youtube = await ask_youtube_music(cache, query)
    # usable timed YouTube Music text is the first truth: LRCLIB is not asked after it.
    lrclib = None if has_usable_timed_text(youtube) else await ask_lrclib(http_client, cache, query)

    chosen = choose_answer(youtube, lrclib)
    if chosen is None:
        return build_prepared_lyrics(query.track_id, (), no_text_reason(youtube, lrclib))

    persisted = await persist_lyrics_in_own_transaction(
        session_factory, query.track_id, chosen.result, source=chosen.source
    )
    reloaded = await load_stored_lyrics(session_factory, query.track_id)
    return build_prepared_lyrics(
        query.track_id,
        reloaded.renderable_lines,
        reason_after_persist(persisted, chosen.result, reloaded),
    )


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


async def ask_youtube_music(cache: SourceCache, query: LyricsQuery) -> LyricsResult | None:
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
    query: LyricsQuery,
) -> LyricsResult | None:
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


def is_storable(result: LyricsResult) -> bool:
    """what the writer would write, decided without logging: it has lines and fits the limits."""
    lines = lines_to_store(result)
    characters = sum(min(len(line.text), MAX_LYRIC_TEXT_LENGTH) for line in lines)
    return bool(lines) and fits_stored_text_limits(len(lines), characters)


def has_usable_timed_text(result: LyricsResult | None) -> bool:
    return result is not None and result.outcome is LyricsOutcome.SYNCED and is_storable(result)


def choose_answer(youtube: LyricsResult | None, lrclib: LyricsResult | None) -> SourceAnswer | None:
    """timed text first (YouTube Music, then LRCLIB), then plain; one not storable is skipped."""
    answers = [
        SourceAnswer(source, result)
        for source, result in ((YTMUSIC_SOURCE, youtube), (LRCLIB_SOURCE, lrclib))
        if result is not None
    ]
    for wanted in (LyricsOutcome.SYNCED, LyricsOutcome.UNSYNCED):
        for answer in answers:
            if answer.result.outcome is wanted and is_storable(answer.result):
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


def reason_after_persist(
    persisted: LyricsPersistResult,
    result: LyricsResult,
    reloaded: StoredLyrics,
) -> str:
    """what the table holds after the write decides the class, not what was found."""
    if persisted.outcome is LyricsPersistOutcome.KEPT_EXISTING:
        return reason_for_stored(reloaded) or LYRICS_ALREADY_STORED
    if persisted.outcome is LyricsPersistOutcome.WRITTEN and result.outcome is LyricsOutcome.UNSYNCED:
        return LYRICS_UNTIMED
    return result.outcome.value


def wants_plain_lookup(reason: str) -> bool:
    return reason in PLAIN_LOOKUP_REASONS


async def fetch_plain_lines(
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsQuery,
) -> SourceAnswer | None:
    """the first source with plain lines wins; one that fails or has none is passed over."""
    for source in PLAIN_LYRICS_SOURCES:
        result = await ask_source(
            source.name,
            query.track_id,
            partial(
                source.fetch,
                http_client,
                cache,
                artist_names=query.artist_names,
                title=query.title,
                duration_ms=query.duration_ms,
            ),
        )
        if result.outcome is LyricsOutcome.UNSYNCED and is_storable(result):
            return SourceAnswer(source.name, result)
    return None


async def store_plain_lyrics(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    cache: SourceCache,
    query: LyricsQuery,
) -> LyricsPersistResult | None:
    """three phases: read, then the network with no session open, then a short write
    under the track lock. `None` when something is already stored or nothing was found."""
    stored = await load_stored_lyrics(session_factory, query.track_id)
    stored_reason = reason_for_stored(stored)
    if stored_reason is not None:
        logger.info("plain_lyrics_skipped", track_id=query.track_id, reason=stored_reason)
        return None

    found = await fetch_plain_lines(http_client, cache, query)
    if found is None:
        logger.info("plain_lyrics_not_found", track_id=query.track_id)
        return None

    return await persist_lyrics_in_own_transaction(session_factory, query.track_id, found.result, source=found.source)


def reason_after_subtitles(
    track_id: int,
    prepared: PreparedLyrics,
    subtitles_text: str | None,
) -> str:
    """decided where the `.ass` is built: only there is it known whether the fragment holds any of the lines."""
    if subtitles_text is not None or not prepared.lines:
        return prepared.reason

    logger.info(
        "lyrics_prepared",
        track_id=track_id,
        reason=LYRICS_OUTSIDE_FRAGMENT,
        line_count=len(prepared.lines),
    )
    return LYRICS_OUTSIDE_FRAGMENT


def no_text_notice(
    post_id: int,
    reason: str,
    text_version: str | None,
) -> str | None:
    """`None` while the video carries text: the subtitle digest decides that, not
    the reason, which only explains an absence."""
    if text_version is not None:
        return None
    return f"Пост {post_id}: {NO_TEXT_NOTICES.get(reason, UNEXPLAINED_NO_TEXT_NOTICE)}"


def build_prepared_lyrics(
    track_id: int,
    lines: tuple[LyricLine, ...],
    reason: str,
) -> PreparedLyrics:
    logger.info("lyrics_prepared", track_id=track_id, reason=reason, line_count=len(lines))
    return PreparedLyrics(lines=lines, reason=reason)
