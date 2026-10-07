"""Stored rows to caption input. The golden test pins that a caption rebuilt from the
rows is byte for byte the caption built from the resolve's own objects.
"""

from __future__ import annotations

from pisniar.db.models import DEFAULT_LANGUAGES
from pisniar.post.caption import build_caption
from pisniar.post.caption_input import (
    build_caption_from_rows,
    genres_from_stored,
    languages_from_stored,
    platform_links_from_stored,
)
from pisniar.post.render import render_html
from pisniar.resolve.types import ArtistInfo, GenreInfo, MatchClass, PlatformLink

ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"


def test_genres_from_stored_reads_the_jsonb_column_back() -> None:
    genres = genres_from_stored([{"name": "рок", "hashtag": None, "source": "deezer"}])

    assert [(genre.name, genre.hashtag, genre.source) for genre in genres] == [
        ("рок", None, "deezer")
    ]


def test_genres_from_stored_skips_entries_without_a_usable_name() -> None:
    assert genres_from_stored(["рок", {"hashtag": "x"}, {"name": ""}]) == []


def test_a_genre_without_a_source_is_marked_as_stored() -> None:
    assert genres_from_stored([{"name": "рок"}])[0].source == "stored"


def test_languages_from_stored_reads_the_jsonb_column_back() -> None:
    assert languages_from_stored(["українська", "англійська"]) == ["українська", "англійська"]


def test_languages_from_stored_falls_back_to_the_schema_default() -> None:
    """the language is shown on every post, so an empty or unreadable column must
    not silently drop the row from the caption."""
    assert languages_from_stored([]) == DEFAULT_LANGUAGES
    assert languages_from_stored([{"name": "українська"}, ""]) == DEFAULT_LANGUAGES


def test_platform_links_from_stored_skips_an_unreadable_entry() -> None:
    links = platform_links_from_stored(
        {
            "tidal": {
                "url": "https://listen.tidal.com/track/1",
                "source": "songlink",
                "match": "aggregator",
            },
            "broken": "not a dict",
            "nourl": {"source": "x"},
        }
    )

    assert list(links) == ["tidal"]
    assert links["tidal"].match is MatchClass.AGGREGATOR


STORED_ARTISTS: list[object] = [
    {"name": "Океан Ельзи", "hashtag": None, "spotify_id": "a1"},
    {"name": "Друга Ріка", "hashtag": "DrugaRika", "spotify_id": None},
]
STORED_LANGUAGES: list[object] = ["англійська", "українська", "польська"]
STORED_GENRES: list[object] = [
    {"name": "Рок", "hashtag": None, "source": "deezer"},
    {"name": "Поп", "hashtag": "popmusic", "source": "deezer"},
]
STORED_LINKS: dict[str, object] = {
    "youtube": {
        "url": "https://www.youtube.com/watch?v=n4bPzdIs_nc",
        "source": "user_input",
        "match": "manual",
    },
    "spotify": {
        "url": "https://open.spotify.com/track/x",
        "source": "spotify",
        "match": "exact_isrc",
    },
    "tidal": {
        "url": "https://listen.tidal.com/track/1",
        "source": "songlink",
        "match": "aggregator",
    },
    "appleMusic": {
        "url": "https://music.apple.com/ua/song/x/1",
        "source": "itunes",
        "match": "confirmed",
    },
}
DOMAIN_LINKS = {
    "youtube": PlatformLink(
        url="https://www.youtube.com/watch?v=n4bPzdIs_nc",
        source="user_input",
        match=MatchClass.MANUAL,
    ),
    "spotify": PlatformLink(
        url="https://open.spotify.com/track/x", source="spotify", match=MatchClass.EXACT_ISRC
    ),
    "tidal": PlatformLink(
        url="https://listen.tidal.com/track/1", source="songlink", match=MatchClass.AGGREGATOR
    ),
    "appleMusic": PlatformLink(
        url="https://music.apple.com/ua/song/x/1", source="itunes", match=MatchClass.CONFIRMED
    ),
}


def test_a_caption_rebuilt_from_rows_is_byte_for_byte_the_resolve_caption() -> None:
    expected_document, expected_warnings = build_caption(
        artists=[
            ArtistInfo(name="Океан Ельзи", hashtag=None, spotify_id="a1"),
            ArtistInfo(name="Друга Ріка", hashtag="DrugaRika", spotify_id=None),
        ],
        title="Тема",
        year=2019,
        release_type="album",
        release_title="Альбом",
        album_id_spotify=ALBUM_ID,
        languages=["англійська", "українська", "польська"],
        genres=[
            GenreInfo(name="Рок", hashtag=None, source="deezer"),
            GenreInfo(name="Поп", hashtag="popmusic", source="deezer"),
        ],
        platform_links=DOMAIN_LINKS,
    )

    rebuilt = build_caption_from_rows(
        title="Тема",
        artists=STORED_ARTISTS,
        year=2019,
        release_type="album",
        release_title="Альбом",
        album_id_spotify=ALBUM_ID,
        languages=STORED_LANGUAGES,
        genres=STORED_GENRES,
        platform_links=STORED_LINKS,
    )

    assert render_html(rebuilt.document) == render_html(expected_document)
    assert rebuilt.warnings == expected_warnings


def test_the_report_names_what_did_not_fit() -> None:
    genres: list[object] = [
        {"name": f"Жанр{index}", "hashtag": None, "source": "deezer"} for index in range(200)
    ]

    degradation = build_caption_from_rows(
        title="Тема",
        artists=[{"name": "Артист", "hashtag": None, "spotify_id": None}],
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        languages=["українська"],
        genres=genres,
        platform_links={},
    ).degradation

    assert degradation.dropped_genres
    assert degradation.dropped_genres[0] == "Жанр199"
    assert not degradation.over_limit
