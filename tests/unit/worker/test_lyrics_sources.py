"""`worker.lyrics`, the order of the sources: timed text first (YouTube Music, then
LRCLIB), plain text after it, the plain-text lookup of the remaining sources, and what
each step swallows or lets through. No network and no database: every collaborator is
replaced.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH, MAX_LYRICS_TOTAL_CHARACTERS
from pisniar.resolve.plain_lyrics import PlainLyricsSource
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.resolve.sources.ytmusic_lyrics import YtmusicLyricsApiError
from pisniar.worker import lyrics
from pisniar.worker.ass import LyricLine
from pisniar.worker.lyrics import LyricsPersistOutcome, SourceAnswer

TRACK_ID = 7
QUERY = lyrics.LyricsQuery(
    track_id=TRACK_ID,
    artist_names=("Burned Time Machine",),
    title="Покаяння",
    duration_ms=212_000,
    video_id="n4bPzdIs_nc",
)
NOTHING_STORED = lyrics.StoredLyrics(renderable_lines=(), total_count=0, edited_by_admin=False)
STORED_LINES = (LyricLine(line_index=0, start_ms=1_000, text="раз"),)
TIMED_STORED = lyrics.StoredLyrics(
    renderable_lines=STORED_LINES, total_count=1, edited_by_admin=False
)
UNTIMED_STORED = lyrics.StoredLyrics(renderable_lines=(), total_count=2, edited_by_admin=False)
SYNCED = LyricsResult(outcome=LyricsOutcome.SYNCED, lines=(LyricsLine(start_ms=1_000, text="раз"),))
OTHER_SYNCED = LyricsResult(
    outcome=LyricsOutcome.SYNCED, lines=(LyricsLine(start_ms=2_000, text="інакше"),)
)
PLAIN = LyricsResult(outcome=LyricsOutcome.UNSYNCED, lines=(LyricsLine(start_ms=None, text="раз"),))
OTHER_PLAIN = LyricsResult(
    outcome=LyricsOutcome.UNSYNCED, lines=(LyricsLine(start_ms=None, text="інакше"),)
)
ABSENT = LyricsResult(outcome=LyricsOutcome.ABSENT)
OVER_LIMIT = LyricsResult(
    outcome=LyricsOutcome.SYNCED,
    lines=tuple(LyricsLine(start_ms=index, text="р") for index in range(MAX_LYRIC_LINES + 1)),
)
OVER_LIMIT_PLAIN = LyricsResult(
    outcome=LyricsOutcome.UNSYNCED,
    lines=tuple(LyricsLine(start_ms=None, text="р") for _ in range(MAX_LYRIC_LINES + 1)),
)


@pytest.fixture(autouse=True)
def silent_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_logger = MagicMock()
    monkeypatch.setattr(lyrics, "logger", fake_logger)
    return fake_logger


def test_timed_text_of_youtube_music_comes_first() -> None:
    assert lyrics.choose_answer(SYNCED, OTHER_SYNCED) == SourceAnswer("ytmusic", SYNCED)


def test_timed_lrclib_text_beats_plain_youtube_music_text() -> None:
    chosen = lyrics.choose_answer(PLAIN, OTHER_SYNCED)

    assert chosen == SourceAnswer("lrclib", OTHER_SYNCED)


def test_plain_text_follows_in_the_same_order() -> None:
    assert lyrics.choose_answer(PLAIN, OTHER_PLAIN) == SourceAnswer("ytmusic", PLAIN)
    assert lyrics.choose_answer(None, OTHER_PLAIN) == SourceAnswer("lrclib", OTHER_PLAIN)


def test_an_over_limit_answer_is_skipped_for_the_next_one() -> None:
    assert lyrics.choose_answer(OVER_LIMIT, OTHER_SYNCED) == SourceAnswer("lrclib", OTHER_SYNCED)
    assert lyrics.choose_answer(OVER_LIMIT, PLAIN) == SourceAnswer("lrclib", PLAIN)


def test_an_over_limit_timed_answer_is_not_usable_timed_text() -> None:
    assert lyrics.has_usable_timed_text(SYNCED) is True
    assert lyrics.has_usable_timed_text(OVER_LIMIT) is False
    assert lyrics.has_usable_timed_text(PLAIN) is False
    assert lyrics.has_usable_timed_text(None) is False


def test_choosing_and_checking_never_log_the_writer_logs_each_event_once(
    silent_logger: MagicMock,
) -> None:
    long_line = LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=(LyricsLine(start_ms=None, text="й" * (MAX_LYRIC_TEXT_LENGTH + 1)),),
    )

    assert lyrics.is_storable(OVER_LIMIT) is False
    assert lyrics.is_storable(long_line) is True
    assert lyrics.has_usable_timed_text(OVER_LIMIT) is False
    assert lyrics.choose_answer(OVER_LIMIT, long_line) is not None
    silent_logger.warning.assert_not_called()

    lyrics.rows_to_write(TRACK_ID, OVER_LIMIT)
    lyrics.rows_to_write(TRACK_ID, long_line)

    assert [call.args[0] for call in silent_logger.warning.call_args_list] == [
        "lyrics_over_stored_text_limits",
        "lyric_line_truncated",
    ]


def test_a_cut_line_still_counts_at_the_cut_length_against_the_limit() -> None:
    lines = tuple(
        LyricsLine(start_ms=None, text="й" * (MAX_LYRIC_TEXT_LENGTH * 2))
        for _ in range(MAX_LYRICS_TOTAL_CHARACTERS // MAX_LYRIC_TEXT_LENGTH)
    )

    assert lyrics.is_storable(LyricsResult(LyricsOutcome.UNSYNCED, lines)) is True
    assert lyrics.is_storable(LyricsResult(LyricsOutcome.UNSYNCED, (*lines, lines[0]))) is False


def test_only_an_answer_that_would_write_lines_is_storable() -> None:
    assert lyrics.is_storable(SYNCED) is True
    assert lyrics.is_storable(PLAIN) is True
    assert lyrics.is_storable(OVER_LIMIT) is False
    assert lyrics.is_storable(ABSENT) is False


@pytest.mark.parametrize(
    ("youtube", "lrclib"),
    [
        (None, None),
        (ABSENT, ABSENT),
        (LyricsResult(outcome=LyricsOutcome.INSTRUMENTAL), None),
        (LyricsResult(outcome=LyricsOutcome.SYNCED), LyricsResult(outcome=LyricsOutcome.UNSYNCED)),
    ],
)
def test_no_lines_means_no_answer(
    youtube: LyricsResult | None, lrclib: LyricsResult | None
) -> None:
    assert lyrics.choose_answer(youtube, lrclib) is None


UNAVAILABLE = LyricsResult(outcome=LyricsOutcome.SOURCE_UNAVAILABLE)
INSTRUMENTAL = LyricsResult(outcome=LyricsOutcome.INSTRUMENTAL)
REJECTED = LyricsResult(outcome=LyricsOutcome.REJECTED_BY_MATCHING)


@pytest.mark.parametrize(
    ("youtube", "lrclib", "reason"),
    [
        (ABSENT, ABSENT, "lyrics_absent"),
        (None, ABSENT, "lyrics_absent"),
        (UNAVAILABLE, ABSENT, "lyrics_source_unavailable"),
        (ABSENT, UNAVAILABLE, "lyrics_source_unavailable"),
        (UNAVAILABLE, UNAVAILABLE, "lyrics_source_unavailable"),
        (None, UNAVAILABLE, "lyrics_source_unavailable"),
        (UNAVAILABLE, INSTRUMENTAL, "lyrics_instrumental"),
        (ABSENT, REJECTED, "lyrics_rejected_by_matching"),
        (UNAVAILABLE, REJECTED, "lyrics_rejected_by_matching"),
        (OVER_LIMIT, ABSENT, "lyrics_over_limits"),
        (ABSENT, OVER_LIMIT, "lyrics_over_limits"),
        (OVER_LIMIT, UNAVAILABLE, "lyrics_source_unavailable"),
        (ABSENT, LyricsResult(outcome=LyricsOutcome.UNSYNCED), "lyrics_unsynced"),
    ],
)
def test_the_reason_for_no_text_is_absent_only_when_every_source_said_so(
    youtube: LyricsResult | None, lrclib: LyricsResult | None, reason: str
) -> None:
    assert lyrics.no_text_reason(youtube, lrclib) == reason


def test_a_kept_write_reports_what_the_table_holds() -> None:
    kept = lyrics.LyricsPersistResult(
        outcome=LyricsPersistOutcome.KEPT_EXISTING, source_outcome=LyricsOutcome.SYNCED
    )

    assert lyrics.reason_after_persist(kept, SYNCED, UNTIMED_STORED) == lyrics.LYRICS_UNTIMED
    assert lyrics.reason_after_persist(kept, SYNCED, TIMED_STORED) == lyrics.LYRICS_ALREADY_STORED
    assert lyrics.reason_after_persist(kept, SYNCED, NOTHING_STORED) == lyrics.LYRICS_ALREADY_STORED


def test_plain_lines_that_were_written_are_untimed_text() -> None:
    written = lyrics.LyricsPersistResult(
        outcome=LyricsPersistOutcome.WRITTEN, source_outcome=LyricsOutcome.UNSYNCED
    )

    assert lyrics.reason_after_persist(written, PLAIN, UNTIMED_STORED) == lyrics.LYRICS_UNTIMED


def test_synced_lines_that_were_written_keep_the_adapters_class() -> None:
    written = lyrics.LyricsPersistResult(
        outcome=LyricsPersistOutcome.WRITTEN, source_outcome=LyricsOutcome.SYNCED
    )

    assert lyrics.reason_after_persist(written, SYNCED, TIMED_STORED) == "lyrics_synced"


def test_plain_text_that_was_not_written_keeps_the_unsynced_class() -> None:
    nothing = lyrics.LyricsPersistResult(
        outcome=LyricsPersistOutcome.NOTHING_TO_WRITE, source_outcome=LyricsOutcome.UNSYNCED
    )

    assert lyrics.reason_after_persist(nothing, PLAIN, NOTHING_STORED) == "lyrics_unsynced"


@pytest.mark.parametrize(
    ("reason", "wanted"),
    [
        ("lyrics_absent", True),
        ("lyrics_rejected_by_matching", True),
        ("lyrics_source_unavailable", True),
        ("lyrics_instrumental", False),
        ("lyrics_unsynced", False),
        ("lyrics_synced", False),
        (lyrics.LYRICS_UNTIMED, False),
        (lyrics.LYRICS_REMOVED_BY_ADMIN, False),
        (lyrics.LYRICS_ALREADY_STORED, False),
        (lyrics.LYRICS_OUTSIDE_FRAGMENT, False),
    ],
)
def test_only_a_missing_or_unreachable_text_is_worth_a_plain_lookup(
    reason: str, wanted: bool
) -> None:
    assert lyrics.wants_plain_lookup(reason) is wanted


async def test_a_source_that_answers_is_passed_through() -> None:
    assert await lyrics.ask_source("x", TRACK_ID, AsyncMock(return_value=SYNCED)) is SYNCED


@pytest.mark.parametrize(
    "failure",
    [YtmusicLyricsApiError("форма"), httpx.ConnectError("refused"), TimeoutError()],
)
async def test_a_source_that_fails_is_an_unavailable_answer_logged_by_name(
    failure: Exception, silent_logger: MagicMock
) -> None:
    answer = await lyrics.ask_source("ytmusic", TRACK_ID, AsyncMock(side_effect=failure))

    assert answer == LyricsResult(outcome=LyricsOutcome.SOURCE_UNAVAILABLE)

    silent_logger.warning.assert_called_once()
    assert silent_logger.warning.call_args.kwargs["source"] == "ytmusic"


@pytest.mark.parametrize(
    "failure",
    [SoftTimeLimitExceeded(), OperationalError("SELECT 1", {}, Exception("server closed"))],
)
async def test_the_soft_limit_and_a_database_error_pass_through(failure: Exception) -> None:
    with pytest.raises(type(failure)):
        await lyrics.ask_source("x", TRACK_ID, AsyncMock(side_effect=failure))


async def test_youtube_music_is_asked_with_the_video_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock(return_value=SYNCED)
    monkeypatch.setattr(lyrics.ytmusic_lyrics, "fetch_lyrics", fetch)
    cache = MagicMock()

    assert await lyrics.ask_youtube_music(cache, QUERY) is SYNCED
    fetch.assert_awaited_once_with(cache, video_id="n4bPzdIs_nc")


async def test_without_a_video_id_youtube_music_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(lyrics.ytmusic_lyrics, "fetch_lyrics", fetch)

    assert await lyrics.ask_youtube_music(MagicMock(), replace(QUERY, video_id=None)) is None
    fetch.assert_not_awaited()


async def test_lrclib_is_asked_with_the_track_as_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock(return_value=ABSENT)
    monkeypatch.setattr(lyrics, "fetch_lrclib_lyrics", fetch)
    client, cache = MagicMock(), MagicMock()

    assert await lyrics.ask_lrclib(client, cache, QUERY) is ABSENT
    fetch.assert_awaited_once_with(
        client,
        cache,
        artist_names=("Burned Time Machine",),
        title="Покаяння",
        duration_ms=212_000,
    )


class LadderSpy:
    """replaces the five collaborators `prepare_lyrics` orchestrates and records the
    order and the `source` of the write, so "who is asked, who is written" is observed."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        youtube: LyricsResult | BaseException | None,
        lrclib: LyricsResult | BaseException | None,
        stored_after: lyrics.StoredLyrics = TIMED_STORED,
        persist_outcome: LyricsPersistOutcome = LyricsPersistOutcome.WRITTEN,
    ) -> None:
        self.calls: list[str] = []
        self.written_source: str | None = None
        self.answers = {"ytmusic": youtube, "lrclib": lrclib}
        self.stored = [NOTHING_STORED, stored_after]
        self.persist_outcome = persist_outcome

        monkeypatch.setattr(lyrics, "load_stored_lyrics", self.load)
        monkeypatch.setattr(lyrics.ytmusic_lyrics, "fetch_lyrics", self.ask("ytmusic"))
        monkeypatch.setattr(lyrics, "fetch_lrclib_lyrics", self.ask("lrclib"))
        monkeypatch.setattr(lyrics, "persist_lyrics_in_own_transaction", self.persist)

    async def load(self, session_factory: object, track_id: int) -> lyrics.StoredLyrics:
        self.calls.append("load")
        return self.stored.pop(0)

    def ask(self, name: str) -> AsyncMock:
        async def answer(*args: object, **kwargs: object) -> LyricsResult:
            self.calls.append(name)
            outcome = self.answers[name]
            if isinstance(outcome, BaseException):
                raise outcome
            assert outcome is not None
            return outcome

        return AsyncMock(side_effect=answer)

    async def persist(
        self, session_factory: object, track_id: int, result: LyricsResult, *, source: str
    ) -> lyrics.LyricsPersistResult:
        self.calls.append("persist")
        self.written_source = source
        return lyrics.LyricsPersistResult(
            outcome=self.persist_outcome, source_outcome=result.outcome
        )


