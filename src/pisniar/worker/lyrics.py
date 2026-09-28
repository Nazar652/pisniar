"""Lyrics persistence in a short transaction of its own; the LRCLIB request runs
before it, never inside, so no lock is held across the network. Only timed
lines are written, and never over existing lines of any `source`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import MAX_LYRIC_TEXT_LENGTH, Lyric, Track
from pisniar.logging import get_logger
from pisniar.resolve.sources.lrclib import LyricsOutcome, LyricsResult

logger = get_logger(__name__)

LRCLIB_SOURCE = "lrclib"


class LyricsPersistOutcome(StrEnum):
    WRITTEN = "written"
    KEPT_EXISTING = "kept_existing"
    NOTHING_TO_WRITE = "nothing_to_write"


class LyricsPersistError(RuntimeError):
    """The write did not land as a whole — never a partial set of lines."""


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
    start_ms: int
    text: str


def rows_to_write(track_id: int, result: LyricsResult) -> tuple[list[LyricRow], int]:
    """A line past the CHECK is cut, not dropped: a `line_index` gap would read
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
            # The length, never the text: a song line does not go to the log.
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
    """The track lock makes the count guard atomic: the unique pair stops a
    duplicate index, not a mix of two writers' lines (or `lrclib` beside `manual`)."""
    rows, truncated_count = rows_to_write(track_id, result)

    if not rows:
        logger.info("lyrics_persist_nothing", track_id=track_id, source_outcome=result.outcome)
        return LyricsPersistResult(
            outcome=LyricsPersistOutcome.NOTHING_TO_WRITE, source_outcome=result.outcome
        )

    async with session_factory() as session:
        try:
            persisted = await _persist_under_track_lock(
                session, track_id, rows, result.outcome, truncated_count
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    return persisted


async def _persist_under_track_lock(
    session: AsyncSession,
    track_id: int,
    rows: list[LyricRow],
    source_outcome: LyricsOutcome,
    truncated_count: int,
) -> LyricsPersistResult:
    # FOR NO KEY UPDATE, the lock `_persist_track_in_own_transaction` takes:
    # `INSERT INTO post` (FOR KEY SHARE) still passes. Any other lyric writer must take it too.
    track = await session.get(Track, track_id, with_for_update={"key_share": True})
    if track is None:
        raise LyricsPersistError(f"трек {track_id} не знайдено — текст нікуди писати")

    existing_count = await session.scalar(
        select(func.count()).select_from(Lyric).where(Lyric.track_id == track_id)
    )
    if existing_count:
        logger.info("lyrics_persist_kept_existing", track_id=track_id, existing=existing_count)
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
                    "source": LRCLIB_SOURCE,
                }
                for row in rows
            ]
        )
        .on_conflict_do_nothing(index_elements=[Lyric.track_id, Lyric.line_index])
        .returning(Lyric.id)
    )
    inserted_count = len((await session.execute(statement)).all())

    # Under the lock this cannot fall short; if it does, a writer skipped the
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
