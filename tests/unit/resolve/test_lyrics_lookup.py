"""`resolve.lyrics_lookup`: the order of the timed sources, why a lookup found nothing, the
plain-text sources an admin can pick and the words taken out of an answer. No network and no
database: every source is replaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from pisniar.db.models import MAX_LYRIC_LINES
from pisniar.resolve import lyrics_lookup
from pisniar.resolve.lyrics_lookup import LyricsLookupQuery, SourceAnswer
from pisniar.resolve.sources import genius, ytmusic_lyrics
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult

QUERY = LyricsLookupQuery(
    track_id=7,
    artist_names=("Burned Time Machine",),
    title="Покаяння",
    duration_ms=212_000,
    video_id="n4bPzdIs_nc",
)


def timed(*lines: tuple[int, str]) -> LyricsResult:
    return LyricsResult(
        outcome=LyricsOutcome.SYNCED,
        lines=tuple(LyricsLine(start_ms=start_ms, text=text) for start_ms, text in lines),
    )


def plain(*texts: str) -> LyricsResult:
    return LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=tuple(LyricsLine(start_ms=None, text=text) for text in texts),
    )


SYNCED = timed((1_000, "раз"))
OTHER_SYNCED = timed((2_000, "інакше"))
ABSENT = LyricsResult(outcome=LyricsOutcome.ABSENT)
UNAVAILABLE = LyricsResult(outcome=LyricsOutcome.SOURCE_UNAVAILABLE)
OVER_LIMIT = LyricsResult(
    outcome=LyricsOutcome.SYNCED,
    lines=tuple(LyricsLine(start_ms=index, text="р") for index in range(MAX_LYRIC_LINES + 1)),
)


class Sources:
    """the two timed sources replaced, with the order they were asked in."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        youtube: LyricsResult | BaseException | None,
        lrclib: LyricsResult | BaseException | None = None,
    ) -> None:
        self.asked: list[str] = []
        self.answers = {"ytmusic": youtube, "lrclib": lrclib}
        monkeypatch.setattr(ytmusic_lyrics, "fetch_lyrics", self.fetch("ytmusic"))
        monkeypatch.setattr(lyrics_lookup, "fetch_lrclib_lyrics", self.fetch("lrclib"))

    def fetch(self, name: str) -> AsyncMock:
        async def answer(*args: object, **kwargs: object) -> LyricsResult:
            self.asked.append(name)
            outcome = self.answers[name]
            if isinstance(outcome, BaseException):
                raise outcome
            assert outcome is not None
            return outcome

        return AsyncMock(side_effect=answer)


async def test_youtube_music_is_asked_first_and_a_usable_answer_spares_lrclib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = Sources(monkeypatch, youtube=SYNCED, lrclib=OTHER_SYNCED)

    answer, reason = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)

    assert answer == SourceAnswer("ytmusic", SYNCED)
    assert (sources.asked, reason) == (["ytmusic"], "")


@pytest.mark.parametrize("youtube", [ABSENT, UNAVAILABLE, plain("раз"), OVER_LIMIT])
async def test_lrclib_is_asked_when_youtube_music_gave_nothing_usable(
    monkeypatch: pytest.MonkeyPatch, youtube: LyricsResult
) -> None:
    sources = Sources(monkeypatch, youtube=youtube, lrclib=OTHER_SYNCED)

    answer, _ = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)

    assert answer == SourceAnswer("lrclib", OTHER_SYNCED)
    assert sources.asked == ["ytmusic", "lrclib"]


async def test_without_a_video_id_only_lrclib_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = Sources(monkeypatch, youtube=SYNCED, lrclib=OTHER_SYNCED)
    query = LyricsLookupQuery(7, ("А",), "Т", None, None)

    answer, _ = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), query)

    assert answer == SourceAnswer("lrclib", OTHER_SYNCED)
    assert sources.asked == ["lrclib"]


async def test_the_order_of_the_timed_sources_is_written_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = Sources(monkeypatch, youtube=ABSENT, lrclib=OTHER_SYNCED)

    youtube, lrclib = await lyrics_lookup.ask_timed_sources(MagicMock(), MagicMock(), QUERY)
    assert (youtube, lrclib) == (ABSENT, OTHER_SYNCED)
    assert sources.asked == ["ytmusic", "lrclib"]

    spy = AsyncMock(return_value=(SYNCED, None))
    monkeypatch.setattr(lyrics_lookup, "ask_timed_sources", spy)
    answer, _ = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)

    assert answer == SourceAnswer("ytmusic", SYNCED)
    spy.assert_awaited_once()


