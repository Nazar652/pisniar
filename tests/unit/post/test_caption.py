"""`build_caption`: the post template, the 1024 media limit as the default, the language row, the clip first, and the
degradation order: genres -> platforms -> release title, never the first line, artist hashtags, languages or channel
link.
"""

from __future__ import annotations

import pytest

from pisniar.post.caption import (
    CHANNEL_LINK_URL,
    MEDIA_CAPTION_LIMIT,
    build_caption,
    build_caption_with_report,
)
from pisniar.post.render import LinkSegment, plain_text, visible_length
from pisniar.resolve.types import ArtistInfo, GenreInfo, MatchClass, PlatformLink

SPOTIFY_LINK = PlatformLink(url="https://open.spotify.com/track/x", source="spotify", match=MatchClass.EXACT_ISRC)
YOUTUBE_MUSIC_LINK = PlatformLink(
    url="https://music.youtube.com/watch?v=x", source="ytmusic", match=MatchClass.CONFIRMED
)
DEEZER_LINK = PlatformLink(url="https://www.deezer.com/track/1", source="deezer", match=MatchClass.EXACT_ISRC)
TIDAL_LINK = PlatformLink(url="https://listen.tidal.com/track/1", source="songlink", match=MatchClass.EXACT_ISRC)
CLIP_LINK = PlatformLink(url="https://www.youtube.com/watch?v=clip", source="user_input", match=MatchClass.MANUAL)
TOPIC_UPLOAD_LINK = PlatformLink(
    url="https://www.youtube.com/watch?v=topic", source="ytmusic", match=MatchClass.AGGREGATOR
)


def test_simple_single_matches_template_shape() -> None:
    document, warnings = build_caption(
        artists=[ArtistInfo(name="Krovna")],
        title="Berserker",
        year=2026,
        release_type="single",
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=[GenreInfo(name="рок", hashtag=None, source="deezer")],
        platform_links={"spotify": SPOTIFY_LINK, "youtubeMusic": YOUTUBE_MUSIC_LINK},
    )

    text = plain_text(document)
    assert text.startswith("Krovna — Berserker\n\n")
    assert "#Krovna · 2026 · сингл" in text
    assert "#українська · #рок" in text
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
        languages=["українська"],
        genres=[],
        platform_links={"spotify": SPOTIFY_LINK},
    )

    assert "альбом «Синдром Бажання»" in plain_text(document)
    release_links = [
        segment
        for segment in document.segments
        if hasattr(segment, "href") and segment.href == "https://open.spotify.com/album/6N9PS4QXF1D0OWPk0Sxtb4"
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
        languages=["українська"],
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
        languages=["українська"],
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
        languages=["українська"],
        genres=[GenreInfo(name="1995", hashtag=None, source="deezer")],
        platform_links={},
    )

    assert any("1995" in warning for warning in warnings)


def test_a_rich_post_fits_the_media_limit_without_degrading() -> None:
    """a realistic post is 150-650 units against 1024, so nothing is dropped and degradation stays the rare branch."""
    document, warnings = build_caption(
        artists=[ArtistInfo(name="hackedface")],
        title="but wait...",
        year=2025,
        release_type="album",
        release_title="SEASHELL",
        album_id_spotify="4yYXqedvSKuJlgI2IWsB0j",
        languages=["українська"],
        genres=[GenreInfo(name=name, hashtag=None, source="deezer") for name in ["рок", "метал", "альтернатива"]],
        platform_links={
            "spotify": SPOTIFY_LINK,
            "youtubeMusic": YOUTUBE_MUSIC_LINK,
            "deezer": DEEZER_LINK,
            "tidal": TIDAL_LINK,
        },
    )

    text = plain_text(document)
    assert visible_length(document) <= MEDIA_CAPTION_LIMIT
    assert "#альтернатива" in text
    assert "TIDAL" in text
    assert warnings == []


def test_the_default_limit_is_the_media_limit_not_4096() -> None:
    """the draft the admin approves must already be built for a photo caption, or
    the channel would show a different text."""
    assert MEDIA_CAPTION_LIMIT == 1024

    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=2020,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=many_genres(200),
        platform_links={"spotify": SPOTIFY_LINK},
    )

    assert visible_length(document) <= 1024


def test_several_languages_share_one_group_before_the_genres() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська", "англійська"],
        genres=[GenreInfo(name=name, hashtag=None, source="deezer") for name in ["альтернатива", "рок"]],
        platform_links={},
    )

    assert "#українська #англійська · #альтернатива #рок" in plain_text(document)


def test_the_language_row_survives_without_any_genre() -> None:
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=[],
        platform_links={},
    )

    text = plain_text(document)
    assert "#українська" in text
    assert " · #" not in text


