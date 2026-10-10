"""`ytmusic_lyrics`: the answer classes told apart by the shape of the `ytmusicapi`
result, the music note kept in timed lines, and what must never be cached. The
client is a fake: no network call is made.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from ytmusicapi.models.lyrics import LyricLine

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import ytmusic_lyrics
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome
from pisniar.resolve.sources.ytmusic_lyrics import YtmusicLyricsApiError

VIDEO_ID = "n4bPzdIs_nc"
TIMED_ANSWER = {
    "lyrics": [
        LyricLine(text="♪", start_time=0, end_time=900, id=1),
        LyricLine(text="Перший  рядок", start_time=1_000, end_time=4_000, id=2),
        LyricLine(text="Другий рядок", start_time=4_500, end_time=8_000, id=3),
    ],
    "source": "Source: Musixmatch",
    "hasTimestamps": True,
}
PLAIN_ANSWER = {
    "lyrics": "Перший рядок\n\n♪\nДругий  рядок\n",
    "source": "Source: LyricFind",
    "hasTimestamps": False,
}


def fake_client(*, browse_id: object, answer: object) -> MagicMock:
    client = MagicMock()
    client.get_watch_playlist.return_value = {"lyrics": browse_id}
    client.get_lyrics.return_value = answer
    return client


def test_the_watch_playlist_without_a_lyrics_page_is_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = fake_client(browse_id=None, answer=TIMED_ANSWER)
    monkeypatch.setattr(ytmusic_lyrics, "get_client", lambda: client)

    assert ytmusic_lyrics.fetch_answer_sync(VIDEO_ID) is None
    client.get_lyrics.assert_not_called()


def test_the_lyrics_are_asked_for_with_timestamps_by_the_page_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = fake_client(browse_id="MPLYt_abc", answer=TIMED_ANSWER)
    monkeypatch.setattr(ytmusic_lyrics, "get_client", lambda: client)

    assert ytmusic_lyrics.fetch_answer_sync(VIDEO_ID) is TIMED_ANSWER
    client.get_watch_playlist.assert_called_once_with(VIDEO_ID)
    client.get_lyrics.assert_called_once_with("MPLYt_abc", timestamps=True)


def test_no_answer_is_a_negative_payload() -> None:
    assert ytmusic_lyrics.payload_from_answer(None) == {"found": False}


def test_a_timed_answer_becomes_start_and_text_pairs() -> None:
    payload = ytmusic_lyrics.payload_from_answer(TIMED_ANSWER)

    assert payload == {
        "found": True,
        "timed": [[0, "♪"], [1_000, "Перший  рядок"], [4_500, "Другий рядок"]],
    }


def test_a_plain_answer_keeps_its_text() -> None:
    assert ytmusic_lyrics.payload_from_answer(PLAIN_ANSWER) == {
        "found": True,
        "plain": PLAIN_ANSWER["lyrics"],
    }


@pytest.mark.parametrize(
    "answer",
    [
        "not a dict",
        {},
        {"lyrics": [], "hasTimestamps": True},
        {"lyrics": ["a bare string"], "hasTimestamps": True},
        {"lyrics": "   ", "hasTimestamps": False},
        {"lyrics": "text", "hasTimestamps": True},
        {"lyrics": [LyricLine(text="x", start_time=0, end_time=1, id=1)], "hasTimestamps": False},
    ],
)
def test_an_answer_of_any_other_shape_raises(answer: object) -> None:
    with pytest.raises(YtmusicLyricsApiError):
        ytmusic_lyrics.payload_from_answer(answer)


def test_the_error_class_reads_as_an_unavailable_source() -> None:
    assert YtmusicLyricsApiError.outcome is LyricsOutcome.SOURCE_UNAVAILABLE


def test_a_negative_payload_is_absent() -> None:
    assert ytmusic_lyrics.result_from_payload({"found": False}).outcome is LyricsOutcome.ABSENT


def test_a_timed_gap_mark_stays_as_the_note_and_an_empty_line_stays_a_pause() -> None:
    result = ytmusic_lyrics.result_from_payload(
        {"found": True, "timed": [[0, "♪"], [1_000, "Перший  рядок"], [4_500, " ♪ "], [9_000, ""]]}
    )

    assert result.outcome is LyricsOutcome.SYNCED
    assert result.lines == (
        LyricsLine(start_ms=0, text="♪"),
        LyricsLine(start_ms=1_000, text="Перший рядок"),
        LyricsLine(start_ms=4_500, text="♪"),
        LyricsLine(start_ms=9_000, text=""),
    )


def test_timed_lines_come_out_in_time_order() -> None:
    result = ytmusic_lyrics.result_from_payload(
        {"found": True, "timed": [[5_000, "другий"], [1_000, "перший"]]}
    )

    assert [line.text for line in result.lines] == ["перший", "другий"]


def test_only_gap_marks_say_nothing_about_the_song() -> None:
    result = ytmusic_lyrics.result_from_payload({"found": True, "timed": [[0, "♪"], [5_000, "♪"]]})

    assert result.outcome is LyricsOutcome.ABSENT


def test_plain_text_becomes_untimed_lines_without_blanks_and_gap_marks() -> None:
    result = ytmusic_lyrics.result_from_payload({"found": True, "plain": PLAIN_ANSWER["lyrics"]})

    assert result.outcome is LyricsOutcome.UNSYNCED
    assert result.lines == (
        LyricsLine(start_ms=None, text="Перший рядок"),
        LyricsLine(start_ms=None, text="Другий рядок"),
    )


def test_plain_text_of_only_gap_marks_is_absent() -> None:
    result = ytmusic_lyrics.result_from_payload({"found": True, "plain": "♪\n\n♪"})

    assert result.outcome is LyricsOutcome.ABSENT


@pytest.mark.parametrize(
    "payload",
    [
        {"found": True},
        {"found": True, "timed": [[1, "a", "b"]]},
        {"found": True, "timed": [["1", "a"]]},
        {"found": True, "timed": [[-1, "a"]]},
        {"found": True, "timed": [[True, "a"]]},
        {"found": True, "timed": [[1, 2]]},
        {"found": True, "timed": ["not a pair"]},
    ],
)
def test_a_cached_row_of_the_wrong_shape_raises(payload: dict[str, object]) -> None:
    with pytest.raises(YtmusicLyricsApiError):
        ytmusic_lyrics.result_from_payload(payload)


def test_a_pair_becomes_a_line() -> None:
    assert ytmusic_lyrics.timed_line_from_pair([2_000, " раз  два "]) == LyricsLine(
        start_ms=2_000, text="раз два"
    )


@pytest.mark.parametrize(
    ("raw_text", "cleaned"),
    [("  a   b ", "a b"), ("♪", "♪"), (" ♪ ", "♪"), ("♪ la", "♪ la"), ("", "")],
)
def test_clean_text_collapses_spaces_and_keeps_the_note(raw_text: str, cleaned: str) -> None:
    assert ytmusic_lyrics.clean_text(raw_text) == cleaned


async def test_a_fetch_goes_through_the_cache_by_video_id(
    monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache
) -> None:
    monkeypatch.setattr(ytmusic_lyrics, "fetch_answer_sync", lambda video_id: TIMED_ANSWER)

    result = await ytmusic_lyrics.fetch_lyrics(fake_cache, video_id=VIDEO_ID)

    assert result.outcome is LyricsOutcome.SYNCED
    cache_mock: Any = fake_cache
    source, request_key, _ = cache_mock.get_or_fetch.await_args.args
    assert (source, request_key) == ("ytmusic", f"ytmusic:lyrics:{VIDEO_ID}")


async def test_a_video_without_a_lyrics_page_is_absent(
    monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache
) -> None:
    monkeypatch.setattr(ytmusic_lyrics, "fetch_answer_sync", lambda video_id: None)

    result = await ytmusic_lyrics.fetch_lyrics(fake_cache, video_id=VIDEO_ID)

    assert result.outcome is LyricsOutcome.ABSENT


async def test_a_malformed_answer_raises_before_anything_is_cached(
    monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache
) -> None:
    """`get_or_fetch` writes only what `fetch` returns, and this one raises."""
    monkeypatch.setattr(ytmusic_lyrics, "fetch_answer_sync", lambda video_id: {"lyrics": 5})

    with pytest.raises(YtmusicLyricsApiError):
        await ytmusic_lyrics.fetch_lyrics(fake_cache, video_id=VIDEO_ID)


async def test_a_transport_error_propagates_for_the_callers_isolation(
    monkeypatch: pytest.MonkeyPatch, fake_cache: SourceCache
) -> None:
    def fail(video_id: str) -> object:
        raise ConnectionError("no route")

    monkeypatch.setattr(ytmusic_lyrics, "fetch_answer_sync", fail)

    with pytest.raises(ConnectionError):
        await ytmusic_lyrics.fetch_lyrics(fake_cache, video_id=VIDEO_ID)
