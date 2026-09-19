"""`merge`: per-field source priority, the four-tier `platform_links`
tie-break (`manual > exact_isrc > aggregator > confirmed`, D-39), the
unsafe-scheme guard on the way in (F-8), and the stored-row merge that keeps
an admin's overrides alive across a re-resolve (D-42)."""

from __future__ import annotations

from pisniar.resolve.merge import (
    merge_artists,
    merge_isrc,
    merge_platform_link,
    merge_stored_artists,
    merge_stored_platform_links,
    merge_title,
    merge_year,
)
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack


def make_link(source: str, match: MatchClass, url: str = "https://example.com/x") -> PlatformLink:
    return PlatformLink(url=url, source=source, match=match)


def test_higher_priority_metadata_source_wins() -> None:
    track = ResolvedTrack()

    merge_title(track, "Dirty Title (Official Video)", "songlink:youtube")
    merge_title(track, "Clean Title", "spotify")

    assert track.title == "Clean Title"


def test_lower_priority_metadata_source_does_not_overwrite() -> None:
    track = ResolvedTrack()

    merge_title(track, "Clean Title", "spotify")
    merge_title(track, "Dirty Title (Official Video)", "songlink:youtube")

    assert track.title == "Clean Title"


def test_isrc_from_spotify_beats_musicbrainz_regardless_of_order() -> None:
    track = ResolvedTrack()

    merge_isrc(track, "MB000000000", "musicbrainz")
    merge_isrc(track, "SP000000000", "spotify")

    assert track.isrc == "SP000000000"


def test_year_from_deezer_beats_musicbrainz() -> None:
    track = ResolvedTrack()

    merge_year(track, 2020, "musicbrainz")
    merge_year(track, 2026, "deezer")

    assert track.year == 2026


def test_merge_artists_wraps_names_and_respects_priority() -> None:
    track = ResolvedTrack()

    merge_artists(track, ["Dirty Name - Topic"], "songlink:youtube")
    merge_artists(track, ["Clean Name"], "spotify")

    assert [artist.name for artist in track.artists] == ["Clean Name"]


def test_merge_field_ignores_none_and_empty_values() -> None:
    track = ResolvedTrack()
    merge_title(track, "Real Title", "spotify")

    merge_title(track, None, "songlink:youtube")

    assert track.title == "Real Title"


def test_platform_link_exact_isrc_beats_confirmed() -> None:
    track = ResolvedTrack()

    merge_platform_link(track, "deezer", make_link("deezer", MatchClass.CONFIRMED))
    merge_platform_link(track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC))

    assert track.platform_links["deezer"].match is MatchClass.EXACT_ISRC


def test_platform_link_manual_beats_exact_isrc_and_confirmed() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "youtube", make_link("user_input", MatchClass.MANUAL, "https://example.com/user")
    )

    merge_platform_link(
        track, "youtube", make_link("deezer", MatchClass.EXACT_ISRC, "https://example.com/other")
    )
    merge_platform_link(
        track, "youtube", make_link("itunes", MatchClass.CONFIRMED, "https://example.com/another")
    )

    assert track.platform_links["youtube"].url == "https://example.com/user"
    assert track.platform_links["youtube"].source == "user_input"


def test_platform_link_manual_is_never_overwritten_even_if_applied_first() -> None:
    track = ResolvedTrack()

    merge_platform_link(track, "spotify", make_link("spotify", MatchClass.EXACT_ISRC))
    merge_platform_link(
        track, "spotify", make_link("manual", MatchClass.MANUAL, "https://example.com/manual")
    )
    merge_platform_link(
        track, "spotify", make_link("spotify", MatchClass.EXACT_ISRC, "https://example.com/auto")
    )

    assert track.platform_links["spotify"].url == "https://example.com/manual"


def test_platform_link_aggregator_beats_confirmed_but_loses_to_direct_isrc() -> None:
    track = ResolvedTrack()

    merge_platform_link(
        track, "tidal", make_link("songlink", MatchClass.AGGREGATOR, "https://example.com/songlink")
    )
    merge_platform_link(
        track, "tidal", make_link("itunes", MatchClass.CONFIRMED, "https://example.com/confirmed")
    )

    assert track.platform_links["tidal"].url == "https://example.com/songlink"

    merge_platform_link(
        track, "tidal", make_link("deezer", MatchClass.EXACT_ISRC, "https://example.com/direct")
    )

    assert track.platform_links["tidal"].url == "https://example.com/direct"


def test_platform_link_does_not_downgrade_on_equal_priority_resubmission() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC, "https://example.com/first")
    )

    merge_platform_link(
        track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC, "https://example.com/second")
    )

    assert track.platform_links["deezer"].url == "https://example.com/first"


