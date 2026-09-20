"""D-76: the fragment starts at the beginning of the track and runs 45
seconds; a track shorter than that is taken whole. Deterministic, no
heuristics. The bounds are absolute milliseconds from the start of the *full*
track (D-5) — the seam step 6 and 4B both read, so it is never fragment-local.
"""

from __future__ import annotations

from dataclasses import dataclass

from pisniar.logging import get_logger

logger = get_logger(__name__)

FRAGMENT_START_MS = 0
FRAGMENT_LENGTH_MS = 45_000


class FragmentBoundsError(ValueError):
    """A duration no fragment can be cut out of."""


@dataclass(frozen=True)
class FragmentBounds:
    start_ms: int
    end_ms: int

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms


def choose_fragment_bounds(track_duration_ms: int) -> FragmentBounds:
    """The whole rule, in one expression: start at zero, end 45 s later or at
    the end of the track, whichever comes first."""
    if track_duration_ms <= 0:
        raise FragmentBoundsError(
            f"тривалість треку має бути додатною, отримано {track_duration_ms}"
        )

    end_ms = min(FRAGMENT_START_MS + FRAGMENT_LENGTH_MS, track_duration_ms)
    bounds = FragmentBounds(start_ms=FRAGMENT_START_MS, end_ms=end_ms)

    logger.info(
        "fragment_bounds_chosen",
        start_ms=bounds.start_ms,
        end_ms=bounds.end_ms,
        length_ms=bounds.length_ms,
        track_duration_ms=track_duration_ms,
        whole_track=bounds.length_ms == track_duration_ms,
    )
    return bounds


def stored_bounds(start_ms: int | None, end_ms: int | None) -> FragmentBounds | None:
    """What is already on the post, if it is a whole pair. A one-sided pair is
    not a fragment — `ck_post_fragment_bounds_valid` refuses to store one."""
    if start_ms is None or end_ms is None:
        return None
    if start_ms < 0 or end_ms <= start_ms:
        raise FragmentBoundsError(f"збережені межі фрагмента невалідні: [{start_ms}, {end_ms})")
    return FragmentBounds(start_ms=start_ms, end_ms=end_ms)


def bounds_within_track(bounds: FragmentBounds, track_duration_ms: int) -> bool:
    """S4-A4: the bounds must lie inside `[0, duration_ms]` of the file that
    is actually on disk, not of the duration some source once reported."""
    return 0 <= bounds.start_ms < bounds.end_ms <= track_duration_ms
