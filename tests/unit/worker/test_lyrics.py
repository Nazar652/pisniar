"""`worker.lyrics`, the database-free half: which lines are written (timed
ones only, D-96) and what happens to a line past the 500-character CHECK.
The lock and the guard are proven against Postgres in
`tests/integration/test_lyrics_persist.py`.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from pisniar.db.models import MAX_LYRIC_TEXT_LENGTH
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.worker import lyrics
from pisniar.worker.lyrics import LyricsPersistOutcome

TRACK_ID = 7


@pytest.fixture(autouse=True)
def persist_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A stand-in: a cached structlog proxy defeats `capture_logs()`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(lyrics, "logger", fake_logger)
    return fake_logger


def synced(*lines: tuple[int, str]) -> LyricsResult:
    return LyricsResult(
        outcome=LyricsOutcome.SYNCED,
        lines=tuple(LyricsLine(start_ms=start_ms, text=text) for start_ms, text in lines),
    )


def test_timed_lines_are_indexed_in_the_order_the_adapter_gave_them() -> None:
    rows, truncated = lyrics.rows_to_write(TRACK_ID, synced((1_000, "раз"), (2_500, "два")))

    assert [(row.line_index, row.start_ms, row.text) for row in rows] == [
        (0, 1_000, "раз"),
        (1, 2_500, "два"),
    ]
    assert truncated == 0


def test_a_pause_marker_is_kept_because_it_ends_the_line_before_it() -> None:
    rows, _ = lyrics.rows_to_write(TRACK_ID, synced((1_000, "раз"), (4_000, "")))

    assert [row.text for row in rows] == ["раз", ""]


@pytest.mark.parametrize(
    "outcome",
    [
        LyricsOutcome.UNSYNCED,
        LyricsOutcome.INSTRUMENTAL,
        LyricsOutcome.ABSENT,
        LyricsOutcome.REJECTED_BY_MATCHING,
    ],
)
def test_nothing_but_synced_lyrics_is_ever_written(outcome: LyricsOutcome) -> None:
    """D-96: untimed text stays a log class until step 7 drops NOT NULL."""
    result = LyricsResult(outcome=outcome, lines=(LyricsLine(start_ms=None, text="голий текст"),))

    assert lyrics.rows_to_write(TRACK_ID, result) == ([], 0)


def test_a_line_past_the_check_is_cut_to_it_and_logged_without_its_text(
    persist_logger: MagicMock,
) -> None:
    """Cut, not dropped: a gap in `line_index` would read in step 7's editor
    like a line deleted by hand (D-96)."""
    long_text = "й" * (MAX_LYRIC_TEXT_LENGTH + 37)

    rows, truncated = lyrics.rows_to_write(TRACK_ID, synced((1_000, "раз"), (2_000, long_text)))

    assert [row.line_index for row in rows] == [0, 1]
    assert len(rows[1].text) == MAX_LYRIC_TEXT_LENGTH
    assert truncated == 1

    persist_logger.warning.assert_called_once_with(
        "lyric_line_truncated",
        track_id=TRACK_ID,
        line_index=1,
        original_length=MAX_LYRIC_TEXT_LENGTH + 37,
    )
    logged_values = persist_logger.warning.call_args.kwargs.values()
    assert all(not isinstance(value, str) or "й" not in value for value in logged_values)


def test_a_line_exactly_at_the_limit_is_left_alone(persist_logger: MagicMock) -> None:
    text = "й" * MAX_LYRIC_TEXT_LENGTH

    rows, truncated = lyrics.rows_to_write(TRACK_ID, synced((1_000, text)))

    assert rows[0].text == text
    assert truncated == 0
    persist_logger.warning.assert_not_called()


async def test_a_result_with_nothing_to_write_never_opens_a_session() -> None:
    def refuse_session() -> None:
        raise AssertionError("no transaction may be opened for nothing")

    result = await lyrics.persist_lyrics_in_own_transaction(
        cast(Any, refuse_session),
        TRACK_ID,
        LyricsResult(outcome=LyricsOutcome.UNSYNCED),
    )

    assert result.outcome is LyricsPersistOutcome.NOTHING_TO_WRITE
    assert result.source_outcome is LyricsOutcome.UNSYNCED
