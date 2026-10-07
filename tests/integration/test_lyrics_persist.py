"""`worker.lyrics` against Postgres: the guard ("any existing line, of any
`source`, means write nothing"), the 500-character CHECK, and the lock that
makes the guard atomic against a second writer of the same track.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import MAX_LYRIC_TEXT_LENGTH, Lyric, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.worker.lyrics import LyricsPersistOutcome, persist_lyrics_in_own_transaction

pytestmark = pytest.mark.integration

SYNCED_RESULT = LyricsResult(
    outcome=LyricsOutcome.SYNCED,
    lines=(
        LyricsLine(start_ms=12_500, text="Перший рядок"),
        LyricsLine(start_ms=17_200, text="Другий рядок"),
        LyricsLine(start_ms=21_000, text=""),
    ),
)


@pytest_asyncio.fixture
async def session_factory(
    migrated_test_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_test_database_url)
    yield build_session_factory(engine)
    await engine.dispose()


async def make_track(session_factory: async_sessionmaker[AsyncSession]) -> int:
    marker_url = f"https://example.test/lyrics/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.commit()
        return track.id


async def stored_lines(
    session_factory: async_sessionmaker[AsyncSession], track_id: int
) -> list[tuple[int, int, str, str]]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(Lyric).where(Lyric.track_id == track_id).order_by(Lyric.line_index)
        )
        return [(row.line_index, row.start_ms, row.text, row.source) for row in rows]


async def add_line(
    session_factory: async_sessionmaker[AsyncSession],
    track_id: int,
    *,
    source: str,
    text: str,
    line_index: int = 0,
) -> None:
    async with session_factory() as session:
        session.add(
            Lyric(track_id=track_id, line_index=line_index, start_ms=0, text=text, source=source)
        )
        await session.commit()


async def test_synced_lines_are_written_in_order_as_lrclib(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    track_id = await make_track(session_factory)

    result = await persist_lyrics_in_own_transaction(session_factory, track_id, SYNCED_RESULT)

    assert result.outcome is LyricsPersistOutcome.WRITTEN
    assert result.written_line_count == 3
    assert await stored_lines(session_factory, track_id) == [
        (0, 12_500, "Перший рядок", "lrclib"),
        (1, 17_200, "Другий рядок", "lrclib"),
        (2, 21_000, "", "lrclib"),
    ]


async def test_an_existing_automatic_text_is_never_written_over(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    track_id = await make_track(session_factory)
    await add_line(session_factory, track_id, source="lrclib", text="старий рядок")

    result = await persist_lyrics_in_own_transaction(session_factory, track_id, SYNCED_RESULT)

    assert result.outcome is LyricsPersistOutcome.KEPT_EXISTING
    assert await stored_lines(session_factory, track_id) == [(0, 0, "старий рядок", "lrclib")]


async def test_a_manual_text_is_never_written_over(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """the guard is "any line", not "any `lrclib` line": one narrowed to `lrclib`
    would insert 0-2 beside the manual line at index 5 without a single conflict."""
    track_id = await make_track(session_factory)
    await add_line(session_factory, track_id, source="manual", text="правка адміна", line_index=5)

    result = await persist_lyrics_in_own_transaction(session_factory, track_id, SYNCED_RESULT)

    assert result.outcome is LyricsPersistOutcome.KEPT_EXISTING
    assert await stored_lines(session_factory, track_id) == [(5, 0, "правка адміна", "manual")]


async def test_a_line_past_the_check_is_stored_cut_instead_of_failing_the_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    track_id = await make_track(session_factory)
    long_result = LyricsResult(
        outcome=LyricsOutcome.SYNCED,
        lines=(LyricsLine(start_ms=1_000, text="й" * (MAX_LYRIC_TEXT_LENGTH + 1)),),
    )

    result = await persist_lyrics_in_own_transaction(session_factory, track_id, long_result)

    assert result.outcome is LyricsPersistOutcome.WRITTEN
    assert result.truncated_line_count == 1
    [(_, _, text, _)] = await stored_lines(session_factory, track_id)
    assert len(text) == MAX_LYRIC_TEXT_LENGTH


async def test_a_second_writer_waits_for_the_first_and_then_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """UNIQUE stops a duplicate index, not a mixed set; the track lock plus READ
    COMMITTED make the second writer count only after the first one commits."""
    track_id = await make_track(session_factory)

    async with session_factory() as first_writer:
        await first_writer.get(Track, track_id, with_for_update={"key_share": True})
        first_writer.add(
            Lyric(track_id=track_id, line_index=0, start_ms=0, text="перший", source="manual")
        )
        await first_writer.flush()

        second_writer = asyncio.create_task(
            persist_lyrics_in_own_transaction(session_factory, track_id, SYNCED_RESULT)
        )
        await asyncio.sleep(0.3)
        assert not second_writer.done(), "the second writer must be blocked on the track lock"

        await first_writer.commit()

    result = await asyncio.wait_for(second_writer, timeout=10)

    assert result.outcome is LyricsPersistOutcome.KEPT_EXISTING
    assert await stored_lines(session_factory, track_id) == [(0, 0, "перший", "manual")]
