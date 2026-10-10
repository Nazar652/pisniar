"""The render's lyrics: stored lines first, LRCLIB only when there are none and no
human saved the text, with no session open during the request. Persistence is a
short transaction of its own, never over lines of any `source` or a saved text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import MAX_LYRIC_TEXT_LENGTH, Lyric, Track, renderable_lyrics_statement
from pisniar.db.session import session_scope
from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources.lrclib import LyricsOutcome, LyricsResult, fetch_lyrics
from pisniar.worker.ass import LyricLine

logger = get_logger(__name__)

LRCLIB_SOURCE = "lrclib"

# the reason class beside the adapter's own: the lines were already stored, so
# LRCLIB was never asked.
LYRICS_ALREADY_STORED = "lyrics_already_stored"
# timed lines exist, yet the fragment has none of them, so the `.ass` is empty.
LYRICS_OUTSIDE_FRAGMENT = "lyrics_outside_fragment"
# lines are stored but none is timed; the text was saved by a human, or found without times.
LYRICS_UNTIMED = "lyrics_untimed"
# a human saved an empty text: no source is asked.
LYRICS_REMOVED_BY_ADMIN = "lyrics_removed_by_admin"

# one sentence per class: "LRCLIB did not answer" and "LRCLIB has no text" call
# for different things from the admin.
NO_TEXT_NOTICES = {
    LyricsOutcome.ABSENT.value: "Тексту цього треку в LRCLIB немає — відео без тексту.",
    LyricsOutcome.UNSYNCED.value: "Текст знайдено, але без таймкодів — у відео його немає.",
    LyricsOutcome.INSTRUMENTAL.value: "Трек інструментальний — відео без тексту, так і має бути.",
    LyricsOutcome.SOURCE_UNAVAILABLE.value: "LRCLIB не відповів — відео зібрано без тексту.",
    LyricsOutcome.REJECTED_BY_MATCHING.value: (
        "Знайдений текст належить іншому треку — не використано."
    ),
    LYRICS_OUTSIDE_FRAGMENT: "Текст є, але жоден рядок не потрапляє у вибраний фрагмент.",
    LYRICS_UNTIMED: "Текст є, але ще не відстукано — відкрийте «Текст» у редакторі поста.",
    LYRICS_REMOVED_BY_ADMIN: "Текст прибрано вручну — відео без тексту.",
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


def rows_to_write(track_id: int, result: LyricsResult) -> tuple[list[LyricRow], int]:
    """a line past the CHECK is cut, not dropped: a `line_index` gap would read
    like a line deleted by hand."""
    if result.outcome is not LyricsOutcome.SYNCED:
        return [], 0

    rows: list[LyricRow] = []
    truncated_count = 0
    timed_lines = [line for line in result.lines if line.start_ms is not None]

    for line_index, line in enumerate(timed_lines):
        assert line.start_ms is not None
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

    return rows, truncated_count


async def persist_lyrics_in_own_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    track_id: int,
    result: LyricsResult,
) -> LyricsPersistResult:
    """the track lock makes the count guard atomic: the unique pair stops a
    duplicate index, not a mix of two writers' lines (or `lrclib` beside `manual`)."""
    rows, truncated_count = rows_to_write(track_id, result)

    if not rows:
        logger.info("lyrics_persist_nothing", track_id=track_id, source_outcome=result.outcome)
        return LyricsPersistResult(
            outcome=LyricsPersistOutcome.NOTHING_TO_WRITE, source_outcome=result.outcome
        )

    async with session_scope(session_factory) as session:
        persisted = await persist_under_track_lock(
            session, track_id, rows, result.outcome, truncated_count
        )

    return persisted


