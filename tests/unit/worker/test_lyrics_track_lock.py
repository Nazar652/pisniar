"""`lyrics.persist_under_track_lock` with a faked session: the track is locked
`FOR NO KEY UPDATE`, existing lines of any source win, and a write that lands short
raises instead of committing a partial set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.db.models import Track
from pisniar.resolve.sources.lrclib import LyricsOutcome
from pisniar.worker import lyrics as worker_lyrics
from pisniar.worker.lyrics import LyricRow, LyricsPersistError, LyricsPersistOutcome

TRACK_ID = 3
ROWS = [
    LyricRow(line_index=0, start_ms=1_000, text="перший"),
    LyricRow(line_index=1, start_ms=2_000, text="другий"),
]


def make_session(track: Track | None, existing_count: int, inserted_rows: int) -> MagicMock:
    session = MagicMock()
    session.get = AsyncMock(return_value=track)
    session.scalar = AsyncMock(return_value=existing_count)
    inserted = MagicMock()
    inserted.all.return_value = [(index,) for index in range(inserted_rows)]
    session.execute = AsyncMock(return_value=inserted)
    return session


async def persist(
    session: MagicMock, truncated_count: int = 0
) -> worker_lyrics.LyricsPersistResult:
    return await worker_lyrics.persist_under_track_lock(
        session, TRACK_ID, ROWS, LyricsOutcome.SYNCED, truncated_count
    )


async def test_the_track_is_locked_for_no_key_update() -> None:
    session = make_session(Track(), existing_count=0, inserted_rows=2)

    await persist(session)

    session.get.assert_awaited_once_with(Track, TRACK_ID, with_for_update={"key_share": True})


async def test_a_missing_track_has_nowhere_to_write() -> None:
    session = make_session(None, existing_count=0, inserted_rows=0)

    with pytest.raises(LyricsPersistError, match="не знайдено"):
        await persist(session)

    session.execute.assert_not_awaited()


async def test_existing_lines_of_any_source_mean_nothing_is_written() -> None:
    session = make_session(Track(), existing_count=5, inserted_rows=0)

    result = await persist(session)

    assert result.outcome is LyricsPersistOutcome.KEPT_EXISTING
    assert result.source_outcome is LyricsOutcome.SYNCED
    session.execute.assert_not_awaited()


async def test_all_rows_landing_is_a_written_result_with_the_counts() -> None:
    session = make_session(Track(), existing_count=0, inserted_rows=2)

    result = await persist(session, truncated_count=1)

    assert result.outcome is LyricsPersistOutcome.WRITTEN
    assert result.written_line_count == 2
    assert result.truncated_line_count == 1


async def test_a_write_that_lands_short_raises_instead_of_keeping_a_partial_set() -> None:
    session = make_session(Track(), existing_count=0, inserted_rows=1)

    with pytest.raises(LyricsPersistError, match="1 рядків із 2"):
        await persist(session)


async def test_the_insert_names_the_lrclib_source_and_skips_conflicts() -> None:
    session = make_session(Track(), existing_count=0, inserted_rows=2)

    await persist(session)

    statement = session.execute.await_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "ON CONFLICT" in compiled
    assert "DO NOTHING" in compiled
    parameters = statement.compile().params
    assert {value for key, value in parameters.items() if key.startswith("source")} == {"lrclib"}