async def prepare(query: lyrics.LyricsQuery = QUERY) -> lyrics.PreparedLyrics:
    return await lyrics.prepare_lyrics(MagicMock(), MagicMock(), MagicMock(), query)


async def test_timed_youtube_music_text_is_written_and_lrclib_is_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=SYNCED, lrclib=None)

    prepared = await prepare()

    assert spy.calls == ["load", "ytmusic", "persist", "load"]
    assert spy.written_source == "ytmusic"
    assert prepared == lyrics.PreparedLyrics(lines=STORED_LINES, reason="lyrics_synced")


async def test_lrclib_is_asked_when_youtube_music_has_no_timed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=ABSENT, lrclib=OTHER_SYNCED)

    prepared = await prepare()

    assert spy.calls == ["load", "ytmusic", "lrclib", "persist", "load"]
    assert spy.written_source == "lrclib"
    assert prepared.reason == "lyrics_synced"


async def test_a_failed_youtube_music_call_falls_through_to_lrclib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=YtmusicLyricsApiError("форма"), lrclib=OTHER_SYNCED)

    prepared = await prepare()

    assert spy.written_source == "lrclib"
    assert prepared.reason == "lyrics_synced"


async def test_without_a_video_id_only_lrclib_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = LadderSpy(monkeypatch, youtube=None, lrclib=OTHER_SYNCED)

    await prepare(replace(QUERY, video_id=None))

    assert spy.calls == ["load", "lrclib", "persist", "load"]


