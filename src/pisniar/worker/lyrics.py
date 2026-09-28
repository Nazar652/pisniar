"""Lyrics persistence (4B.6): the adapter's finished lines into `lyric`, in a
short transaction of its own. The LRCLIB request happens before it, never
inside — holding a lock across the network is R2-3/D-54/R3-3.

Only timed lines are written (D-96); untimed text stays a log class until
step 7. The automaton never writes over existing lines of any `source`.
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
    """`source_outcome` is the adapter's class, carried through unchanged so
    4B.8 can name it (`lyrics_unsynced` writes nothing and says why)."""

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
    """Timed lines only, indexed in the adapter's (time) order. A line past
    the CHECK is cut to it rather than dropped: a gap in `line_index` would
    read in step 7's editor like a line deleted by hand (D-96)."""
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
    """Lock, guard, write, commit — nothing else, `_persist_track_in_own_
    transaction`'s pattern. The track lock is what makes the guard atomic:
    `UNIQUE (track_id, line_index)` stops a duplicate index, not a mixed set
    (writer A's lines 0-9 plus writer B's 10-11, or `lrclib` beside `manual`).
    Under READ COMMITTED the second writer's count runs after the first one's
    commit, so it sees those lines and writes nothing.
    """
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
    # `INSERT INTO post` (FOR KEY SHARE) still passes. Step 7 takes it too.
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
