"""`merge`: per-field source priority, the four-tier `platform_links` tie-break
(`manual > exact_isrc > aggregator > confirmed`), the unsafe-scheme guard on the way
in, and the stored-row merge that keeps an admin's overrides across a re-resolve.
"""

from __future__ import annotations

from pisniar.resolve.merge import (
    keep_stored_title_and_artists_after_source_failure,
    merge_artists,
    merge_isrc,
    merge_platform_link,
    merge_stored_artists,
    merge_stored_platform_links,
    merge_stored_scalars,
    merge_title,
    merge_year,
)
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack


def make_link(
    source: str, match: MatchClass, url: str = "https://www.deezer.com/track/1"
) -> PlatformLink:
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
        track, "youtube", make_link("user_input", MatchClass.MANUAL, "https://www.youtube.com/user")
    )

    merge_platform_link(
        track,
        "youtube",
        make_link("deezer", MatchClass.EXACT_ISRC, "https://www.youtube.com/other"),
    )
    merge_platform_link(
        track,
        "youtube",
        make_link("itunes", MatchClass.CONFIRMED, "https://www.youtube.com/another"),
    )

    assert track.platform_links["youtube"].url == "https://www.youtube.com/user"
    assert track.platform_links["youtube"].source == "user_input"


def test_platform_link_manual_is_never_overwritten_even_if_applied_first() -> None:
    track = ResolvedTrack()

    merge_platform_link(track, "spotify", make_link("spotify", MatchClass.EXACT_ISRC))
    merge_platform_link(
        track, "spotify", make_link("manual", MatchClass.MANUAL, "https://open.spotify.com/manual")
    )
    merge_platform_link(
        track,
        "spotify",
        make_link("spotify", MatchClass.EXACT_ISRC, "https://open.spotify.com/auto"),
    )

    assert track.platform_links["spotify"].url == "https://open.spotify.com/manual"


def test_platform_link_aggregator_beats_confirmed_but_loses_to_direct_isrc() -> None:
    track = ResolvedTrack()

    merge_platform_link(
        track,
        "tidal",
        make_link("songlink", MatchClass.AGGREGATOR, "https://listen.tidal.com/songlink"),
    )
    merge_platform_link(
        track,
        "tidal",
        make_link("itunes", MatchClass.CONFIRMED, "https://listen.tidal.com/confirmed"),
    )

    assert track.platform_links["tidal"].url == "https://listen.tidal.com/songlink"

    merge_platform_link(
        track,
        "tidal",
        make_link("deezer", MatchClass.EXACT_ISRC, "https://listen.tidal.com/direct"),
    )

    assert track.platform_links["tidal"].url == "https://listen.tidal.com/direct"


def test_platform_link_does_not_downgrade_on_equal_priority_resubmission() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC, "https://www.deezer.com/first")
    )

    merge_platform_link(
        track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC, "https://www.deezer.com/second")
    )

    assert track.platform_links["deezer"].url == "https://www.deezer.com/first"


def test_unsafe_scheme_is_rejected_on_the_way_in_with_a_warning() -> None:
    """MusicBrainz maps platform by host from a publicly editable database, so one
    `javascript:` URL must not fail the whole post later."""
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
        track,
        "spotify",
        make_link("spotify", MatchClass.CONFIRMED, "https://open.spotify.com/safe"),
    )

    merge_platform_link(
        track, "spotify", make_link("manual", MatchClass.MANUAL, "data:text/html,x")
    )

    assert track.platform_links["spotify"].url == "https://open.spotify.com/safe"


def test_stored_manual_link_survives_a_fresh_exact_isrc_resolve() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track,
        "spotify",
        make_link("spotify", MatchClass.EXACT_ISRC, "https://open.spotify.com/fresh"),
    )

    merge_stored_platform_links(
        track,
        {
            "spotify": {
                "url": "https://open.spotify.com/stored-manual",
                "source": "user_input",
                "match": "manual",
            }
        },
    )

    assert track.platform_links["spotify"].url == "https://open.spotify.com/stored-manual"


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
        track, "deezer", make_link("deezer", MatchClass.EXACT_ISRC, "https://www.deezer.com/fresh")
    )

    merge_stored_platform_links(
        track,
        {
            "deezer": {
                "url": "https://www.deezer.com/stale",
                "source": "deezer",
                "match": "exact_isrc",
            }
        },
    )

    assert track.platform_links["deezer"].url == "https://www.deezer.com/fresh"