async def test_plain_text_is_stored_without_times_and_explained_as_untimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=PLAIN, lrclib=ABSENT, stored_after=UNTIMED_STORED)

    prepared = await prepare()

    assert spy.written_source == "ytmusic"
    assert prepared == lyrics.PreparedLyrics(lines=(), reason=lyrics.LYRICS_UNTIMED)


async def test_lrclib_plain_text_is_stored_when_youtube_music_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=ABSENT, lrclib=PLAIN, stored_after=UNTIMED_STORED)

    prepared = await prepare()

    assert spy.written_source == "lrclib"
    assert prepared.reason == lyrics.LYRICS_UNTIMED


async def test_nothing_found_carries_lrclibs_class_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=ABSENT, lrclib=ABSENT)

    prepared = await prepare()

    assert spy.calls == ["load", "ytmusic", "lrclib"]
    assert prepared == lyrics.PreparedLyrics(lines=(), reason="lyrics_absent")


async def test_every_source_failing_is_an_unavailable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    LadderSpy(monkeypatch, youtube=TimeoutError(), lrclib=httpx.ConnectError("refused"))

    prepared = await prepare()

    assert prepared == lyrics.PreparedLyrics(lines=(), reason="lyrics_source_unavailable")


@pytest.mark.parametrize(
    ("youtube", "lrclib", "reason"),
    [
        (YtmusicLyricsApiError("форма"), ABSENT, "lyrics_source_unavailable"),
        (ABSENT, httpx.ConnectError("refused"), "lyrics_source_unavailable"),
        (ABSENT, ABSENT, "lyrics_absent"),
        (TimeoutError(), httpx.ConnectError("refused"), "lyrics_source_unavailable"),
    ],
)
async def test_absent_needs_every_source_to_have_answered_absent(
    monkeypatch: pytest.MonkeyPatch,
    youtube: LyricsResult | BaseException,
    lrclib: LyricsResult | BaseException,
    reason: str,
) -> None:
    LadderSpy(monkeypatch, youtube=youtube, lrclib=lrclib)

    prepared = await prepare()

    assert prepared == lyrics.PreparedLyrics(lines=(), reason=reason)