async def persist_under_track_lock(
    session: AsyncSession,
    track_id: int,
    rows: list[LyricRow],
    source_outcome: LyricsOutcome,
    truncated_count: int,
    source: str = LRCLIB_SOURCE,
) -> LyricsPersistResult:
    # FOR NO KEY UPDATE, the lock `persist_track_in_own_transaction` takes:
    # `INSERT INTO post` (FOR KEY SHARE) still passes. Any other lyric writer must take it too.
    # populate_existing: a track already in the identity map must not hand back a stale marker.
    track = await session.get(
        Track, track_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if track is None:
        raise LyricsPersistError(f"трек {track_id} не знайдено — текст нікуди писати")

    existing_count = await session.scalar(
        select(func.count()).select_from(Lyric).where(Lyric.track_id == track_id)
    )
    if existing_count or track.lyrics_edited_at is not None:
        logger.info(
            "lyrics_persist_kept_existing",
            track_id=track_id,
            existing=existing_count,
            edited_by_admin=track.lyrics_edited_at is not None,
        )
        return LyricsPersistResult(
            outcome=LyricsPersistOutcome.KEPT_EXISTING, source_outcome=source_outcome
        )

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
        raise LyricsPersistError(
            f"трек {track_id}: записано {inserted_count} рядків із {len(rows)}"
        )

    logger.info("lyrics_persisted", track_id=track_id, line_count=inserted_count)
    return LyricsPersistResult(
        outcome=LyricsPersistOutcome.WRITTEN,
        source_outcome=source_outcome,
        written_line_count=inserted_count,
        truncated_line_count=truncated_count,
    )


@dataclass(frozen=True)
class LyricsQuery:
    track_id: int
    artist_names: tuple[str, ...]
    title: str
    duration_ms: int | None


@dataclass(frozen=True)
class PreparedLyrics:
    """`reason` is a class, never text: it goes to the log and to the admin."""

    lines: tuple[LyricLine, ...]
    reason: str


@dataclass(frozen=True)
class StoredLyrics:
    """`total_count` counts every stored line, timed or not; the render sees only
    `renderable_lines`."""

    renderable_lines: tuple[LyricLine, ...]
    total_count: int
    edited_by_admin: bool


async def load_stored_lyrics(
    session_factory: async_sessionmaker[AsyncSession], track_id: int
) -> StoredLyrics:
    """plain values out of a session closed on return, so nothing lazily
    reloads once the long phase starts. REPEATABLE READ, so the rows, the count and the
    marker come from one snapshot."""
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        rows = (await session.scalars(renderable_lyrics_statement(track_id))).all()
        total_count = await session.scalar(
            select(func.count()).select_from(Lyric).where(Lyric.track_id == track_id)
        )
        edited_at = await session.scalar(select(Track.lyrics_edited_at).where(Track.id == track_id))
        return StoredLyrics(
            renderable_lines=tuple(
                LyricLine(line_index=row.line_index, start_ms=row.start_ms, text=row.text)
                for row in rows
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
    """a source that fails means a video without text. Our own failures (persist,
    read) raise, and so do timeouts and database errors raised under the fetch."""
    stored = await load_stored_lyrics(session_factory, query.track_id)
    stored_reason = reason_for_stored(stored)
    if stored_reason is not None:
        return build_prepared_lyrics(query.track_id, stored.renderable_lines, stored_reason)

    try:
        result = await fetch_lyrics(
            http_client,
            cache,
            artist_names=query.artist_names,
            title=query.title,
            duration_ms=query.duration_ms,
        )
    except (SoftTimeLimitExceeded, SQLAlchemyError):
        raise
    except Exception:
        logger.warning(
            "lyrics_source_failed",
            track_id=query.track_id,
            reason=LyricsOutcome.SOURCE_UNAVAILABLE.value,
            exc_info=True,
        )
        return build_prepared_lyrics(query.track_id, (), LyricsOutcome.SOURCE_UNAVAILABLE.value)

    persisted = await persist_lyrics_in_own_transaction(session_factory, query.track_id, result)
    reloaded = await load_stored_lyrics(session_factory, query.track_id)

    if persisted.outcome is LyricsPersistOutcome.KEPT_EXISTING:
        kept_reason = reason_for_stored(reloaded) or LYRICS_ALREADY_STORED
        return build_prepared_lyrics(query.track_id, reloaded.renderable_lines, kept_reason)
    return build_prepared_lyrics(query.track_id, reloaded.renderable_lines, result.outcome.value)


def reason_after_subtitles(
    track_id: int, prepared: PreparedLyrics, subtitles_text: str | None
) -> str:
    """decided where the `.ass` is built: only there is it known whether the
    fragment holds any of the lines."""
    if subtitles_text is not None or not prepared.lines:
        return prepared.reason

    logger.info(
        "lyrics_prepared",
        track_id=track_id,
        reason=LYRICS_OUTSIDE_FRAGMENT,
        line_count=len(prepared.lines),
    )
    return LYRICS_OUTSIDE_FRAGMENT


def no_text_notice(post_id: int, reason: str, text_version: str | None) -> str | None:
    """`None` while the video carries text: the subtitle digest decides that, not
    the reason, which only explains an absence."""
    if text_version is not None:
        return None
    return f"Пост {post_id}: {NO_TEXT_NOTICES.get(reason, UNEXPLAINED_NO_TEXT_NOTICE)}"


def build_prepared_lyrics(
    track_id: int, lines: tuple[LyricLine, ...], reason: str
) -> PreparedLyrics:
    logger.info("lyrics_prepared", track_id=track_id, reason=reason, line_count=len(lines))
    return PreparedLyrics(lines=lines, reason=reason)