async def test_a_failure_and_a_miss_make_the_source_unavailable_not_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Sources(monkeypatch, youtube=ABSENT, lrclib=ConnectionError("down"))

    answer, reason = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)

    assert answer is None
    assert reason == LyricsOutcome.SOURCE_UNAVAILABLE.value


async def test_every_source_saying_absent_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    Sources(monkeypatch, youtube=ABSENT, lrclib=ABSENT)

    answer, reason = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)

    assert (answer, reason) == (None, LyricsOutcome.ABSENT.value)


async def test_plain_text_found_on_the_way_is_a_miss_for_a_timed_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Sources(monkeypatch, youtube=plain("раз"), lrclib=plain("два"))

    answer, reason = await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)

    assert (answer, reason) == (None, LyricsOutcome.ABSENT.value)


@pytest.mark.parametrize(
    "failure",
    [SoftTimeLimitExceeded(), OperationalError("SELECT 1", {}, Exception("server closed"))],
)
async def test_the_soft_limit_and_a_database_error_pass_through_a_timed_lookup(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    Sources(monkeypatch, youtube=failure)

    with pytest.raises(type(failure)):
        await lyrics_lookup.look_up_timed_text(MagicMock(), MagicMock(), QUERY)


def test_the_timed_answer_is_the_first_usable_one_in_the_order_of_the_sources() -> None:
    assert lyrics_lookup.choose_timed_answer(SYNCED, OTHER_SYNCED) == SourceAnswer("ytmusic", SYNCED)
    assert lyrics_lookup.choose_timed_answer(OVER_LIMIT, OTHER_SYNCED) == SourceAnswer("lrclib", OTHER_SYNCED)
    assert lyrics_lookup.choose_timed_answer(plain("раз"), plain("два")) is None
    assert lyrics_lookup.choose_timed_answer(None, None) is None


@pytest.mark.parametrize(
    ("youtube", "lrclib", "reason"),
    [
        (ABSENT, ABSENT, "lyrics_absent"),
        (None, ABSENT, "lyrics_absent"),
        (UNAVAILABLE, ABSENT, "lyrics_source_unavailable"),
        (ABSENT, UNAVAILABLE, "lyrics_source_unavailable"),
        (OVER_LIMIT, ABSENT, "lyrics_over_limits"),
        (OVER_LIMIT, UNAVAILABLE, "lyrics_source_unavailable"),
        (plain("раз"), ABSENT, "lyrics_absent"),
    ],
)
def test_a_timed_miss_is_a_failure_before_an_oversized_text_before_a_plain_absence(
    youtube: LyricsResult | None,
    lrclib: LyricsResult | None,
    reason: str,
) -> None:
    assert lyrics_lookup.timed_miss_reason(youtube, lrclib) == reason


def test_the_plain_sources_are_the_three_an_admin_can_pick_with_genius_first() -> None:
    assert [(source.name, source.label) for source in lyrics_lookup.PLAIN_TEXT_SOURCES] == [
        ("genius", "Genius"),
        ("ytmusic", "YouTube Music"),
        ("lrclib", "LRCLIB"),
    ]


def test_the_lookup_has_one_time_limit_of_twenty_seconds() -> None:
    assert lyrics_lookup.LYRICS_LOOKUP_TIMEOUT_SECONDS == 20


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (plain("раз", "два"), ("раз", "два")),
        (timed((1_000, "раз"), (2_000, "два")), ("раз", "два")),
        (plain("  раз \t два  ", "три"), ("раз два", "три")),
        (plain("раз", "", "   ", "два"), ("раз", "два")),
        (timed((1_000, "♪"), (2_000, "раз"), (3_000, "")), ("раз",)),
        (plain("♪ слово"), ("♪ слово",)),
        (ABSENT, ()),
        (LyricsResult(outcome=LyricsOutcome.INSTRUMENTAL), ()),
        (LyricsResult(outcome=LyricsOutcome.REJECTED_BY_MATCHING, lines=plain("раз").lines), ()),
        (UNAVAILABLE, ()),
    ],
)
def test_plain_lines_are_the_words_without_times_spaces_empty_lines_and_a_lone_note(
    result: LyricsResult, expected: tuple[str, ...]
) -> None:
    assert lyrics_lookup.plain_lines_of(result) == expected


