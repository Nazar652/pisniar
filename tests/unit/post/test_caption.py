"""`build_caption`: the D-29 template, S2-A9 (limit checked at both 4096
and 1024 — 1024 is step 3's real limit and degrades routinely), and the
deterministic degradation order (genres -> platforms -> release title,
never the first line/artist hashtags/channel link)."""

from __future__ import annotations

from pisniar.post.caption import CHANNEL_LINK_URL, build_caption
from pisniar.post.render import LinkSegment, plain_text, visible_length
from pisniar.resolve.types import ArtistInfo, GenreInfo, MatchClass, PlatformLink

SPOTIFY_LINK = PlatformLink(
    url="https://open.spotify.com/track/x", source="spotify", match=MatchClass.EXACT_ISRC
)
YTM_LINK = PlatformLink(
    url="https://music.youtube.com/watch?v=x", source="ytmusic", match=MatchClass.CONFIRMED
)
DEEZER_LINK = PlatformLink(
    url="https://www.deezer.com/track/1", source="deezer", match=MatchClass.EXACT_ISRC
)
TIDAL_LINK = PlatformLink(
    url="https://listen.tidal.com/track/1", source="songlink", match=MatchClass.EXACT_ISRC
)


def test_simple_single_matches_template_shape() -> None:
    document, warnings = build_caption(
        artists=[ArtistInfo(name="Krovna")],
        title="Berserker",
        year=2026,
        release_type="single",
        release_title=None,
        album_id_spotify=None,
        genres=[GenreInfo(name="рок", hashtag=None, source="deezer")],
        platform_links={"spotify": SPOTIFY_LINK, "youtubeMusic": YTM_LINK},
    )

    text = plain_text(document)
    assert text.startswith("Krovna — Berserker\n\n")
    assert "#Krovna · 2026 · сингл" in text
    assert "#рок" in text
    assert "Spotify · YouTube Music" in text
    assert text.endswith("Піснярня 🇺🇦")
    assert warnings == []


def test_album_with_link_uses_named_release_title() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="Тол")],
        title="Сумніви",
        year=2013,
        release_type="album",
        release_title="Синдром Бажання",
        album_id_spotify="6N9PS4QXF1D0OWPk0Sxtb4",
        genres=[],
        platform_links={"spotify": SPOTIFY_LINK},
    )

    assert "альбом «Синдром Бажання»" in plain_text(document)
    release_links = [
        segment
        for segment in document.segments
        if hasattr(segment, "href")
        and segment.href == "https://open.spotify.com/album/6N9PS4QXF1D0OWPk0Sxtb4"
    ]
    assert len(release_links) == 1


def test_multiple_artists_joined_by_hashtags_with_space() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="Burned Time Machine"), ArtistInfo(name="0%Mercury")],
        title="Покаяння",
        year=2026,
        release_type="single",
        release_title=None,
        album_id_spotify=None,
        genres=[],
        platform_links={},
    )

    text = plain_text(document)
    assert "Burned Time Machine, 0%Mercury — Покаяння" in text
    assert "#BurnedTimeMachine #0Mercury" in text


def test_manual_artist_hashtag_override_is_used() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="0%Mercury", hashtag="ZeroPercentMercury")],
        title="X",
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        genres=[],
        platform_links={},
    )

    assert "#ZeroPercentMercury" in plain_text(document)


def test_digit_only_genre_name_is_skipped_with_warning() -> None:
    _, warnings = build_caption(
        artists=[ArtistInfo(name="Sick Solution")],
        title="Зашморг",
        year=1995,
        release_type="single",
        release_title=None,
        album_id_spotify=None,
        genres=[GenreInfo(name="1995", hashtag=None, source="deezer")],
        platform_links={},
    )

    assert any("1995" in warning for warning in warnings)


def test_caption_fits_within_4096_for_a_rich_post() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="hackedface")],
        title="but wait...",
        year=2025,
        release_type="album",
        release_title="SEASHELL",
        album_id_spotify="alb1",
        genres=[
            GenreInfo(name=name, hashtag=None, source="deezer")
            for name in ["рок", "метал", "альтернатива"]
        ],
        platform_links={
            "spotify": SPOTIFY_LINK,
            "youtubeMusic": YTM_LINK,
            "deezer": DEEZER_LINK,
            "tidal": TIDAL_LINK,
        },
    )

    assert visible_length(document) <= 4096


