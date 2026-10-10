"""`worker.lyrics`, the database-free half: which lines are written, a line past the 500-character CHECK, and
`prepare_lyrics`' split between "no text" and a loud failure. The lock, the guard and the idle database are in
`test_lyrics_persist.py` and `test_render_lyrics.py`.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH, MAX_LYRICS_TOTAL_CHARACTERS
from pisniar.resolve.lrc import LrcParseError
from pisniar.resolve.sources.lrclib import LrclibApiError, LyricsLine, LyricsOutcome, LyricsResult
from pisniar.worker import lyrics
from pisniar.worker.ass import LyricLine
from pisniar.worker.lyrics import LyricsPersistOutcome

TRACK_ID = 7


@pytest.fixture(autouse=True)
def persist_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """a stand-in: a cached structlog proxy defeats `capture_logs()`."""
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
        LyricsOutcome.INSTRUMENTAL,
        LyricsOutcome.ABSENT,
        LyricsOutcome.REJECTED_BY_MATCHING,
    ],
)
def test_an_answer_that_is_neither_synced_nor_plain_writes_nothing(outcome: LyricsOutcome) -> None:
    result = LyricsResult(outcome=outcome, lines=(LyricsLine(start_ms=None, text="голий текст"),))

    assert lyrics.rows_to_write(TRACK_ID, result) == ([], 0)


def test_the_lines_to_store_are_decided_by_the_class_of_the_answer() -> None:
    mixed = (LyricsLine(start_ms=None, text="a"), LyricsLine(start_ms=5, text="b"))

    synced_lines = lyrics.lines_to_store(LyricsResult(LyricsOutcome.SYNCED, mixed))
    plain_lines = lyrics.lines_to_store(LyricsResult(LyricsOutcome.UNSYNCED, mixed))

    assert [(line.start_ms, line.text) for line in synced_lines] == [(5, "b")]
    assert [(line.start_ms, line.text) for line in plain_lines] == [(None, "a"), (None, "b")]
    assert lyrics.lines_to_store(LyricsResult(LyricsOutcome.ABSENT, mixed)) == []


@pytest.mark.parametrize(
    ("line_count", "character_count", "fits"),
    [
        (MAX_LYRIC_LINES, MAX_LYRICS_TOTAL_CHARACTERS, True),
        (MAX_LYRIC_LINES + 1, 1, False),
        (1, MAX_LYRICS_TOTAL_CHARACTERS + 1, False),
        (0, 0, True),
    ],
)
def test_the_stored_text_limits_are_inclusive(
    line_count: int,
    character_count: int,
    fits: bool,
) -> None:
    assert lyrics.fits_stored_text_limits(line_count, character_count) is fits


def test_plain_lines_are_written_without_a_time() -> None:
    result = LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=(LyricsLine(start_ms=None, text="раз"), LyricsLine(start_ms=None, text="два")),
    )

    rows, truncated = lyrics.rows_to_write(TRACK_ID, result)

    assert [(row.line_index, row.start_ms, row.text) for row in rows] == [
        (0, None, "раз"),
        (1, None, "два"),
    ]
    assert truncated == 0


def test_a_synced_answer_leaves_its_untimed_lines_out() -> None:
    result = LyricsResult(
        outcome=LyricsOutcome.SYNCED,
        lines=(
            LyricsLine(start_ms=None, text="без часу"),
            LyricsLine(start_ms=500, text="з часом"),
        ),
    )

    rows, _ = lyrics.rows_to_write(TRACK_ID, result)

    assert [(row.start_ms, row.text) for row in rows] == [(500, "з часом")]


def test_a_text_within_the_limits_passes(persist_logger: MagicMock) -> None:
    rows = [lyrics.LyricRow(line_index=index, start_ms=None, text="р") for index in range(300)]

    assert lyrics.within_stored_text_limits(TRACK_ID, rows) is True
    persist_logger.warning.assert_not_called()


def test_too_many_lines_are_not_written_and_the_log_has_counts_only(
    persist_logger: MagicMock,
) -> None:
    result = LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=tuple(LyricsLine(start_ms=None, text="книга") for _ in range(MAX_LYRIC_LINES + 1)),
    )

    assert lyrics.rows_to_write(TRACK_ID, result) == ([], 0)
    persist_logger.warning.assert_called_once_with(
        "lyrics_over_stored_text_limits",
        track_id=TRACK_ID,
        line_count=MAX_LYRIC_LINES + 1,
        character_count=5 * (MAX_LYRIC_LINES + 1),
    )


def test_too_many_characters_are_not_written() -> None:
    long_line = "й" * MAX_LYRIC_TEXT_LENGTH
    line_count = MAX_LYRICS_TOTAL_CHARACTERS // MAX_LYRIC_TEXT_LENGTH + 1
    result = LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=tuple(LyricsLine(start_ms=None, text=long_line) for _ in range(line_count)),
    )

    assert lyrics.rows_to_write(TRACK_ID, result) == ([], 0)


def test_a_line_past_the_check_is_cut_to_it_and_logged_without_its_text(
    persist_logger: MagicMock,
) -> None:
    """cut, not dropped: a gap in `line_index` would read like a line deleted by hand."""
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
        source="lrclib",
    )

    assert result.outcome is LyricsPersistOutcome.NOTHING_TO_WRITE
    assert result.source_outcome is LyricsOutcome.UNSYNCED


QUERY = lyrics.LyricsQuery(
    track_id=TRACK_ID,
    artist_names=("Burned Time Machine",),
    title="Покаяння",
    duration_ms=212_000,
    video_id=None,
)
STORED_LINES = (LyricLine(line_index=0, start_ms=1_000, text="раз"),)
NOTHING_STORED = lyrics.StoredLyrics(renderable_lines=(), total_count=0, edited_by_admin=False)
TIMED_STORED = lyrics.StoredLyrics(renderable_lines=STORED_LINES, total_count=1, edited_by_admin=False)
UNTIMED_STORED = lyrics.StoredLyrics(renderable_lines=(), total_count=3, edited_by_admin=False)
REMOVED_STORED = lyrics.StoredLyrics(renderable_lines=(), total_count=0, edited_by_admin=True)


class PrepareSpy:
    """replaces the three collaborators `prepare_lyrics` orchestrates and records the order they ran in, so
    "no network when lines exist" is observed."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stored_before: lyrics.StoredLyrics,
        stored_after: lyrics.StoredLyrics = NOTHING_STORED,
        fetch_result: LyricsResult | BaseException | None = None,
        persist_outcome: LyricsPersistOutcome = LyricsPersistOutcome.WRITTEN,
    ) -> None:
        self.calls: list[str] = []
        self.stored = [stored_before, stored_after]
        self.fetch_result = fetch_result
        self.persist_outcome = persist_outcome

        monkeypatch.setattr(lyrics, "load_stored_lyrics", self.load)
        monkeypatch.setattr(lyrics, "fetch_lrclib_lyrics", self.fetch)
        monkeypatch.setattr(lyrics, "persist_lyrics_in_own_transaction", self.persist)

    async def load(self, session_factory: object, track_id: int) -> lyrics.StoredLyrics:
        self.calls.append("load")
        return self.stored.pop(0)

    async def fetch(self, *args: object, **kwargs: object) -> LyricsResult:
        self.calls.append("fetch")
        if isinstance(self.fetch_result, BaseException):
            raise self.fetch_result
        assert self.fetch_result is not None
        return self.fetch_result

    async def persist(
        self,
        session_factory: object,
        track_id: int,
        result: LyricsResult,
        *,
        source: str,
    ) -> lyrics.LyricsPersistResult:
        self.calls.append("persist")
        return lyrics.LyricsPersistResult(outcome=self.persist_outcome, source_outcome=result.outcome)


