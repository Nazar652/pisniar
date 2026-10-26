"""The description in the caption: between the hashtags and the links, never cut, and invisible when empty."""

from __future__ import annotations

from typing import Any

from pisniar.db.models import MAX_DESCRIPTION_LENGTH
from pisniar.post.caption import MEDIA_CAPTION_LIMIT, build_caption, build_caption_with_report
from pisniar.post.render import LinkSegment, plain_text, render_html, visible_length
from pisniar.resolve.types import ArtistInfo, GenreInfo, MatchClass, PlatformLink

SPOTIFY_LINK = PlatformLink(url="https://open.spotify.com/track/x", source="spotify", match=MatchClass.EXACT_ISRC)
YOUTUBE_MUSIC_LINK = PlatformLink(
    url="https://music.youtube.com/watch?v=x", source="ytmusic", match=MatchClass.CONFIRMED
)
ALL_LINKS = {
    "spotify": SPOTIFY_LINK,
    "youtubeMusic": YOUTUBE_MUSIC_LINK,
    "appleMusic": PlatformLink(url="https://music.apple.com/ua/song/x/1", source="itunes", match=MatchClass.CONFIRMED),
    "deezer": PlatformLink(url="https://www.deezer.com/track/1", source="deezer", match=MatchClass.EXACT_ISRC),
    "youtube": PlatformLink(url="https://www.youtube.com/watch?v=clip", source="user_input", match=MatchClass.MANUAL),
    "soundcloud": PlatformLink(url="https://soundcloud.com/a/b", source="songlink", match=MatchClass.AGGREGATOR),
    "bandcamp": PlatformLink(url="https://a.bandcamp.com/track/b", source="songlink", match=MatchClass.AGGREGATOR),
}


def base_fields() -> dict[str, Any]:
    return {
        "artists": [ArtistInfo(name="Krovna")],
        "title": "Berserker",
        "year": 2026,
        "release_type": "single",
        "release_title": None,
        "album_id_spotify": None,
        "languages": ["українська"],
        "genres": [GenreInfo(name="рок", hashtag=None, source="deezer")],
        "platform_links": {"spotify": SPOTIFY_LINK, "youtubeMusic": YOUTUBE_MUSIC_LINK},
    }


def test_without_a_description_the_caption_is_byte_for_byte_the_old_one() -> None:
    plain, _ = build_caption(**base_fields())
    none_given, _ = build_caption(**base_fields(), description=None)
    empty_given, _ = build_caption(**base_fields(), description="  \n ")

    assert render_html(none_given) == render_html(plain) == render_html(empty_given)
    assert plain_text(plain) == (
        "Krovna — Berserker\n#Krovna · 2026\n#українська · #рок\n\nSpotify · YouTube Music\n\nПіснярня 🇺🇦"
    )


def test_the_description_sits_after_the_hashtags_and_before_the_links_with_blank_lines_around() -> None:
    document, _ = build_caption(**base_fields(), description="Перший абзац\nдруга лінія\n\nДругий абзац")

    assert plain_text(document) == (
        "Krovna — Berserker\n#Krovna · 2026\n#українська · #рок\n\n"
        "Перший абзац\nдруга лінія\n\nДругий абзац\n\n"
        "Spotify · YouTube Music\n\nПіснярня 🇺🇦"
    )


def test_a_description_without_platform_links_still_precedes_the_channel_line() -> None:
    fields = base_fields()
    fields["platform_links"] = {}

    document, _ = build_caption(**fields, description="Текст")

    assert plain_text(document).endswith("#українська · #рок\n\nТекст\n\nПіснярня 🇺🇦")


def test_only_the_links_of_a_description_become_anchors_and_the_rest_is_escaped() -> None:
    document, _ = build_caption(
        **base_fields(),
        description="<b>жирний</b> & [Кліп](https://youtu.be/x?a=1&b=2) **x**",
    )

    html = render_html(document)
    assert "&lt;b&gt;жирний&lt;/b&gt; &amp; " in html
    assert '<a href="https://youtu.be/x?a=1&amp;b=2">Кліп</a>' in html
    assert "**x**" in html
    assert "<b>" not in html


def test_a_link_costs_its_text_and_a_joiner_emoji_costs_its_utf16_units() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    plain, _ = build_caption(**base_fields())
    document, _ = build_caption(**base_fields(), description=f"{family} [ab](https://example.com/{'x' * 300})")

    assert family in plain_text(document)
    extra = visible_length(document) - visible_length(plain)
    # two line breaks, the emoji (8 units with two joiners), a space and the 2-character label
    assert extra == 2 + 8 + 1 + 2 + 0


def test_bidi_controls_in_a_stored_description_never_reach_the_caption() -> None:
    document, _ = build_caption(**base_fields(), description="a‮b [c⁦d](https://example.com)")

    assert "‮" not in plain_text(document)
    assert "⁦" not in plain_text(document)
    assert [segment.text for segment in document.segments if isinstance(segment, LinkSegment)][0:1] == ["cd"]


def test_a_broken_link_in_the_stored_text_is_shown_as_the_text_it_was() -> None:
    document, _ = build_caption(**base_fields(), description="[Клік](javascript:alert(1))")

    assert "javascript" not in "".join(
        segment.href for segment in document.segments if isinstance(segment, LinkSegment)
    )
    assert "[Клік](javascript:alert(1))" in plain_text(document)


def test_a_link_whose_host_does_not_encode_is_literal_text_in_the_caption() -> None:
    document, _ = build_caption(**base_fields(), description="[Банк](https://банк..укр/x)")

    assert "[Банк](https://банк..укр/x)" in plain_text(document)
    assert "банк" not in "".join(s.href for s in document.segments if isinstance(s, LinkSegment))


def realistic_worst_fields() -> dict[str, Any]:
    """three 30-letter artists, a 100-letter title, a 60-letter album, two languages, six 15-letter genres and all
    seven platforms: the base caption alone is 595 characters."""
    return {
        "artists": [ArtistInfo(name="А" * 30) for _ in range(3)],
        "title": "Т" * 100,
        "year": 2026,
        "release_type": "album",
        "release_title": "Р" * 60,
        "album_id_spotify": None,
        "languages": ["українська", "англійська"],
        "genres": [GenreInfo(name="Ж" * 15, hashtag=None, source="deezer") for _ in range(6)],
        "platform_links": ALL_LINKS,
    }


def test_a_full_description_on_a_realistic_worst_case_fits_by_degrading_the_rest_never_the_text() -> None:
    description = "в" * MAX_DESCRIPTION_LENGTH

    without = build_caption_with_report(**realistic_worst_fields(), limit=10_000)
    result = build_caption_with_report(**realistic_worst_fields(), description=description)

    assert visible_length(without.document) == 595
    assert not result.degradation.over_limit
    assert visible_length(result.document) <= MEDIA_CAPTION_LIMIT
    assert description in plain_text(result.document)
    assert result.degradation.dropped_genres


def test_the_description_is_kept_whole_even_when_the_caption_stays_over_the_limit() -> None:
    fields = realistic_worst_fields()
    fields["title"] = "Т" * 400
    description = "в" * MAX_DESCRIPTION_LENGTH

    result = build_caption_with_report(**fields, description=description)

    assert result.degradation.over_limit
    assert description in plain_text(result.document)
    assert result.degradation.release_title_dropped
