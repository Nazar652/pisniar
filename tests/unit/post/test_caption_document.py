"""`caption.build_document`: one attempt at the document, with the degradation decisions (`include_release_title`, the
genre and platform lists) already made by the caller, and every warning collected into the list it is given."""

from __future__ import annotations

from typing import Any

import pytest

from pisniar.post import caption
from pisniar.post.render import LinkSegment, plain_text
from pisniar.resolve.types import ArtistInfo, GenreInfo, MatchClass, PlatformLink

ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"
SPOTIFY_LINK = PlatformLink(url="https://open.spotify.com/track/x", source="spotify", match=MatchClass.EXACT_ISRC)
CLIP_LINK = PlatformLink(url="https://www.youtube.com/watch?v=clip", source="user_input", match=MatchClass.MANUAL)


def build(warnings: list[str] | None = None, **overrides: Any) -> tuple[str, list[str], Any]:
    collected = [] if warnings is None else warnings
    arguments: dict[str, Any] = {
        "artists": [ArtistInfo(name="Тол")],
        "title": "Сумніви",
        "year": 2013,
        "release_type": "album",
        "release_title": "Синдром Бажання",
        "album_id_spotify": ALBUM_ID,
        "languages": ["українська"],
        "genres": [GenreInfo(name="рок", hashtag=None, source="deezer")],
        "platform_links": {"spotify": SPOTIFY_LINK},
        "include_release_title": True,
        "warnings": collected,
    }
    arguments.update(overrides)
    document = caption.build_document(**arguments)
    return plain_text(document), collected, document


def test_the_first_line_is_the_artists_and_the_title() -> None:
    text, _, _ = build(artists=[ArtistInfo(name="Тол"), ArtistInfo(name="Друга Ріка")])

    assert text.startswith("Тол, Друга Ріка — Сумніви\n«Синдром Бажання»\n")


def test_a_full_album_post_is_four_lines_then_the_links_then_the_channel() -> None:
    text, _, _ = build(
        platform_links={
            "tidal": PlatformLink(url="https://tidal.com/track/1", source="songlink", match=MatchClass.AGGREGATOR),
            "spotify": SPOTIFY_LINK,
            "bandcamp": PlatformLink(url="https://a.bandcamp.com/track/b", source="songlink", match=MatchClass.MANUAL),
        }
    )

    assert text == (
        "Тол — Сумніви\n"
        "«Синдром Бажання»\n"
        "#Тол · 2013\n"
        "#українська · #рок\n"
        "\n"
        "Spotify · Bandcamp\n"
        "\n"
        f"{caption.CHANNEL_LINK_TEXT}"
    )


def test_a_single_is_three_lines_without_a_release_line_or_a_type_word() -> None:
    text, _, _ = build(release_type="single", release_title=None)

    assert text == (f"Тол — Сумніви\n#Тол · 2013\n#українська · #рок\n\nSpotify\n\n{caption.CHANNEL_LINK_TEXT}")


def test_a_named_album_links_to_spotify_when_the_id_is_valid() -> None:
    text, warnings, document = build()

    assert "\n«Синдром Бажання»\n" in text
    links = [segment for segment in document.segments if isinstance(segment, LinkSegment)]
    assert (links[0].text, links[0].href) == (
        "Синдром Бажання",
        f"https://open.spotify.com/album/{ALBUM_ID}",
    )
    assert warnings == []


def test_an_album_id_that_is_not_base62_degrades_to_plain_text_with_a_warning() -> None:
    text, warnings, document = build(album_id_spotify="bad id")

    assert "«Синдром Бажання»" in text
    assert not [
        segment for segment in document.segments if isinstance(segment, LinkSegment) and "album" in segment.href
    ]
    assert warnings == ["album_id_spotify «bad id» не base62, лінк знято"]


def test_the_release_title_is_left_out_when_the_caller_dropped_it() -> None:
    text, _, _ = build(include_release_title=False)

    assert text.startswith("Тол — Сумніви\n#Тол · 2013\n")
    assert "Синдром" not in text


@pytest.mark.parametrize("release_type", ["single", "compilation", "mixtape", None])
def test_only_an_album_or_an_ep_shows_a_release_title(release_type: str | None) -> None:
    text, _, _ = build(release_type=release_type)

    assert text.startswith("Тол — Сумніви\n#Тол · 2013\n")
    assert "Синдром" not in text
    assert "сингл" not in text