async def prepare() -> lyrics.PreparedLyrics:
    return await lyrics.prepare_lyrics(MagicMock(), MagicMock(), MagicMock(), QUERY)


async def test_stored_lines_are_used_and_lrclib_is_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = PrepareSpy(monkeypatch, stored_before=TIMED_STORED)

    prepared = await prepare()

    assert spy.calls == ["load"]
    assert prepared == lyrics.PreparedLyrics(lines=STORED_LINES, reason=lyrics.LYRICS_ALREADY_STORED)


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        (TIMED_STORED, lyrics.LYRICS_ALREADY_STORED),
        (UNTIMED_STORED, lyrics.LYRICS_UNTIMED),
        (REMOVED_STORED, lyrics.LYRICS_REMOVED_BY_ADMIN),
        (
            lyrics.StoredLyrics(renderable_lines=STORED_LINES, total_count=4, edited_by_admin=True),
            lyrics.LYRICS_ALREADY_STORED,
        ),
        (
            lyrics.StoredLyrics(renderable_lines=(), total_count=2, edited_by_admin=True),
            lyrics.LYRICS_UNTIMED,
        ),
    ],
)
async def test_stored_lines_or_a_saved_text_mean_no_network_and_a_class_each(
    monkeypatch: pytest.MonkeyPatch,
    stored: lyrics.StoredLyrics,
    reason: str,
) -> None:
    spy = PrepareSpy(monkeypatch, stored_before=stored)

    prepared = await prepare()

    assert spy.calls == ["load"]
    assert prepared == lyrics.PreparedLyrics(lines=stored.renderable_lines, reason=reason)


