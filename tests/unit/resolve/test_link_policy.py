"""`link_policy`: the host list against the live data, the manual-link rules and the
album link parser.
"""

from __future__ import annotations

import pytest

from pisniar.resolve import link_policy

# every (platform, host) pair found in `track.platform_links` of the live database
LIVE_HOSTS = [
    ("amazonMusic", "music.amazon.com"),
    ("amazonStore", "amazon.com"),
    ("anghami", "play.anghami.com"),
    ("appleMusic", "music.apple.com"),
    ("deezer", "www.deezer.com"),
    ("napster", "play.napster.com"),
    ("pandora", "www.pandora.com"),
    ("spotify", "open.spotify.com"),
    ("tidal", "listen.tidal.com"),
    ("youtube", "www.youtube.com"),
    ("youtubeMusic", "music.youtube.com"),
]
SPOTIFY_ID = "4yYXqedvSKuJlgI2IWsB0j"
VIDEO_ID = "n4bPzdIs_nc"


@pytest.mark.parametrize(("platform", "host"), LIVE_HOSTS)
def test_every_host_seen_in_live_data_is_allowed(platform: str, host: str) -> None:
    assert link_policy.automatic_link_problem(platform, f"https://{host}/x") is None


def test_a_host_of_another_platform_is_refused() -> None:
    problem = link_policy.automatic_link_problem("tidal", "https://open.spotify.com/track/x")
    assert problem is not None
    assert "open.spotify.com" in problem


@pytest.mark.parametrize(
    "url",
    [
        "http://listen.tidal.com/track/1",
        "https://listen.tidal.com:8443/track/1",
        "https://user:pass@listen.tidal.com/track/1",
        "https://listen.tidal.com.evil.example/track/1",
        "https://evil.example/listen.tidal.com",
        "javascript:alert(1)",
        "listen.tidal.com/track/1",
    ],
)
def test_a_lookalike_or_unsafe_url_is_refused(url: str) -> None:
    assert link_policy.automatic_link_problem("tidal", url) is not None


def test_an_unknown_platform_is_refused() -> None:
    problem = link_policy.automatic_link_problem("newservice", "https://newservice.example/x")
    assert problem is not None
    assert "newservice" in problem


def test_an_overlong_url_is_refused() -> None:
    url = "https://listen.tidal.com/track/" + "1" * 2100
    assert link_policy.automatic_link_problem("tidal", url) == "посилання задовге"


def test_a_bandcamp_artist_subdomain_is_allowed_but_a_lookalike_is_not() -> None:
    assert (
        link_policy.automatic_link_problem("bandcamp", "https://artist.bandcamp.com/track/a")
        is None
    )
    assert (
        link_policy.automatic_link_problem("bandcamp", "https://artist.bandcamp.com.evil.example/")
        is not None
    )


def test_a_regional_amazon_host_is_allowed() -> None:
    assert (
        link_policy.automatic_link_problem("amazonMusic", "https://music.amazon.de/albums/B0")
        is None
    )


def test_a_manual_track_link_is_stored_normalised() -> None:
    check = link_policy.check_manual_link("youtube", f"  https://youtu.be/{VIDEO_ID}  ")

    assert check.ok
    assert check.url == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_a_manual_link_to_an_album_is_refused_for_a_track_platform() -> None:
    check = link_policy.check_manual_link("spotify", f"https://open.spotify.com/album/{SPOTIFY_ID}")

    assert not check.ok
    assert check.url is None


def test_a_manual_link_must_belong_to_the_platform_it_is_filed_under() -> None:
    check = link_policy.check_manual_link(
        "youtube", f"https://music.youtube.com/watch?v={VIDEO_ID}"
    )

    assert not check.ok


@pytest.mark.parametrize("platform", ["youtube", "youtubeMusic"])
def test_a_manual_video_link_needs_a_valid_video_id(platform: str) -> None:
    host = "music.youtube.com" if platform == "youtubeMusic" else "www.youtube.com"

    check = link_policy.check_manual_link(platform, f"https://{host}/watch?v=short")

    assert not check.ok


def test_a_manual_link_on_a_host_outside_the_list_is_refused() -> None:
    check = link_policy.check_manual_link("spotify", "https://evil.example/track/abc")

    assert not check.ok


def test_a_manual_link_on_an_unclassified_platform_needs_only_the_host_check() -> None:
    check = link_policy.check_manual_link("pandora", " https://www.pandora.com/artist/x/y ")

    assert check.url == "https://www.pandora.com/artist/x/y"


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/album/{SPOTIFY_ID}",
        f"https://open.spotify.com/intl-uk/album/{SPOTIFY_ID}",
        f"https://open.spotify.com/album/{SPOTIFY_ID}?si=abc",
    ],
)
def test_an_album_link_gives_its_id(url: str) -> None:
    check = link_policy.parse_spotify_album_link(url)

    assert check.ok
    assert check.album_id == SPOTIFY_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://open.spotify.com/album/" + SPOTIFY_ID,
        "https://play.spotify.com/album/" + SPOTIFY_ID,
        f"https://open.spotify.com/track/{SPOTIFY_ID}",
        "https://open.spotify.com/album/short",
        "https://open.spotify.com/album/" + "a" * 23,
        "https://evil.example/album/" + SPOTIFY_ID,
        "https://open.spotify.com:444/album/" + SPOTIFY_ID,
    ],
)
def test_anything_but_a_spotify_album_link_is_refused(url: str) -> None:
    check = link_policy.parse_spotify_album_link(url)

    assert not check.ok
    assert check.album_id is None