def test_unsafe_scheme_is_rejected_on_the_way_in_with_a_warning() -> None:
    """F-8: MusicBrainz maps platform by host from a publicly editable
    database, so a `javascript:` URL used to land in `platform_links` and
    only blow up later, failing the whole post over one bad link."""
    track = ResolvedTrack()

    merge_platform_link(
        track,
        "spotify",
        make_link("musicbrainz", MatchClass.EXACT_ISRC, "javascript://open.spotify.com/track/x"),
    )

    assert "spotify" not in track.platform_links
    assert any("spotify" in warning for warning in track.warnings)


def test_unsafe_candidate_does_not_displace_an_already_merged_safe_link() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "spotify", make_link("spotify", MatchClass.CONFIRMED, "https://example.com/safe")
    )

    merge_platform_link(
        track, "spotify", make_link("manual", MatchClass.MANUAL, "data:text/html,x")
    )

    assert track.platform_links["spotify"].url == "https://example.com/safe"


def test_stored_manual_link_survives_a_fresh_exact_isrc_resolve() -> None:
    """D-42: step 5 writes a `manual` link, and the first `/retry` used to
    overwrite it with whatever the automation happened to find."""
    track = ResolvedTrack()
    merge_platform_link(
        track, "spotify", make_link("spotify", MatchClass.EXACT_ISRC, "https://example.com/fresh")
    )

    merge_stored_platform_links(
        track,
        {
            "spotify": {
                "url": "https://example.com/stored-manual",
                "source": "user_input",
                "match": "manual",
            }
        },
    )

    assert track.platform_links["spotify"].url == "https://example.com/stored-manual"


def test_stored_platform_missing_from_the_fresh_resolve_is_kept() -> None:
    """Transport errors are deliberately not cached, so a source that was
    merely unreachable this time must not shrink `platform_links`."""
    track = ResolvedTrack()

    merge_stored_platform_links(
        track,
        {
            "deezer": {
                "url": "https://www.deezer.com/track/1",
                "source": "deezer",
                "match": "exact_isrc",
            }
        },
    )

    assert track.platform_links["deezer"].url == "https://www.deezer.com/track/1"


def test_fresh_result_wins_a_tie_against_the_same_stored_class() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC, "https://example.com/fresh")
    )

    merge_stored_platform_links(
        track,
        {"deezer": {"url": "https://example.com/stale", "source": "deezer", "match": "exact_isrc"}},
    )

    assert track.platform_links["deezer"].url == "https://example.com/fresh"


def test_stored_link_with_an_unreadable_match_class_lands_in_the_bottom_tier() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "tidal", make_link("deezer", MatchClass.EXACT_ISRC, "https://example.com/fresh")
    )

    merge_stored_platform_links(
        track, {"tidal": {"url": "https://example.com/legacy", "source": "?", "match": "nonsense"}}
    )

    assert track.platform_links["tidal"].url == "https://example.com/fresh"


def test_stored_artist_hashtag_survives_a_resolve_that_returned_none() -> None:
    """D-29 makes `artists[].hashtag` the only manual override there is;
    `/retry` used to write `null` straight over it."""
    track = ResolvedTrack(artists=[ArtistInfo(name="0%Mercury")])

    merge_stored_artists(
        track, [{"name": "0%Mercury", "hashtag": "ZeroPercentMercury", "spotify_id": "sp1"}]
    )

    assert track.artists[0].hashtag == "ZeroPercentMercury"
    assert track.artists[0].spotify_id == "sp1"


def test_fresh_artist_hashtag_is_not_replaced_by_the_stored_one() -> None:
    track = ResolvedTrack(artists=[ArtistInfo(name="A", hashtag="Fresh")])

    merge_stored_artists(track, [{"name": "A", "hashtag": "Stored"}])

    assert track.artists[0].hashtag == "Fresh"


def test_stored_artists_are_kept_when_the_resolve_returned_none_at_all() -> None:
    track = ResolvedTrack()

    merge_stored_artists(track, [{"name": "A", "hashtag": "Stored", "spotify_id": None}])

    assert [artist.name for artist in track.artists] == ["A"]
    assert track.artists[0].hashtag == "Stored"


def test_stored_artist_entry_for_a_different_name_is_ignored() -> None:
    track = ResolvedTrack(artists=[ArtistInfo(name="New Name")])

    merge_stored_artists(track, [{"name": "Old Name", "hashtag": "Stale"}])

    assert [artist.name for artist in track.artists] == ["New Name"]
    assert track.artists[0].hashtag is None