def test_nothing_stored_and_no_human_decision_leaves_the_sources_free() -> None:
    assert lyrics.reason_for_stored(NOTHING_STORED) is None


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        (TIMED_STORED, lyrics.LYRICS_ALREADY_STORED),
        (UNTIMED_STORED, lyrics.LYRICS_UNTIMED),
        (REMOVED_STORED, lyrics.LYRICS_REMOVED_BY_ADMIN),
    ],
)
def test_each_stored_state_has_its_own_reason(stored: lyrics.StoredLyrics, reason: str) -> None:
    assert lyrics.reason_for_stored(stored) == reason


@pytest.mark.parametrize(
    ("after", "reason"),
    [
        (TIMED_STORED, lyrics.LYRICS_ALREADY_STORED),
        (UNTIMED_STORED, lyrics.LYRICS_UNTIMED),
        (REMOVED_STORED, lyrics.LYRICS_REMOVED_BY_ADMIN),
    ],
)
async def test_a_writer_that_kept_what_was_there_reports_what_is_there(
    monkeypatch: pytest.MonkeyPatch,
    after: lyrics.StoredLyrics,
    reason: str,
) -> None:
    PrepareSpy(
        monkeypatch,
        stored_before=NOTHING_STORED,
        stored_after=after,
        fetch_result=synced((1_000, "інше")),
        persist_outcome=LyricsPersistOutcome.KEPT_EXISTING,
    )

    prepared = await prepare()

    assert prepared == lyrics.PreparedLyrics(lines=after.renderable_lines, reason=reason)