def _many_genres(count: int) -> list[GenreInfo]:
    return [GenreInfo(name=f"жанр{i}", hashtag=f"Жанр{i}", source="deezer") for i in range(count)]


def _many_platforms(count: int) -> dict[str, PlatformLink]:
    base = {"spotify": SPOTIFY_LINK, "youtubeMusic": YTM_LINK}
    for i in range(count):
        base[f"platform{i}"] = PlatformLink(
            url=f"https://example.com/platform{i}", source="songlink", match=MatchClass.EXACT_ISRC
        )
    return base


def test_caption_degrades_to_fit_1024_limit() -> None:
    """Step 3's real limit (1024, video caption) — degradation must fire
    routinely here, not just in a contrived edge case."""
    document, _ = build_caption(
        artists=[ArtistInfo(name="Burned Time Machine"), ArtistInfo(name="0%Mercury")],
        title="A Reasonably Long Track Title For This Test",
        year=2026,
        release_type="album",
        release_title="A Reasonably Long Release Title For This Test Too",
        album_id_spotify="alb1",
        genres=_many_genres(15),
        platform_links=_many_platforms(15),
        limit=1024,
    )

    assert visible_length(document) <= 1024


def test_degradation_drops_genres_before_platforms() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=2020,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        genres=_many_genres(30),
        platform_links={"spotify": SPOTIFY_LINK, "deezer": DEEZER_LINK},
        limit=200,
    )

    text = plain_text(document)
    assert "Spotify" in text
    assert "жанр" not in text


def test_degradation_protects_spotify_and_youtube_music_until_last() -> None:
    # limit=60 is exactly the minimal document's length with only the two
    # protected platforms — any looser and a "platformN" would still fit.
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=2020,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        genres=[],
        platform_links=_many_platforms(30),
        limit=60,
    )

    text = plain_text(document)
    assert "Spotify" in text
    assert "YouTube Music" in text
    assert "platform0" not in text


def test_degradation_never_touches_first_line_or_channel_link() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="Very Long Artist Name Indeed")],
        title="An Extremely Long Track Title That Keeps Going On And On",
        year=2026,
        release_type="album",
        release_title="Also A Very Long Release Title That Keeps Going",
        album_id_spotify="alb1",
        genres=_many_genres(20),
        platform_links=_many_platforms(20),
        limit=50,
    )

    text = plain_text(document)
    assert text.startswith("Very Long Artist Name Indeed — An Extremely Long Track Title")
    assert text.endswith("Піснярня 🇺🇦")
    channel_links = [
        segment
        for segment in document.segments
        if getattr(segment, "href", None) == CHANNEL_LINK_URL
    ]
    assert len(channel_links) == 1


def test_degradation_reports_warning_when_still_over_limit() -> None:
    _, warnings = build_caption(
        artists=[ArtistInfo(name="Very Long Artist Name Indeed Times A Hundred")],
        title="An Extremely Long Track Title That Cannot Possibly Fit In A Tiny Limit",
        year=2026,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        genres=[],
        platform_links={},
        limit=10,
    )

    assert any("ліміт" in warning for warning in warnings)


def test_a_malformed_album_id_degrades_to_plain_text_with_a_warning() -> None:
    """R2-16: `escape_attribute` stops the XSS, but a space in the id is a
    Telegram 400 on the whole post — a missing link costs far less."""
    document, warnings = build_caption(
        artists=[ArtistInfo(name="Тол")],
        title="Сумніви",
        year=2013,
        release_type="album",
        release_title="Синдром Бажання",
        album_id_spotify="bad id with spaces",
        genres=[],
        platform_links={"spotify": SPOTIFY_LINK},
    )

    assert "альбом «Синдром Бажання»" in plain_text(document)
    assert not [
        segment
        for segment in document.segments
        if isinstance(segment, LinkSegment) and "/album/" in segment.href
    ]
    assert any("base62" in warning for warning in warnings)
