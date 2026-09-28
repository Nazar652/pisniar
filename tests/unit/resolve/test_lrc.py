"""The LRC parser: both timecode precisions, several tags per source line,
metadata ignored, pauses kept as gap markers — and the loud refusal that
tells "a few crooked lines" apart from "this is a different format".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pisniar.resolve import lrc


def test_two_digit_and_three_digit_fractions_both_become_milliseconds() -> None:
    parsed = lrc.parse_lrc("[00:12.34]два знаки\n[00:12.345]три знаки")

    assert [line.start_ms for line in parsed] == [12_340, 12_345]


def test_a_fraction_is_padded_not_read_as_an_integer() -> None:
    """`.5` is half a second, not five milliseconds — the whole reason the
    fraction is right-padded rather than parsed as a number."""
    parsed = lrc.parse_lrc("[00:01.5]пів секунди")

    assert parsed[0].start_ms == 1_500


def test_minutes_carry_and_may_exceed_an_hour_of_seconds() -> None:
    parsed = lrc.parse_lrc("[03:07.00]рядок\n[100:00.00]довгий трек")

    assert [line.start_ms for line in parsed] == [187_000, 6_000_000]


def test_a_missing_fraction_is_a_whole_second() -> None:
    parsed = lrc.parse_lrc("[01:02]без дробу")

    assert parsed[0].start_ms == 62_000


def test_several_tags_on_one_source_line_repeat_the_same_text() -> None:
    parsed = lrc.parse_lrc("[00:10.00][00:40.00]приспів")

    assert [(line.start_ms, line.text) for line in parsed] == [
        (10_000, "приспів"),
        (40_000, "приспів"),
    ]


def test_metadata_tags_are_ignored_without_counting_as_damage() -> None:
    content = "[ar:Виконавець]\n[ti:Назва]\n[length:03:21]\n[by:хтось]\n[00:01.00]рядок"

    parsed = lrc.parse_lrc(content)

    assert [(line.start_ms, line.text) for line in parsed] == [(1_000, "рядок")]


def test_an_offset_tag_is_ignored_rather_than_applied() -> None:
    """Applying it would shift every timecode silently; metatags are ignored,
    and `offset` is one."""
    parsed = lrc.parse_lrc("[offset:+5000]\n[00:01.00]рядок")

    assert parsed[0].start_ms == 1_000


def test_lines_come_back_sorted_by_time_whatever_order_the_source_had() -> None:
    parsed = lrc.parse_lrc("[00:30.00]третій\n[00:10.00]перший\n[00:20.00]другий")

    assert [line.text for line in parsed] == ["перший", "другий", "третій"]


def test_ties_keep_the_order_of_the_source() -> None:
    parsed = lrc.parse_lrc("[00:10.00]перший\n[00:10.00]другий")

    assert [line.text for line in parsed] == ["перший", "другий"]


def test_an_empty_text_under_a_timecode_is_a_pause_and_is_kept() -> None:
    """It draws nothing, and it is what ends the line before it — dropping
    it would leave a verse's last line hanging until the next verse."""
    parsed = lrc.parse_lrc("[00:01.00]рядок\n[00:05.00]\n[00:09.00]наступний")

    assert [line.is_pause for line in parsed] == [False, True, False]


def test_surrounding_whitespace_is_collapsed_not_carried_into_the_frame() -> None:
    parsed = lrc.parse_lrc("[00:01.00]   два    слова   ")

    assert parsed[0].text == "два слова"


def test_a_bracket_inside_the_lyrics_is_text_not_a_tag() -> None:
    parsed = lrc.parse_lrc("[00:01.00]слово [у дужках] далі")

    assert parsed[0].text == "слово [у дужках] далі"


def test_one_crooked_line_among_many_is_dropped_with_a_warning() -> None:
    content = "\n".join(f"[00:{second:02d}.00]рядок" for second in range(10))
    content += "\nце не рядок LRC"

    parsed = lrc.parse_lrc(content)

    assert len(parsed) == 10


def test_plain_lyrics_are_refused_loudly_instead_of_coming_back_empty() -> None:
    """Everything dropped is not "a few crooked lines", it is a different
    format — and an empty list would read as "this track has no text"."""
    with pytest.raises(lrc.LrcParseError, match="не LRC"):
        lrc.parse_lrc("перший рядок\nдругий рядок\nтретій рядок")


def test_crossing_the_drop_threshold_raises_even_when_most_lines_parsed() -> None:
    content = "[00:01.00]раз\n[00:02.00]два\nсміття\nще сміття"

    with pytest.raises(lrc.LrcParseError):
        lrc.parse_lrc(content)


def test_an_empty_payload_is_an_empty_result_not_an_error() -> None:
    """Nothing was dropped, so nothing is loud here; classifying an empty
    answer is the adapter's job, not the parser's."""
    assert lrc.parse_lrc("") == []
    assert lrc.parse_lrc("\n  \n\n") == []


def test_a_metadata_only_payload_is_empty_rather_than_a_format_error() -> None:
    assert lrc.parse_lrc("[ar:Виконавець]\n[ti:Назва]") == []


def test_an_out_of_range_seconds_field_is_not_taken_as_a_timecode() -> None:
    with pytest.raises(lrc.LrcParseError):
        lrc.parse_lrc("[00:75.00]сімдесят п'ять секунд")


def test_the_warning_carries_the_shape_of_the_dropped_line_not_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A song's words do not belong in the structured log."""
    fake_logger = MagicMock()
    monkeypatch.setattr(lrc, "logger", fake_logger)
    content = "\n".join(f"[00:{second:02d}.00]рядок" for second in range(10))
    content += "\nтаємний текст пісні"

    lrc.parse_lrc(content)

    warning_call = fake_logger.warning.call_args
    assert warning_call.args == ("lrc_line_dropped",)
    assert warning_call.kwargs == {"line_number": 11, "length": len("таємний текст пісні")}