def test_stored_link_with_an_unreadable_match_class_lands_in_the_bottom_tier() -> None:
    track = ResolvedTrack()
    merge_platform_link(
        track, "tidal", make_link("deezer", MatchClass.EXACT_ISRC, "https://listen.tidal.com/fresh")
    )

    merge_stored_platform_links(
        track,
        {"tidal": {"url": "https://listen.tidal.com/legacy", "source": "?", "match": "nonsense"}},
    )

    assert track.platform_links["tidal"].url == "https://listen.tidal.com/fresh"


def test_stored_artist_hashtag_survives_a_resolve_that_returned_none() -> None:
    """`artists[].hashtag` is the only manual override there is, and a `/retry`
    must not write `null` over it."""
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


def test_stored_artist_entry_for_a_different_name_is_ignored_when_it_holds_no_override() -> None:
    """Nothing irreplaceable to lose, so the fresh name wins outright and the
    name-shape fallback below does not fire."""
    track = ResolvedTrack(artists=[ArtistInfo(name="New Name")])

    merge_stored_artists(track, [{"name": "Old Name", "hashtag": None}])

    assert [artist.name for artist in track.artists] == ["New Name"]
    assert track.artists[0].hashtag is None


def test_stored_artist_key_matching_ignores_case_and_repeated_whitespace() -> None:
    track = ResolvedTrack(artists=[ArtistInfo(name="0%Mercury")])

    merge_stored_artists(track, [{"name": "0%mercury", "hashtag": "ZeroPercentMercury"}])

    assert track.artists[0].hashtag == "ZeroPercentMercury"


def test_stored_artists_with_a_hashtag_survive_a_change_of_name_shape() -> None:
    """`spotify` lists artists separately while `songlink:spotify` joins them, so
    one Spotify outage leaves no key to find the override by."""
    track = ResolvedTrack(artists=[ArtistInfo(name="Burned Time Machine, 0%Mercury")])

    merge_stored_artists(
        track,
        [
            {"name": "Burned Time Machine", "hashtag": None},
            {"name": "0%Mercury", "hashtag": "ZeroPercentMercury"},
        ],
    )

    assert [artist.name for artist in track.artists] == [
        "Burned Time Machine",
        "0%Mercury",
    ]
    assert track.artists[1].hashtag == "ZeroPercentMercury"
    assert track.warnings


def test_merge_stored_scalars_keeps_a_value_the_fresh_resolve_lost() -> None:
    """After the TTL a re-resolve really goes to the network, and one transient
    failure must not null a good `isrc` and with it the duplicate warning."""
    track = ResolvedTrack(title="Creep")

    merge_stored_scalars(track, {"isrc": "GBAYE9200070", "title": None, "year": 1993})

    assert track.isrc == "GBAYE9200070"
    assert track.year == 1993
    assert track.title == "Creep"


def test_merge_stored_scalars_keeps_the_stored_album_id() -> None:
    """The album link is built from this column, so a re-resolve without song.link
    must not null it."""
    track = ResolvedTrack(title="Сумніви")

    merge_stored_scalars(track, {"album_id_spotify": "4yYXqedvSKuJlgI2IWsB0j"})

    assert track.album_id_spotify == "4yYXqedvSKuJlgI2IWsB0j"


def test_merge_stored_scalars_keeps_the_stored_duration() -> None:
    """The download is checked against `track.duration_ms`, so one failed source
    on a `/retry` must not null the yardstick."""
    track = ResolvedTrack(title="Creep")

    merge_stored_scalars(track, {"duration_ms": 238000})

    assert track.duration_ms == 238000


