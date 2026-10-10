"""`apply_manual_fields`, `drop_removed_platforms` and the host list inside `merge_platform_link`."""

from __future__ import annotations

import structlog.testing

from pisniar.resolve import merge
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack

TIDAL_URL = "https://listen.tidal.com/track/1"


def make_link(url: str = TIDAL_URL, match: MatchClass = MatchClass.AGGREGATOR) -> PlatformLink:
    return PlatformLink(url=url, source="songlink", match=match)


def test_a_listed_field_takes_the_stored_value_even_against_a_fresh_one() -> None:
    resolved = ResolvedTrack(title="Fresh", year=1992, release_type="album")

    merge.apply_manual_fields(resolved, {"title": "Mine", "year": 1990, "release_type": "single"}, ["title", "year"])

    assert (resolved.title, resolved.year) == ("Mine", 1990)
    assert resolved.release_type == "album"


def test_a_listed_field_stays_empty_when_the_stored_value_is_none() -> None:
    resolved = ResolvedTrack(year=1992, release_title="Fresh", album_id_spotify="x")

    merge.apply_manual_fields(
        resolved,
        {"year": None, "release_title": None, "album_id_spotify": None},
        ["year", "release_title", "album_id_spotify"],
    )

    assert resolved.year is None
    assert resolved.release_title is None
    assert resolved.album_id_spotify is None


def test_listed_artists_keep_the_stored_hashtags() -> None:
    resolved = ResolvedTrack(artists=[ArtistInfo(name="Fresh")])

    merge.apply_manual_fields(
        resolved,
        {"artists": [{"name": "Mine", "hashtag": "own", "spotify_id": "abc"}]},
        ["artists"],
    )

    assert resolved.artists == [ArtistInfo(name="Mine", hashtag="own", spotify_id="abc")]


def test_an_unlisted_field_is_left_alone() -> None:
    resolved = ResolvedTrack(title="Fresh", year=1992)

    merge.apply_manual_fields(resolved, {"title": "Mine", "year": 1990}, [])

    assert (resolved.title, resolved.year) == ("Fresh", 1992)


def test_a_name_outside_the_known_columns_is_ignored() -> None:
    resolved = ResolvedTrack(title="Fresh", cover_url="https://c/1.jpg")

    merge.apply_manual_fields(resolved, {"cover_url": None}, ["cover_url"])

    assert resolved.cover_url == "https://c/1.jpg"


def test_only_the_fields_whose_fresh_value_differed_are_logged() -> None:
    resolved = ResolvedTrack(title="Same", year=1992)

    with structlog.testing.capture_logs() as captured:
        merge.apply_manual_fields(resolved, {"title": "Same", "year": 1990}, ["title", "year"])

    kept = [entry for entry in captured if entry["event"] == "manual_fields_kept"]
    assert [entry["fields"] for entry in kept] == [["year"]]


def test_no_log_when_nothing_differed() -> None:
    resolved = ResolvedTrack(title="Same")

    with structlog.testing.capture_logs() as captured:
        merge.apply_manual_fields(resolved, {"title": "Same"}, ["title"])

    assert captured == []


def test_a_removed_platform_is_dropped_with_its_priority() -> None:
    resolved = ResolvedTrack()
    merge.merge_platform_link(resolved, "tidal", make_link())
    merge.merge_platform_link(resolved, "deezer", make_link("https://www.deezer.com/track/1"))

    merge.drop_removed_platforms(resolved, ["tidal", "never-there", 7])

    assert set(resolved.platform_links) == {"deezer"}
    assert "tidal" not in resolved.platform_link_priorities


def test_a_dropped_platform_can_be_found_again_once_it_is_unlisted() -> None:
    resolved = ResolvedTrack()
    merge.merge_platform_link(resolved, "tidal", make_link())
    merge.drop_removed_platforms(resolved, ["tidal"])

    merge.merge_platform_link(resolved, "tidal", make_link())

    assert "tidal" in resolved.platform_links


def test_an_automatic_link_on_a_foreign_host_is_not_stored_and_is_reported() -> None:
    resolved = ResolvedTrack()

    with structlog.testing.capture_logs() as captured:
        merge.merge_platform_link(resolved, "tidal", make_link("https://evil.example/track/1"))

    assert resolved.platform_links == {}
    assert len(resolved.link_rejections) == 1
    assert "tidal" in resolved.link_rejections[0]
    assert "evil.example" in resolved.link_rejections[0]
    assert [entry["event"] for entry in captured] == ["platform_link_rejected_by_allowlist"]


def test_a_platform_the_app_does_not_list_is_dropped_quietly() -> None:
    resolved = ResolvedTrack()

    with structlog.testing.capture_logs() as captured:
        merge.merge_platform_link(resolved, "yandex", make_link("https://music.yandex.ru/track/1"))

    assert resolved.platform_links == {}
    assert resolved.link_rejections == []
    assert resolved.warnings == []
    assert [(entry["event"], entry["log_level"]) for entry in captured] == [("platform_link_ignored", "info")]


def test_any_unlisted_key_is_dropped_quietly_not_only_yandex() -> None:
    resolved = ResolvedTrack()

    merge.merge_platform_link(resolved, "newservice", make_link("https://newservice.example/t/1"))

    assert resolved.platform_links == {}
    assert resolved.link_rejections == []
    assert resolved.warnings == []


def test_a_manual_link_skips_the_host_list_here() -> None:
    resolved = ResolvedTrack()
    manual = make_link("https://evil.example/track/1", MatchClass.MANUAL)

    merge.merge_platform_link(resolved, "tidal", manual)

    assert "tidal" in resolved.platform_links
    assert resolved.link_rejections == []


def test_a_stored_automatic_link_is_kept_without_a_host_check() -> None:
    resolved = ResolvedTrack()

    merge.merge_stored_platform_links(
        resolved,
        {
            "tidal": {
                "url": "https://old.example/track/1",
                "source": "songlink",
                "match": "aggregator",
            }
        },
    )

    assert "tidal" in resolved.platform_links
    assert resolved.link_rejections == []
