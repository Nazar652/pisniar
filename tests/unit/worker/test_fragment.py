"""The fragment rule: start at zero, 45 seconds, a shorter track taken whole,
and the bounds absolute from the start of the full track, which is what the
subtitle timing and hand-set bounds both read.
"""

from __future__ import annotations

import pytest

from pisniar.worker import fragment as fragment_rule


def test_a_long_track_gives_forty_five_seconds_from_the_start() -> None:
    bounds = fragment_rule.choose_fragment_bounds(212_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)
    assert bounds.length_ms == 45_000


def test_a_track_shorter_than_the_fragment_is_taken_whole() -> None:
    bounds = fragment_rule.choose_fragment_bounds(31_500)

    assert (bounds.start_ms, bounds.end_ms) == (0, 31_500)


def test_a_track_exactly_as_long_as_the_fragment_is_not_truncated() -> None:
    bounds = fragment_rule.choose_fragment_bounds(45_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)


def test_the_rule_is_deterministic_for_the_same_duration() -> None:
    assert fragment_rule.choose_fragment_bounds(212_000) == fragment_rule.choose_fragment_bounds(
        212_000
    )


def test_the_start_is_always_absolute_zero_never_a_percentage() -> None:
    """`ass_t = lyric.start_ms - fragment_start_ms`, so a fragment-local offset
    here would silently shift every subtitle."""
    for duration_ms in (10_000, 45_000, 60_000, 480_000):
        assert fragment_rule.choose_fragment_bounds(duration_ms).start_ms == 0


@pytest.mark.parametrize("duration_ms", [0, -1])
def test_a_duration_no_fragment_fits_in_is_refused(duration_ms: int) -> None:
    with pytest.raises(fragment_rule.FragmentBoundsError):
        fragment_rule.choose_fragment_bounds(duration_ms)


def test_stored_bounds_are_read_back_as_a_pair() -> None:
    bounds = fragment_rule.stored_bounds(1_000, 46_000)

    assert bounds == fragment_rule.FragmentBounds(start_ms=1_000, end_ms=46_000)


@pytest.mark.parametrize("stored", [(None, None), (0, None), (None, 45_000)])
def test_a_missing_or_one_sided_pair_is_not_a_fragment(
    stored: tuple[int | None, int | None],
) -> None:
    assert fragment_rule.stored_bounds(*stored) is None


@pytest.mark.parametrize("stored", [(-1, 45_000), (45_000, 45_000), (45_000, 1_000)])
def test_stored_bounds_the_check_constraint_would_refuse_raise_here_too(
    stored: tuple[int, int],
) -> None:
    with pytest.raises(fragment_rule.FragmentBoundsError):
        fragment_rule.stored_bounds(*stored)


def test_bounds_inside_the_file_are_accepted() -> None:
    bounds = fragment_rule.choose_fragment_bounds(212_000)

    assert fragment_rule.bounds_within_track(bounds, 212_000) is True


def test_bounds_past_the_end_of_the_file_are_rejected() -> None:
    """The bounds must fit the audio that is actually on disk, not the duration
    some source once reported."""
    bounds = fragment_rule.FragmentBounds(start_ms=0, end_ms=45_000)

    assert fragment_rule.bounds_within_track(bounds, 31_000) is False