def test_an_album_without_a_release_title_has_no_second_line() -> None:
    text, _, _ = build(release_title=None)

    assert text.startswith("Тол — Сумніви\n#Тол · 2013\n")


def test_an_ep_shows_its_release_title_and_album_link() -> None:
    text, _, document = build(release_type="ep")

    assert "\n«Синдром Бажання»\n" in text
    assert "міні-альбом" not in text
    assert any(
        getattr(segment, "href", "") == f"https://open.spotify.com/album/{ALBUM_ID}" for segment in document.segments
    )


def test_the_year_and_the_release_type_are_optional() -> None:
    text, _, _ = build(year=None, release_type=None)

    assert "\n#Тол\n" in text


def test_languages_lead_the_hashtag_line_and_genres_follow() -> None:
    text, _, _ = build(languages=["англійська", "українська"])

    assert "#англійська #українська · #рок" in text


def test_a_name_that_makes_no_hashtag_is_reported_and_skipped() -> None:
    warnings: list[str] = []

    text, collected, _ = build(
        warnings,
        artists=[ArtistInfo(name="2024"), ArtistInfo(name="Тол")],
        languages=["2024"],
        genres=[GenreInfo(name="123", hashtag=None, source="deezer")],
    )

    assert collected is warnings
    assert "хештег для артиста «2024» не згенеровано (лише цифри)" in warnings
    assert "хештег для мови «2024» не згенеровано" in warnings
    assert "хештег для жанру «123» не згенеровано (лише цифри)" in warnings
    assert "#Тол" in text


def test_a_manual_clip_comes_first_and_is_labelled() -> None:
    text, _, _ = build(platform_links={"spotify": SPOTIFY_LINK, "youtube": CLIP_LINK})

    assert "YouTube (кліп) · Spotify" in text


def test_the_platforms_follow_the_display_order_and_an_automatic_youtube_is_fifth() -> None:
    automatic = PlatformLink(url="https://www.youtube.com/watch?v=a", source="songlink", match=MatchClass.AGGREGATOR)
    links = {
        platform: PlatformLink(url=f"https://{platform}.example/x", source="s", match=MatchClass.AGGREGATOR)
        for platform in ("bandcamp", "soundcloud", "deezer", "appleMusic", "youtubeMusic", "spotify")
    }

    text, _, _ = build(platform_links={**links, "youtube": automatic})

    assert "\nSpotify · YouTube Music · Apple Music · Deezer · YouTube · SoundCloud · Bandcamp\n" in text


def test_a_platform_the_caption_does_not_show_is_left_out() -> None:
    hidden = {
        platform: PlatformLink(url=f"https://{platform}.example/x", source="s", match=MatchClass.AGGREGATOR)
        for platform in ("tidal", "pandora", "amazonMusic", "napster")
    }

    text, _, _ = build(platform_links={"spotify": SPOTIFY_LINK, **hidden})

    assert "\nSpotify\n" in text
    assert "TIDAL" not in text
    assert "Pandora" not in text


def test_the_document_ends_with_the_channel_link() -> None:
    text, _, document = build(platform_links={})

    assert text.endswith(caption.CHANNEL_LINK_TEXT)
    last_link = [segment for segment in document.segments if isinstance(segment, LinkSegment)][-1]
    assert last_link.href == caption.CHANNEL_LINK_URL


def test_a_stored_override_wins_over_the_default_hashtag() -> None:
    warnings: list[str] = []

    hashtags = caption.collect_hashtags(
        [("Own", "Артист"), (None, "Другий")], caption.ARTIST_WITHOUT_HASHTAG_WARNING, warnings
    )

    assert hashtags == ["#Own", "#Другий"]
    assert warnings == []


def test_a_name_without_a_hashtag_is_reported_with_the_given_sentence_and_skipped() -> None:
    warnings: list[str] = []

    hashtags = caption.collect_hashtags([(None, "123"), (None, "рок")], caption.GENRE_WITHOUT_HASHTAG_WARNING, warnings)

    assert hashtags == ["#рок"]
    assert warnings == ["хештег для жанру «123» не згенеровано (лише цифри)"]


def test_no_entries_give_no_hashtags() -> None:
    assert caption.collect_hashtags([], caption.LANGUAGE_WITHOUT_HASHTAG_WARNING, []) == []
