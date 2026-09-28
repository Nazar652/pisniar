"""The fragment starts at 0 and runs 45 s (a shorter track whole), no heuristics.
Bounds are absolute ms from the start of the *full* track, never fragment-local.
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
    if start_ms is None or end_ms is None:
        return None
    if start_ms < 0 or end_ms <= start_ms:
        raise FragmentBoundsError(f"збережені межі фрагмента невалідні: [{start_ms}, {end_ms})")
    return FragmentBounds(start_ms=start_ms, end_ms=end_ms)


def bounds_within_track(bounds: FragmentBounds, track_duration_ms: int) -> bool:
    """`track_duration_ms` is the file on disk, never a source-reported one."""
    return 0 <= bounds.start_ms < bounds.end_ms <= track_duration_ms