async def test_fresh_synced_lyrics_are_persisted_then_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rendered from what the table holds, not from the adapter's answer: the two
    differ once a line is truncated to the CHECK."""
    spy = PrepareSpy(
        monkeypatch,
        stored_before=NOTHING_STORED,
        stored_after=TIMED_STORED,
        fetch_result=synced((1_000, "раз")),
    )

    prepared = await prepare()

    assert spy.calls == ["load", "fetch", "persist", "load"]
    assert prepared == lyrics.PreparedLyrics(lines=STORED_LINES, reason="lyrics_synced")


async def test_lines_written_by_someone_else_meanwhile_count_as_already_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    PrepareSpy(
        monkeypatch,
        stored_before=NOTHING_STORED,
        stored_after=TIMED_STORED,
        fetch_result=synced((1_000, "інше")),
        persist_outcome=LyricsPersistOutcome.KEPT_EXISTING,
    )

    prepared = await prepare()

    assert prepared.reason == lyrics.LYRICS_ALREADY_STORED
    assert prepared.lines == STORED_LINES


@pytest.mark.parametrize(
    "outcome",
    [
        LyricsOutcome.UNSYNCED,
        LyricsOutcome.INSTRUMENTAL,
        LyricsOutcome.ABSENT,
        LyricsOutcome.REJECTED_BY_MATCHING,
    ],
)
async def test_an_answer_without_timed_lines_carries_its_own_class(
    monkeypatch: pytest.MonkeyPatch, outcome: LyricsOutcome
) -> None:
    PrepareSpy(monkeypatch, stored_before=NOTHING_STORED, fetch_result=LyricsResult(outcome=outcome))

    prepared = await prepare()

    assert prepared == lyrics.PreparedLyrics(lines=(), reason=outcome.value)


@pytest.mark.parametrize(
    "failure",
    [
        LrclibApiError("lrclib не відповів за 3 спроб: HTTP 503"),
        LrcParseError("не розпізнано 9 рядків із 10"),
        httpx.ConnectError("connection refused"),
    ],
)
async def test_a_source_that_fails_means_a_video_without_text(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    spy = PrepareSpy(monkeypatch, stored_before=NOTHING_STORED, fetch_result=failure)

    prepared = await prepare()

    assert spy.calls == ["load", "fetch"]
    assert prepared == lyrics.PreparedLyrics(lines=(), reason=LyricsOutcome.SOURCE_UNAVAILABLE.value)


@pytest.mark.parametrize(
    "failure",
    [SoftTimeLimitExceeded(), OperationalError("SELECT 1", {}, Exception("server closed"))],
)
async def test_a_timeout_or_a_database_error_under_the_fetch_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """the soft limit is reported as a timeout, and `SourceCache` talks to
    Postgres inside the fetch: neither is the source failing."""
    PrepareSpy(monkeypatch, stored_before=NOTHING_STORED, fetch_result=failure)

    with pytest.raises(type(failure)):
        await prepare()


async def test_a_failed_persist_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = PrepareSpy(monkeypatch, stored_before=NOTHING_STORED, fetch_result=synced((1_000, "раз")))

    async def refuse(*args: object, **kwargs: object) -> object:
        raise lyrics.LyricsPersistError("записано 0 рядків із 1")

    monkeypatch.setattr(lyrics, "persist_lyrics_in_own_transaction", refuse)

    with pytest.raises(lyrics.LyricsPersistError):
        await prepare()
    assert spy.calls == ["load", "fetch"]


async def test_a_failed_read_of_stored_lines_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(*args: object, **kwargs: object) -> object:
        raise OperationalError("SELECT lyric", {}, Exception("server closed"))

    monkeypatch.setattr(lyrics, "load_stored_lyrics", refuse)

    with pytest.raises(OperationalError):
        await prepare()


@pytest.mark.parametrize(
    ("reason", "sentence"),
    [
        (
            "lyrics_absent",
            "Тексту цього треку немає ні на YouTube Music, ні в LRCLIB — відео без тексту.",
        ),
        ("lyrics_unsynced", "Текст знайдено, але без таймкодів — у відео його немає."),
        ("lyrics_instrumental", "Трек інструментальний — відео без тексту, так і має бути."),
        ("lyrics_source_unavailable", "Джерело тексту не відповіло — відео зібрано без тексту."),
        (
            "lyrics_over_limits",
            "Знайдений текст завеликий, щоб його зберегти — відео без тексту.",
        ),
        (
            "lyrics_rejected_by_matching",
            "Знайдений текст належить іншому треку — не використано.",
        ),
        (
            "lyrics_outside_fragment",
            "Текст є, але жоден рядок не потрапляє у вибраний фрагмент.",
        ),
        (
            "lyrics_untimed",
            "Текст є, але ще не відстукано — відкрийте «Текст» у редакторі поста.",
        ),
        ("lyrics_removed_by_admin", "Текст прибрано вручну — відео без тексту."),
    ],
)
def test_every_reason_for_a_missing_text_has_its_own_sentence(reason: str, sentence: str) -> None:
    """ "LRCLIB did not answer" and "LRCLIB has nothing" ask different things of the
    admin, so no two classes may share a sentence."""
    assert lyrics.no_text_notice(42, reason, None) == f"Пост 42: {sentence}"


def test_no_two_reasons_share_a_sentence() -> None:
    sentences = list(lyrics.NO_TEXT_NOTICES.values())

    assert len(set(sentences)) == len(sentences)
    assert lyrics.UNEXPLAINED_NO_TEXT_NOTICE not in sentences


@pytest.mark.parametrize("reason", ["lyrics_synced", lyrics.LYRICS_ALREADY_STORED])
def test_a_video_with_text_gets_no_notice(reason: str) -> None:
    assert lyrics.no_text_notice(42, reason, "0123456789abcdef") is None


def test_a_video_with_text_gets_no_notice_whatever_the_reason_says() -> None:
    """the subtitle digest decides whether text is in the frames; the reason only explains an absence."""
    assert lyrics.no_text_notice(42, "lyrics_absent", "0123456789abcdef") is None


def test_a_video_without_text_is_reported_even_for_an_unforeseen_reason() -> None:
    assert lyrics.no_text_notice(42, "lyrics_synced", None) == (f"Пост 42: {lyrics.UNEXPLAINED_NO_TEXT_NOTICE}")


def test_lines_that_all_miss_the_fragment_are_a_class_of_their_own() -> None:
    prepared = lyrics.PreparedLyrics(lines=STORED_LINES, reason=lyrics.LYRICS_ALREADY_STORED)

    assert lyrics.reason_after_subtitles(TRACK_ID, prepared, None) == (lyrics.LYRICS_OUTSIDE_FRAGMENT)


def test_drawn_lines_keep_the_reason_they_came_with() -> None:
    prepared = lyrics.PreparedLyrics(lines=STORED_LINES, reason="lyrics_synced")

    assert lyrics.reason_after_subtitles(TRACK_ID, prepared, "[Script Info]\n") == "lyrics_synced"


def test_no_lines_at_all_keep_the_sources_reason() -> None:
    prepared = lyrics.PreparedLyrics(lines=(), reason="lyrics_absent")

    assert lyrics.reason_after_subtitles(TRACK_ID, prepared, None) == "lyrics_absent"