async def test_an_over_limit_youtube_music_answer_falls_through_to_lrclib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=OVER_LIMIT, lrclib=OTHER_SYNCED)

    prepared = await prepare()

    assert spy.calls == ["load", "ytmusic", "lrclib", "persist", "load"]
    assert spy.written_source == "lrclib"
    assert prepared.reason == "lyrics_synced"


async def test_text_too_long_to_store_from_every_source_has_a_class_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LadderSpy(monkeypatch, youtube=OVER_LIMIT, lrclib=ABSENT)

    prepared = await prepare()

    assert spy.written_source is None
    assert prepared == lyrics.PreparedLyrics(lines=(), reason=lyrics.LYRICS_OVER_LIMITS)


async def test_a_timeout_under_youtube_music_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    LadderSpy(monkeypatch, youtube=SoftTimeLimitExceeded(), lrclib=None)

    with pytest.raises(SoftTimeLimitExceeded):
        await prepare()


def plain_source(name: str, outcome: LyricsResult | BaseException) -> PlainLyricsSource:
    async def fetch(*args: object, **kwargs: object) -> LyricsResult:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return PlainLyricsSource(name=name, fetch=fetch)


async def test_the_first_plain_source_with_lines_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lyrics,
        "PLAIN_LYRICS_SOURCES",
        (
            plain_source("first", ABSENT),
            plain_source("second", PLAIN),
            plain_source("third", OTHER_PLAIN),
        ),
    )

    found = await lyrics.fetch_plain_lines(MagicMock(), MagicMock(), QUERY)

    assert found == SourceAnswer("second", PLAIN)


