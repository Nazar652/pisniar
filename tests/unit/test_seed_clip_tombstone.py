"""A clip the admin sends lifts an earlier removal of the `youtube` platform."""

from __future__ import annotations

from pisniar.bot import handlers
from pisniar.db.models import Track
from pisniar.resolve.urls import classify_url

CLIP = classify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def make_track(removed_platforms: list[object] | None) -> Track:
    track = Track(source_url="x", source_url_normalized="x")
    track.platform_links = {}
    if removed_platforms is not None:
        track.removed_platforms = removed_platforms
    return track


def test_a_clip_lifts_the_removal_of_the_youtube_platform() -> None:
    track = make_track(["youtube", "tidal"])

    handlers.seed_user_input_clip(track, CLIP)

    assert track.removed_platforms == ["tidal"]
    assert track.platform_links["youtube"] == {
        "url": CLIP.normalized_url,
        "source": "user_input",
        "match": "manual",
    }


def test_a_clip_leaves_other_removals_alone() -> None:
    track = make_track(["tidal"])

    handlers.seed_user_input_clip(track, CLIP)

    assert track.removed_platforms == ["tidal"]


def test_a_track_without_a_removal_list_yet_is_seeded() -> None:
    track = make_track(None)

    handlers.seed_user_input_clip(track, CLIP)

    assert track.platform_links["youtube"] == {
        "url": CLIP.normalized_url,
        "source": "user_input",
        "match": "manual",
    }


def test_no_clip_changes_nothing() -> None:
    track = make_track(["youtube"])

    handlers.seed_user_input_clip(track, None)

    assert track.removed_platforms == ["youtube"]
