"""`classify_url()`/`normalize_url()`: syntactic track/not-track detection
and the UNIQUE-key normalization contract (p.11 of the step 2 contract)."""

from __future__ import annotations

import pytest

from pisniar.resolve.urls import UrlKind, classify_url, normalize_url


@pytest.mark.parametrize(
    ("raw_url", "expected_platform", "expected_track_id", "expected_normalized"),
    [
        (
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc123",
            "spotify",
            "4uLU6hMCjMI75M1A2tKUQC",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        ),
        (
            "https://open.spotify.com/intl-uk/track/4uLU6hMCjMI75M1A2tKUQC",
            "spotify",
            "4uLU6hMCjMI75M1A2tKUQC",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        ),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=3",
            "youtube",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?si=xyz",
            "youtube",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "youtubeMusic",
            "dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.deezer.com/en/track/138547415",
            "deezer",
            "138547415",
            "https://www.deezer.com/track/138547415",
        ),
        (
            "https://music.apple.com/ua/song/creep/1424394174",
            "appleMusic",
            "1424394174",
            "https://music.apple.com/ua/song/creep/1424394174",
        ),
        (
            "https://music.apple.com/ua/album/pablo-honey/1440857781?i=1440857792&ls=1",
            "appleMusic",
            "1440857792",
            "https://music.apple.com/ua/album/pablo-honey/1440857781?i=1440857792",
        ),
        (
            "https://tidal.com/browse/track/12345678",
            "tidal",
            "12345678",
            "https://tidal.com/track/12345678",
        ),
        (
            "https://artist.bandcamp.com/track/song-title",
            "bandcamp",
            "song-title",
            "https://artist.bandcamp.com/track/song-title",
        ),
        (
            "https://soundcloud.com/some-artist/some-track?si=abc",
            "soundcloud",
            "some-artist/some-track",
            "https://soundcloud.com/some-artist/some-track",
        ),
    ],
)
def test_classify_url_recognizes_track_links(
    raw_url: str, expected_platform: str, expected_track_id: str, expected_normalized: str
) -> None:
    classified = classify_url(raw_url)

    assert classified.kind is UrlKind.TRACK
    assert classified.platform == expected_platform
    assert classified.track_id == expected_track_id
    assert classified.normalized_url == expected_normalized


@pytest.mark.parametrize(
    ("raw_url", "expected_reason"),
    [
        ("https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j", "album"),
        ("https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb", "artist"),
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "playlist"),
        ("https://www.youtube.com/playlist?list=PL123", "playlist"),
        ("https://www.youtube.com/@SomeChannel", "artist"),
        ("https://www.deezer.com/en/album/302127", "album"),
        ("https://music.apple.com/ua/album/pablo-honey/1440857781", "album"),
        ("https://music.apple.com/ua/artist/radiohead/657515", "artist"),
        ("https://tidal.com/browse/album/12345", "album"),
        ("https://artist.bandcamp.com/album/some-album", "album"),
        ("https://soundcloud.com/some-artist", "artist"),
        ("https://soundcloud.com/some-artist/sets/some-album", "album"),
    ],
)
def test_classify_url_recognizes_not_track_links(raw_url: str, expected_reason: str) -> None:
    classified = classify_url(raw_url)

    assert classified.kind is UrlKind.NOT_TRACK
    assert classified.not_track_reason == expected_reason
    assert classified.platform is None


@pytest.mark.parametrize(
    "raw_url",
    [
        "not a url at all",
        "ftp://example.com/track/1",
        "https://example.com/whatever",
        "https://open.spotify.com/",
        "",
    ],
)
def test_classify_url_returns_unknown_for_unrecognized_input(raw_url: str) -> None:
    classified = classify_url(raw_url)

    assert classified.kind is UrlKind.UNKNOWN
    assert classified.platform is None
    assert classified.track_id is None


def test_normalize_url_strips_tracking_query_params() -> None:
    tracked = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=tracking-noise"
    clean = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"

    assert normalize_url(tracked) == normalize_url(clean)


def test_normalize_url_is_stable_across_equivalent_youtube_forms() -> None:
    watch_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    short_url = "https://youtu.be/dQw4w9WgXcQ"

    assert normalize_url(watch_url) == normalize_url(short_url)


def test_youtube_and_youtube_music_normalize_to_different_urls() -> None:
    youtube = normalize_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    youtube_music = normalize_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ")

    assert youtube != youtube_music