def test_merge_stored_scalars_never_overwrites_a_fresh_value() -> None:
    track = ResolvedTrack(isrc="UAAAA2500001")

    merge_stored_scalars(track, {"isrc": "GBAYE9200070"})

    assert track.isrc == "UAAAA2500001"


def test_a_degraded_resolve_keeps_the_stored_title_and_artists() -> None:
    """The fresh title is the raw `songlink:youtube` clip name: lower priority, but
    not `None`, so only this guard keeps the stored one."""
    track = ResolvedTrack(
        title="Bruise Pristine",
        artists=[ArtistInfo(name="Валентин Стрыкало-Бувайте")],
        source_failures=["spotify:bridge: HTTP 503"],
    )

    keep_stored_title_and_artists_after_source_failure(
        track,
        "Bruise Pristine (Official Video)",
        [{"name": "Placebo", "hashtag": "Placebo", "spotify_id": "abc"}],
    )

    assert track.title == "Bruise Pristine (Official Video)"
    assert track.artists == [ArtistInfo(name="Placebo", hashtag="Placebo", spotify_id="abc")]


def test_a_degraded_resolve_keeps_the_stored_title_when_only_youtube_answered() -> None:
    """The fresh title is the raw clip name `songlink:youtube` gave, so the stored
    clean one still wins."""
    track = ResolvedTrack(source_failures=["spotify:isrc: HTTP 503"])
    merge_title(track, "vesnavoseny - ніч минала", "songlink:youtube")
    merge_artists(track, ["vesnavoseny"], "songlink:youtube")

    keep_stored_title_and_artists_after_source_failure(
        track, "ніч минала", [{"name": "vesnavoseny", "hashtag": "vesnavoseny"}]
    )

    assert track.title == "ніч минала"
    assert track.artists == [ArtistInfo(name="vesnavoseny", hashtag="vesnavoseny")]


def test_a_degraded_resolve_takes_the_fresh_title_from_a_strong_source() -> None:
    """Spotify answered this time, so its clean values must replace the stored
    clip-derived ones even while iTunes fails."""
    track = ResolvedTrack(source_failures=["itunes: timeout"])
    merge_title(track, "Bruise Pristine", "spotify")
    merge_artists(track, ["Placebo"], "spotify")

    keep_stored_title_and_artists_after_source_failure(
        track, "Bruise Pristine (Official Video)", [{"name": "Валентин Стрыкало-Бувайте"}]
    )

    assert track.title == "Bruise Pristine"
    assert track.artists == [ArtistInfo(name="Placebo")]


def test_a_complete_resolve_still_overwrites_the_stored_title_and_artists() -> None:
    """The guard is scoped to `source_failures`: with every source answering, the
    fresh result is the authority and must win."""
    track = ResolvedTrack(title="Creep", artists=[ArtistInfo(name="Radiohead")])

    keep_stored_title_and_artists_after_source_failure(
        track, "Creep (Official Video)", [{"name": "RadioheadVEVO"}]
    )

    assert track.title == "Creep"
    assert track.artists == [ArtistInfo(name="Radiohead")]


def test_a_degraded_resolve_keeps_its_own_values_when_nothing_is_stored() -> None:
    """First resolve of a track: an empty stored row must not blank the only
    metadata there is."""
    track = ResolvedTrack(
        title="Creep", artists=[ArtistInfo(name="Radiohead")], source_failures=["itunes: timeout"]
    )

    keep_stored_title_and_artists_after_source_failure(track, None, [])

    assert track.title == "Creep"
    assert track.artists == [ArtistInfo(name="Radiohead")]


def test_a_degraded_resolve_ignores_stored_artist_entries_without_a_name() -> None:
    track = ResolvedTrack(
        artists=[ArtistInfo(name="Uploader Channel")], source_failures=["spotify:isrc: HTTP 500"]
    )

    keep_stored_title_and_artists_after_source_failure(track, None, [{"hashtag": "X"}, "junk"])

    assert track.artists == [ArtistInfo(name="Uploader Channel")]