async def test_an_over_limit_plain_answer_falls_through_to_the_next_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lyrics,
        "PLAIN_LYRICS_SOURCES",
        (plain_source("book", OVER_LIMIT_PLAIN), plain_source("song", PLAIN)),
    )

    found = await lyrics.fetch_plain_lines(MagicMock(), MagicMock(), QUERY)

    assert found == SourceAnswer("song", PLAIN)


async def test_a_failing_plain_source_is_passed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lyrics,
        "PLAIN_LYRICS_SOURCES",
        (plain_source("broken", httpx.ConnectError("refused")), plain_source("fine", PLAIN)),
    )

    found = await lyrics.fetch_plain_lines(MagicMock(), MagicMock(), QUERY)

    assert found == SourceAnswer("fine", PLAIN)


async def test_plain_sources_with_nothing_to_offer_find_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lyrics,
        "PLAIN_LYRICS_SOURCES",
        (plain_source("a", ABSENT), plain_source("b", LyricsResult(LyricsOutcome.UNSYNCED))),
    )

    assert await lyrics.fetch_plain_lines(MagicMock(), MagicMock(), QUERY) is None


@pytest.mark.parametrize(
    "failure",
    [SoftTimeLimitExceeded(), OperationalError("SELECT 1", {}, Exception("server closed"))],
)
async def test_the_soft_limit_and_a_database_error_pass_through_the_plain_lookup(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.setattr(lyrics, "PLAIN_LYRICS_SOURCES", (plain_source("a", failure),))

    with pytest.raises(type(failure)):
        await lyrics.fetch_plain_lines(MagicMock(), MagicMock(), QUERY)


class StoreSpy:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stored: lyrics.StoredLyrics,
        found: SourceAnswer | None,
    ) -> None:
        self.calls: list[str] = []
        self.written_source: str | None = None
        self.stored = stored
        self.found = found

        monkeypatch.setattr(lyrics, "load_stored_lyrics", self.load)
        monkeypatch.setattr(lyrics, "fetch_plain_lines", self.fetch)
        monkeypatch.setattr(lyrics, "persist_lyrics_in_own_transaction", self.persist)

    async def load(self, session_factory: object, track_id: int) -> lyrics.StoredLyrics:
        self.calls.append("load")
        return self.stored

    async def fetch(self, *args: object) -> SourceAnswer | None:
        self.calls.append("fetch")
        return self.found

    async def persist(
        self, session_factory: object, track_id: int, result: LyricsResult, *, source: str
    ) -> lyrics.LyricsPersistResult:
        self.calls.append("persist")
        self.written_source = source
        return lyrics.LyricsPersistResult(
            outcome=LyricsPersistOutcome.WRITTEN, source_outcome=result.outcome
        )