def test_the_stored_lines_of_an_answer_follow_its_class() -> None:
    mixed = (LyricsLine(start_ms=1_000, text="раз"), LyricsLine(start_ms=None, text="два"))

    assert lyrics_lookup.lines_to_store(LyricsResult(LyricsOutcome.SYNCED, mixed)) == [mixed[0]]
    assert lyrics_lookup.lines_to_store(LyricsResult(LyricsOutcome.UNSYNCED, mixed)) == [
        LyricsLine(start_ms=None, text="раз"),
        LyricsLine(start_ms=None, text="два"),
    ]
    assert lyrics_lookup.lines_to_store(LyricsResult(LyricsOutcome.ABSENT, mixed)) == []


class PlainSources:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: LyricsResult) -> None:
        self.genius = AsyncMock(return_value=result)
        self.youtube = AsyncMock(return_value=result)
        self.lrclib = AsyncMock(return_value=result)
        monkeypatch.setattr(genius, "fetch_lyrics", self.genius)
        monkeypatch.setattr(ytmusic_lyrics, "fetch_lyrics", self.youtube)
        monkeypatch.setattr(lyrics_lookup, "fetch_lrclib_lyrics", self.lrclib)


async def test_genius_is_asked_with_the_track_as_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = PlainSources(monkeypatch, plain("раз"))
    client, cache = MagicMock(), MagicMock()

    result = await lyrics_lookup.fetch_plain_text("genius", client, cache, QUERY)

    assert result == plain("раз")
    sources.genius.assert_awaited_once_with(
        client,
        cache,
        artist_names=("Burned Time Machine",),
        title="Покаяння",
        duration_ms=212_000,
    )
    sources.youtube.assert_not_awaited()
    sources.lrclib.assert_not_awaited()


async def test_youtube_music_is_asked_with_the_video_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = PlainSources(monkeypatch, timed((1_000, "раз")))
    cache = MagicMock()

    result = await lyrics_lookup.fetch_plain_text("ytmusic", MagicMock(), cache, QUERY)

    assert result == timed((1_000, "раз"))
    sources.youtube.assert_awaited_once_with(cache, video_id="n4bPzdIs_nc")
    sources.genius.assert_not_awaited()


async def test_youtube_music_without_a_video_id_has_nothing_to_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = PlainSources(monkeypatch, plain("раз"))
    query = LyricsLookupQuery(7, ("А",), "Т", None, None)

    result = await lyrics_lookup.fetch_plain_text("ytmusic", MagicMock(), MagicMock(), query)

    assert result.outcome is LyricsOutcome.ABSENT
    sources.youtube.assert_not_awaited()


async def test_lrclib_is_asked_with_the_track_as_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = PlainSources(monkeypatch, plain("раз"))
    client, cache = MagicMock(), MagicMock()

    await lyrics_lookup.fetch_plain_text("lrclib", client, cache, QUERY)

    sources.lrclib.assert_awaited_once_with(
        client,
        cache,
        artist_names=("Burned Time Machine",),
        title="Покаяння",
        duration_ms=212_000,
    )


@pytest.mark.parametrize("source", ["genius", "ytmusic", "lrclib"])
async def test_a_failing_plain_source_answers_unavailable_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    sources = PlainSources(monkeypatch, plain("раз"))
    for mock in (sources.genius, sources.youtube, sources.lrclib):
        mock.side_effect = ConnectionError("down")

    result = await lyrics_lookup.fetch_plain_text(source, MagicMock(), MagicMock(), QUERY)

    assert result.outcome is LyricsOutcome.SOURCE_UNAVAILABLE


async def test_a_database_error_passes_through_a_plain_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = PlainSources(monkeypatch, plain("раз"))
    sources.genius.side_effect = OperationalError("SELECT 1", {}, Exception("closed"))

    with pytest.raises(OperationalError):
        await lyrics_lookup.fetch_plain_text("genius", MagicMock(), MagicMock(), QUERY)


async def test_an_unknown_plain_source_is_refused() -> None:
    with pytest.raises(ValueError, match="невідоме джерело"):
        await lyrics_lookup.fetch_plain_text("nope", MagicMock(), MagicMock(), QUERY)
