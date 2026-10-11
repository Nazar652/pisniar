"""`plan_lyrics`, the rule for a text the admin saves: every bound at its edge, and the number of
the first line that breaks it.
"""

from __future__ import annotations

import pytest

from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH, MAX_LYRICS_TOTAL_CHARACTERS
from pisniar.webapp.lyrics_rules import LineDraft, LyricsRuleError, normalized_text, plan_lyrics

DURATION_MS = 180_000


def timed(start_ms: int, text: str = "рядок") -> LineDraft:
    return LineDraft(text=text, start_ms=start_ms)


def untimed(text: str = "рядок") -> LineDraft:
    return LineDraft(text=text, start_ms=None)


def refusal_of(lines: list[LineDraft]) -> LyricsRuleError:
    with pytest.raises(LyricsRuleError) as refusal:
        plan_lyrics(lines, DURATION_MS)
    return refusal.value


def test_spaces_are_collapsed_and_the_order_is_kept() -> None:
    planned = plan_lyrics([timed(1_000, "  раз \t два "), untimed("три\n  чотири")], DURATION_MS)

    assert planned == (timed(1_000, "раз два"), untimed("три чотири"))


def test_an_empty_text_is_a_text_removed_on_purpose() -> None:
    assert plan_lyrics([], DURATION_MS) == ()


def test_a_text_of_untimed_lines_only_is_accepted() -> None:
    assert plan_lyrics([untimed("раз"), untimed("два")], DURATION_MS) == (
        untimed("раз"),
        untimed("два"),
    )


def test_untimed_lines_may_sit_between_timed_ones() -> None:
    lines = [timed(1_000), untimed(), timed(2_000), untimed()]

    assert plan_lyrics(lines, DURATION_MS) == tuple(lines)


def test_an_empty_line_with_a_time_is_a_pause_and_without_one_is_refused() -> None:
    assert plan_lyrics([timed(1_000, "  ")], DURATION_MS) == (timed(1_000, ""),)

    refusal = refusal_of([timed(1_000), untimed("   ")])
    assert refusal.number == 2
    assert "Рядок 2" in refusal.message


@pytest.mark.parametrize(("length", "accepted"), [(MAX_LYRIC_TEXT_LENGTH, True), (501, False)])
def test_a_line_of_500_characters_is_the_longest(length: int, accepted: bool) -> None:
    lines = [untimed("а" * length)]

    if accepted:
        assert plan_lyrics(lines, DURATION_MS) == tuple(lines)
    else:
        refusal = refusal_of(lines)
        assert refusal.number == 1
        assert "501" in refusal.message and "500" in refusal.message


def test_the_length_is_measured_after_the_spaces_are_collapsed() -> None:
    fits = untimed("а" * 249 + " " * 400 + "б" * 250)
    too_long = untimed("а" * 250 + " " * 400 + "б" * 250)

    assert len(plan_lyrics([fits], DURATION_MS)[0].text) == MAX_LYRIC_TEXT_LENGTH
    assert refusal_of([too_long]).number == 1


@pytest.mark.parametrize(("count", "accepted"), [(MAX_LYRIC_LINES, True), (MAX_LYRIC_LINES + 1, False)])
def test_the_number_of_lines_is_bounded(count: int, accepted: bool) -> None:
    lines = [untimed("р") for _ in range(count)]

    if accepted:
        assert len(plan_lyrics(lines, DURATION_MS)) == count
    else:
        refusal = refusal_of(lines)
        assert refusal.number == MAX_LYRIC_LINES + 1
        assert str(MAX_LYRIC_LINES) in refusal.message


def test_the_total_of_characters_is_bounded_and_the_line_that_crosses_it_is_named() -> None:
    per_line = 400
    full_lines = MAX_LYRICS_TOTAL_CHARACTERS // per_line
    lines = [untimed("а" * per_line) for _ in range(full_lines)]

    assert len(plan_lyrics(lines, DURATION_MS)) == full_lines

    refusal = refusal_of([*lines, untimed("б")])
    assert refusal.number == full_lines + 1
    assert str(MAX_LYRICS_TOTAL_CHARACTERS) in refusal.message


@pytest.mark.parametrize(
    ("start_ms", "accepted"),
    [(0, True), (DURATION_MS - 1, True), (DURATION_MS, False), (-1, False)],
)
def test_a_time_must_lie_inside_the_track(start_ms: int, accepted: bool) -> None:
    if accepted:
        assert plan_lyrics([timed(start_ms)], DURATION_MS) == (timed(start_ms),)
    else:
        refusal = refusal_of([timed(start_ms)])
        assert refusal.number == 1
        assert str(start_ms) in refusal.message and str(DURATION_MS) in refusal.message


@pytest.mark.parametrize("second_ms", [1_000, 999, 0])
def test_timed_lines_must_rise_strictly(second_ms: int) -> None:
    refusal = refusal_of([timed(500), timed(1_000), timed(second_ms)])

    assert refusal.number == 3
    assert str(second_ms) in refusal.message and "1000" in refusal.message


def test_untimed_lines_between_timed_ones_do_not_reset_the_order() -> None:
    refusal = refusal_of([timed(5_000), untimed(), timed(4_000)])

    assert refusal.number == 3


def test_the_first_broken_line_is_the_one_named() -> None:
    refusal = refusal_of([timed(1_000), timed(900), untimed("   "), timed(DURATION_MS)])

    assert refusal.number == 2


def test_the_music_note_is_an_ordinary_line_with_a_time_or_without() -> None:
    lines = [timed(1_000, "♪"), untimed("♪")]

    assert plan_lyrics(lines, DURATION_MS) == tuple(lines)


def test_normalized_text_collapses_every_kind_of_space() -> None:
    assert normalized_text(" а \tб\n в ") == "а б в"