def test_a_manual_youtube_link_leads_the_row_and_is_marked_as_a_clip() -> None:
    """`MANUAL` covers both the link the admin submits and the one the Mini App will paste."""
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=[],
        platform_links={
            "spotify": SPOTIFY_LINK,
            "youtube": CLIP_LINK,
            "youtubeMusic": YOUTUBE_MUSIC_LINK,
        },
    )

    assert "YouTube (кліп) · Spotify · YouTube Music" in plain_text(document)


def test_an_automatic_youtube_link_is_neither_first_nor_called_a_clip() -> None:
    """`ytmusic` can return a `youtube` link with the same `videoId` as YouTube Music: a Topic upload, not a clip."""
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=[],
        platform_links={
            "spotify": SPOTIFY_LINK,
            "youtube": TOPIC_UPLOAD_LINK,
            "youtubeMusic": YOUTUBE_MUSIC_LINK,
        },
    )

    text = plain_text(document)
    assert "кліп" not in text
    assert "Spotify · YouTube Music · YouTube" in text


def many_genres(count: int) -> list[GenreInfo]:
    return [GenreInfo(name=f"жанр{index}", hashtag=f"Жанр{index}", source="deezer") for index in range(count)]


def many_platforms(count: int) -> dict[str, PlatformLink]:
    base = {"spotify": SPOTIFY_LINK, "youtubeMusic": YOUTUBE_MUSIC_LINK}
    for index in range(count):
        base[f"platform{index}"] = PlatformLink(
            url=f"https://example.com/platform{index}",
            source="songlink",
            match=MatchClass.EXACT_ISRC,
        )
    return base


def test_caption_degrades_to_fit_1024_limit() -> None:
    """at the real 1024 limit degradation must fire routinely, not just in a contrived edge case."""
    document, _ = build_caption(
        artists=[ArtistInfo(name="Burned Time Machine"), ArtistInfo(name="0%Mercury")],
        title="A Reasonably Long Track Title For This Test",
        year=2026,
        release_type="album",
        release_title="A Reasonably Long Release Title For This Test Too",
        album_id_spotify="alb1",
        languages=["українська"],
        genres=many_genres(15),
        platform_links=many_platforms(15),
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
        languages=["українська"],
        genres=many_genres(30),
        platform_links={"spotify": SPOTIFY_LINK, "deezer": DEEZER_LINK},
        limit=200,
    )

    text = plain_text(document)
    assert "Spotify" in text
    assert "жанр" not in text


def test_degradation_protects_spotify_and_youtube_music_until_last() -> None:
    # 72 sits between the minimum with only the two protected platforms (68 units,
    # language row included) and the 80 a third platform would need.
    document, _ = build_caption(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=2020,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=[],
        platform_links=many_platforms(30),
        limit=72,
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
        languages=["українська"],
        genres=many_genres(20),
        platform_links=many_platforms(20),
        limit=50,
    )

    text = plain_text(document)
    assert text.startswith("Very Long Artist Name Indeed — An Extremely Long Track Title")
    # the language is never degraded: it is always shown.
    assert "#українська" in text
    assert text.endswith("Піснярня 🇺🇦")
    channel_links = [segment for segment in document.segments if getattr(segment, "href", None) == CHANNEL_LINK_URL]
    assert len(channel_links) == 1


def test_degradation_reports_warning_when_still_over_limit() -> None:
    _, warnings = build_caption(
        artists=[ArtistInfo(name="Very Long Artist Name Indeed Times A Hundred")],
        title="An Extremely Long Track Title That Cannot Possibly Fit In A Tiny Limit",
        year=2026,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=[],
        platform_links={},
        limit=10,
    )

    assert any("ліміт" in warning for warning in warnings)


def test_a_malformed_album_id_degrades_to_plain_text_with_a_warning() -> None:
    """`escape_attribute` stops the XSS, but a space in the id is a Telegram 400
    on the whole post, and a missing link costs far less."""
    document, warnings = build_caption(
        artists=[ArtistInfo(name="Тол")],
        title="Сумніви",
        year=2013,
        release_type="album",
        release_title="Синдром Бажання",
        album_id_spotify="bad id with spaces",
        languages=["українська"],
        genres=[],
        platform_links={"spotify": SPOTIFY_LINK},
    )

    assert "альбом «Синдром Бажання»" in plain_text(document)
    assert not [
        segment for segment in document.segments if isinstance(segment, LinkSegment) and "/album/" in segment.href
    ]
    assert any("base62" in warning for warning in warnings)


@pytest.mark.parametrize(("release_type", "dropped"), [("album", True), ("ep", True), ("single", False)])
def test_degradation_drops_the_release_title_of_an_album_and_an_ep_only(release_type: str, dropped: bool) -> None:
    result = build_caption_with_report(
        artists=[ArtistInfo(name="A")],
        title="T",
        year=2020,
        release_type=release_type,
        release_title="Дуже довга назва релізу " * 4,
        album_id_spotify=None,
        languages=["українська"],
        genres=[],
        platform_links={"spotify": SPOTIFY_LINK},
        limit=110,
    )

    assert result.degradation.release_title_dropped is dropped
