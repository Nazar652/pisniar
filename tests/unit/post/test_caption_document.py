"""`caption.build_document`: one attempt at the document, with the degradation decisions (`include_release_title`, the
genre and platform lists) already made by the caller, and every warning collected into the list it is given."""

from __future__ import annotations

from typing import Any

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

    assert text.startswith("Тол, Друга Ріка — Сумніви\n\n")


def test_a_named_album_links_to_spotify_when_the_id_is_valid() -> None:
    text, warnings, document = build()

    assert "#Тол · 2013 · альбом «Синдром Бажання»" in text
    links = [segment for segment in document.segments if isinstance(segment, LinkSegment)]
    assert (links[0].text, links[0].href) == (
        "Синдром Бажання",
        f"https://open.spotify.com/album/{ALBUM_ID}",
    )
    assert warnings == []


def test_an_album_id_that_is_not_base62_degrades_to_plain_text_with_a_warning() -> None:
    text, warnings, document = build(album_id_spotify="bad id")

    assert "альбом «Синдром Бажання»" in text
    assert not [
        segment for segment in document.segments if isinstance(segment, LinkSegment) and "album" in segment.href
    ]
    assert warnings == ["album_id_spotify «bad id» не base62, лінк знято"]


def test_the_release_title_is_left_out_when_the_caller_dropped_it() -> None:
    text, _, _ = build(include_release_title=False)

    assert "#Тол · 2013 · альбом" in text
    assert "Синдром" not in text


def test_a_single_never_shows_a_release_title() -> None:
    text, _, _ = build(release_type="single")

    assert "#Тол · 2013 · сингл" in text
    assert "Синдром" not in text


def test_an_unknown_release_type_is_shown_as_the_source_gave_it() -> None:
    text, _, _ = build(release_type="mixtape", release_title=None)

    assert "#Тол · 2013 · mixtape" in text


def test_an_ep_is_a_mini_album_with_its_release_title_and_album_link() -> None:
    text, _, document = build(release_type="ep")

    assert "#Тол · 2013 · міні-альбом «Синдром Бажання»" in text
    assert any(
        getattr(segment, "href", "") == f"https://open.spotify.com/album/{ALBUM_ID}" for segment in document.segments
    )


def test_the_year_and_the_release_type_are_optional() -> None:
    text, _, _ = build(year=None, release_type=None)

    assert "#Тол\n" in text


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