async def store() -> lyrics.LyricsPersistResult | None:
    return await lyrics.store_plain_lyrics(MagicMock(), MagicMock(), MagicMock(), QUERY)


async def test_found_plain_text_is_written_under_the_sources_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = StoreSpy(monkeypatch, stored=NOTHING_STORED, found=SourceAnswer("genius", PLAIN))

    persisted = await store()

    assert spy.calls == ["load", "fetch", "persist"]
    assert spy.written_source == "genius"
    assert persisted is not None
    assert persisted.outcome is LyricsPersistOutcome.WRITTEN


@pytest.mark.parametrize(
    "stored",
    [
        TIMED_STORED,
        UNTIMED_STORED,
        lyrics.StoredLyrics(renderable_lines=(), total_count=0, edited_by_admin=True),
    ],
)
async def test_stored_lines_or_a_saved_text_mean_no_request_at_all(
    monkeypatch: pytest.MonkeyPatch, stored: lyrics.StoredLyrics
) -> None:
    spy = StoreSpy(monkeypatch, stored=stored, found=SourceAnswer("genius", PLAIN))

    assert await store() is None
    assert spy.calls == ["load"]


async def test_nothing_found_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = StoreSpy(monkeypatch, stored=NOTHING_STORED, found=None)

    assert await store() is None
    assert spy.calls == ["load", "fetch"]
