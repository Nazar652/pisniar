"""Validating an edit and planning what it changes: every rule of the field list, the
semantics of the manual-field list, and the removal of links.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from pisniar.webapp import fields
from pisniar.webapp.errors import FieldsInvalidError
from pisniar.webapp.fields import (
    PlannedFields,
    StoredFields,
    plan_album_id,
    plan_artists,
    plan_fields,
    plan_genres,
    plan_hashtag,
    plan_languages,
    plan_manual_fields,
    plan_platform_links,
    plan_release_title,
    plan_release_type,
    plan_title,
    plan_year,
    valid_hashtag,
)
from pisniar.webapp.schemas import ArtistInput, FieldError, FieldsInput, GenreInput

ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"
TRACK_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
SPOTIFY_LINK = {"url": TRACK_URL, "source": "spotify", "match": "exact_isrc"}
TIDAL_LINK = {
    "url": "https://listen.tidal.com/track/1",
    "source": "songlink",
    "match": "aggregator",
}
YOUTUBE_MUSIC_URL = "https://music.youtube.com/watch?v=n4bPzdIs_nc"
YOUTUBE_MUSIC_LINK = {"url": YOUTUBE_MUSIC_URL, "source": "ytmusic", "match": "confirmed"}


def make_stored(**overrides: Any) -> StoredFields:
    values: dict[str, Any] = {
        "title": "Тема",
        "artists": [
            {"name": "Океан Ельзи", "hashtag": None, "spotify_id": "a1"},
            {"name": "Друга Ріка", "hashtag": "DrugaRika", "spotify_id": None},
        ],
        "languages": ["українська"],
        "genres": [{"name": "рок", "hashtag": None, "source": "deezer"}],
        "year": 2019,
        "release_type": "album",
        "release_title": "Альбом",
        "album_id_spotify": ALBUM_ID,
        "platform_links": {
            "spotify": SPOTIFY_LINK,
            "tidal": TIDAL_LINK,
            "youtubeMusic": YOUTUBE_MUSIC_LINK,
        },
        "manual_fields": [],
        "removed_platforms": [],
    }
    values.update(overrides)
    return StoredFields(**values)


def make_unchanged_update(stored: StoredFields | None = None, **overrides: Any) -> FieldsInput:
    """the form exactly as the editor would send it back without a single edit."""
    stored = stored or make_stored()
    artists = cast("list[dict[str, Any]]", stored.artists)
    genres = cast("list[dict[str, Any]]", stored.genres)
    links = cast("dict[str, dict[str, Any]]", stored.platform_links)
    values: dict[str, Any] = {
        "title": stored.title,
        "artists": [ArtistInput(name=e["name"], hashtag=e["hashtag"]) for e in artists],
        "languages": list(stored.languages),
        "genres": [GenreInput(name=e["name"], hashtag=e["hashtag"]) for e in genres],
        "year": stored.year,
        "release_type": stored.release_type,
        "release_title": stored.release_title,
        "album_link": f"https://open.spotify.com/album/{stored.album_id_spotify}",
        "platform_links": {key: entry["url"] for key, entry in links.items()},
    }
    values.update(overrides)
    return FieldsInput(**values)


def plan(
    update: FieldsInput,
    stored: StoredFields | None = None,
    year: int = 2026,
) -> PlannedFields:
    return plan_fields(update, stored or make_stored(), year)


def problems(update: FieldsInput, stored: StoredFields | None = None) -> dict[str, str]:
    with pytest.raises(FieldsInvalidError) as raised:
        plan(update, stored)
    return {error.field: error.message for error in raised.value.errors}


def test_saving_the_form_unchanged_marks_and_changes_nothing() -> None:
    planned = plan(make_unchanged_update())

    assert planned.changed_fields == ()
    assert planned.manual_fields == []
    assert planned.removed_platforms == []
    assert planned.youtube_music_changed is False
    assert planned.platform_links == make_stored().platform_links
    assert planned.artists == make_stored().artists


@pytest.mark.parametrize(
    ("override", "expected_name"),
    [
        ({"title": "Нова тема"}, "title"),
        ({"year": 2020}, "year"),
        ({"release_type": "single"}, "release_type"),
        ({"release_title": "Інший альбом"}, "release_title"),
        (
            {"album_link": "https://open.spotify.com/album/0123456789abcdefghijkl"},
            "album_id_spotify",
        ),
        (
            {
                "artists": [
                    ArtistInput(name="Інший", hashtag=None),
                    ArtistInput(name="Друга Ріка", hashtag="DrugaRika"),
                ]
            },
            "artists",
        ),
    ],
)
def test_a_changed_field_joins_the_manual_list_and_nothing_else_does(
    override: dict[str, Any], expected_name: str
) -> None:
    planned = plan(make_unchanged_update(**override))

    assert planned.manual_fields == [expected_name]
    assert planned.changed_fields == (expected_name,)


def test_a_manually_emptied_field_is_listed_with_a_null_value() -> None:
    planned = plan(make_unchanged_update(year=None, release_title=None, album_link=None))

    assert planned.year is None
    assert planned.release_title is None
    assert planned.album_id_spotify is None
    assert planned.manual_fields == ["year", "release_title", "album_id_spotify"]


def test_an_empty_string_is_the_same_as_no_value() -> None:
    planned = plan(make_unchanged_update(release_type="  ", release_title="", album_link=" "))

    assert (planned.release_type, planned.release_title, planned.album_id_spotify) == (
        None,
        None,
        None,
    )


def test_a_field_already_in_the_list_stays_there_while_unchanged() -> None:
    stored = make_stored(manual_fields=["year"])

    planned = plan(make_unchanged_update(stored), stored)

    assert planned.manual_fields == ["year"]
    assert planned.changed_fields == ()


def test_the_manual_list_keeps_the_canonical_order() -> None:
    stored = make_stored(manual_fields=["release_title"])

    planned = plan(make_unchanged_update(stored, title="X", year=2000), stored)

    assert planned.manual_fields == ["title", "year", "release_title"]


def test_back_to_automatic_takes_the_name_off_the_list_and_keeps_the_value() -> None:
    stored = make_stored(manual_fields=["year", "title"])

    planned = plan(make_unchanged_update(stored, revert_to_automatic=["year"]), stored)

    assert planned.manual_fields == ["title"]
    assert planned.year == 2019


def test_a_field_cannot_be_changed_and_reverted_in_one_save() -> None:
    stored = make_stored(manual_fields=["year"])

    found = problems(make_unchanged_update(stored, year=2000, revert_to_automatic=["year"]), stored)

    assert "revert_to_automatic" in found


def test_an_unknown_name_cannot_be_reverted() -> None:
    assert "revert_to_automatic" in problems(make_unchanged_update(revert_to_automatic=["cover_url"]))


def test_a_hashtag_alone_does_not_freeze_the_artist_names() -> None:
    update = make_unchanged_update(
        artists=[
            ArtistInput(name="Океан Ельзи", hashtag="OE"),
            ArtistInput(name="Друга Ріка", hashtag="DrugaRika"),
        ]
    )

    planned = plan(update)

    assert planned.manual_fields == []
    assert planned.artists[0]["hashtag"] == "OE"
    assert planned.artists[0]["spotify_id"] == "a1"


def test_a_new_artist_has_no_spotify_id_and_a_known_one_keeps_it() -> None:
    update = make_unchanged_update(artists=[ArtistInput(name="Океан Ельзи"), ArtistInput(name="Хтось Новий")])

    planned = plan(update)

    assert [(entry["name"], entry["spotify_id"]) for entry in planned.artists] == [
        ("Океан Ельзи", "a1"),
        ("Хтось Новий", None),
    ]


def test_an_artist_name_is_trimmed_and_an_empty_one_is_refused() -> None:
    errors: list[FieldError] = []

    planned, _ = plan_artists([("  Тол  ", None), ("   ", None)], [], errors)

    assert [entry["name"] for entry in planned] == ["Тол"]
    assert [error.field for error in errors] == ["artists[1].name"]


def test_at_least_one_artist_is_required() -> None:
    errors: list[FieldError] = []

    plan_artists([], [], errors)

    assert [error.field for error in errors] == ["artists"]


@pytest.mark.parametrize("value", ["abc", "Тема_1", "a" * 64, "ДругаРіка", "x1"])
def test_a_word_hashtag_is_valid(value: str) -> None:
    assert valid_hashtag(value) is True


@pytest.mark.parametrize("value", ["", "#tag", "two words", "a" * 65, "12345", "tag!", "a-b"])
def test_a_hashtag_with_other_characters_or_only_digits_is_not(value: str) -> None:
    assert valid_hashtag(value) is False


def test_an_empty_hashtag_means_unset_and_a_bad_one_is_reported() -> None:
    errors: list[FieldError] = []

    assert plan_hashtag(None, "f", errors) is None
    assert plan_hashtag("   ", "f", errors) is None
    assert plan_hashtag(" ok ", "f", errors) == "ok"
    assert errors == []
    assert plan_hashtag("#bad", "artists[0].hashtag", errors) is None
    assert [error.field for error in errors] == ["artists[0].hashtag"]


def test_the_title_is_trimmed_and_may_not_be_empty() -> None:
    errors: list[FieldError] = []

    assert plan_title("  Тема ", errors) == "Тема"
    assert errors == []
    plan_title("   ", errors)
    assert [error.field for error in errors] == ["title"]


def test_languages_keep_their_order_and_refuse_duplicates_and_blanks() -> None:
    errors: list[FieldError] = []

    languages = plan_languages(["англійська", "українська", "Українська", " "], errors)

    assert languages == ["англійська", "українська"]
    assert [error.field for error in errors] == ["languages[2]", "languages[3]"]


def test_at_least_one_language_is_required() -> None:
    errors: list[FieldError] = []

    plan_languages([], errors)

    assert [error.field for error in errors] == ["languages"]


def test_genres_may_be_empty_a_known_genre_keeps_its_source_and_a_new_one_is_manual() -> None:
    errors: list[FieldError] = []
    stored = [{"name": "рок", "hashtag": None, "source": "deezer"}]

    genres = plan_genres([("рок", None), ("новий жанр", "NewGenre")], stored, errors)

    assert genres == [
        {"name": "рок", "hashtag": None, "source": "deezer"},
        {"name": "новий жанр", "hashtag": "NewGenre", "source": "manual"},
    ]
    assert errors == []
    assert plan_genres([], stored, errors) == []


def test_a_genre_needs_a_name_a_valid_hashtag_and_no_duplicate() -> None:
    errors: list[FieldError] = []

    plan_genres([(" ", None), ("рок", "#x"), ("Рок", None)], [], errors)

    assert [error.field for error in errors] == [
        "genres[0].name",
        "genres[1].hashtag",
        "genres[2].name",
    ]


@pytest.mark.parametrize("value", [None, 1900, 2026, 2027])
def test_a_year_in_range_or_none_is_accepted(value: int | None) -> None:
    errors: list[FieldError] = []

    assert plan_year(value, 2026, errors) == value
    assert errors == []


@pytest.mark.parametrize("value", [1899, 2028, 0, -5, 99999])
def test_a_year_out_of_range_is_refused(value: int) -> None:
    errors: list[FieldError] = []

    plan_year(value, 2026, errors)

    assert [error.field for error in errors] == ["year"]


@pytest.mark.parametrize("value", ["single", "album", "ep", "compilation", None, "", "  "])
def test_the_three_release_types_and_empty_are_accepted(value: str | None) -> None:
    errors: list[FieldError] = []

    plan_release_type(value, None, errors)

    assert errors == []


def test_a_release_type_from_a_source_is_accepted_only_unchanged() -> None:
    errors: list[FieldError] = []

    assert plan_release_type("mixtape", "mixtape", errors) == "mixtape"
    assert errors == []
    plan_release_type("mixtape", "single", errors)
    plan_release_type("mixtape", None, errors)
    assert [error.field for error in errors] == ["release_type", "release_type"]


def test_a_release_title_is_trimmed_and_empty_means_none() -> None:
    assert plan_release_title("  Альбом ") == "Альбом"
    assert plan_release_title("") is None
    assert plan_release_title(None) is None


@pytest.mark.parametrize(
    "link",
    [
        f"https://open.spotify.com/album/{ALBUM_ID}",
        f"https://open.spotify.com/intl-uk/album/{ALBUM_ID}?si=x",
    ],
)
def test_an_album_link_gives_the_id_that_is_stored(link: str) -> None:
    errors: list[FieldError] = []

    assert plan_album_id(link, errors) == ALBUM_ID
    assert errors == []


@pytest.mark.parametrize(
    "link",
    [TRACK_URL, "https://evil.example/album/" + ALBUM_ID, "http://open.spotify.com/album/x", "x"],
)
def test_anything_but_an_album_link_is_refused(link: str) -> None:
    errors: list[FieldError] = []

    assert plan_album_id(link, errors) is None
    assert [error.field for error in errors] == ["album_link"]


def test_an_unchanged_link_stays_exactly_as_stored_whatever_its_source() -> None:
    stored_links = {"tidal": TIDAL_LINK}
    errors: list[FieldError] = []

    links, removed, music_changed = plan_platform_links({"tidal": TIDAL_LINK["url"]}, stored_links, [], errors)

    assert links == {"tidal": TIDAL_LINK}
    assert links["tidal"] is TIDAL_LINK
    assert (removed, music_changed, errors) == ([], False, [])


def test_a_new_or_edited_link_becomes_a_normalised_manual_link() -> None:
    errors: list[FieldError] = []

    links, _, _ = plan_platform_links(
        {"youtube": "https://youtu.be/n4bPzdIs_nc", "tidal": "https://listen.tidal.com/track/2"},
        {"tidal": TIDAL_LINK},
        [],
        errors,
    )

    assert links["youtube"] == {
        "url": "https://www.youtube.com/watch?v=n4bPzdIs_nc",
        "source": "manual",
        "match": "manual",
    }
    tidal = cast("dict[str, Any]", links["tidal"])
    assert tidal["url"] == "https://listen.tidal.com/track/2"
    assert tidal["source"] == "manual"
    assert errors == []


def test_a_stored_link_missing_from_the_input_is_removed_for_good() -> None:
    errors: list[FieldError] = []

    links, removed, _ = plan_platform_links(
        {"spotify": SPOTIFY_LINK["url"]}, {"spotify": SPOTIFY_LINK, "tidal": TIDAL_LINK}, [], errors
    )

    assert set(links) == {"spotify"}
    assert removed == ["tidal"]


def test_adding_a_removed_platform_by_hand_lifts_its_removal() -> None:
    errors: list[FieldError] = []

    links, removed, _ = plan_platform_links(
        {"tidal": "https://listen.tidal.com/track/9"}, {}, ["tidal", "deezer"], errors
    )

    assert "tidal" in links
    assert removed == ["deezer"]


def test_a_removal_survives_a_save_that_does_not_touch_that_platform() -> None:
    errors: list[FieldError] = []

    _, removed, _ = plan_platform_links({}, {}, ["tidal"], errors)

    assert removed == ["tidal"]


def test_a_link_off_the_platforms_hosts_or_for_an_unknown_platform_is_refused() -> None:
    errors: list[FieldError] = []

    links, _, _ = plan_platform_links(
        {
            "tidal": "https://evil.example/track/1",
            "newservice": "https://newservice.example/x",
            "spotify": "http://open.spotify.com/track/abc",
        },
        {},
        [],
        errors,
    )

    assert links == {}
    assert [error.field for error in errors] == [
        "platform_links.tidal",
        "platform_links.newservice",
        "platform_links.spotify",
    ]


def test_an_album_link_is_refused_in_a_track_slot() -> None:
    errors: list[FieldError] = []

    plan_platform_links({"spotify": f"https://open.spotify.com/album/{ALBUM_ID}"}, {}, [], errors)

    assert [error.field for error in errors] == ["platform_links.spotify"]


def test_a_changed_youtube_music_link_is_flagged_and_an_unchanged_one_is_not() -> None:
    errors: list[FieldError] = []
    stored = {"youtubeMusic": YOUTUBE_MUSIC_LINK}

    _, _, unchanged = plan_platform_links({"youtubeMusic": YOUTUBE_MUSIC_URL}, stored, [], errors)
    _, _, edited = plan_platform_links(
        {"youtubeMusic": "https://music.youtube.com/watch?v=dQw4w9WgXcQ"}, stored, [], errors
    )
    _, _, removed = plan_platform_links({}, stored, [], errors)
    _, _, added = plan_platform_links({"youtubeMusic": YOUTUBE_MUSIC_URL}, {}, [], errors)

    assert (unchanged, edited, removed, added) == (False, True, True, True)


def test_the_manual_list_helper_reports_unknown_and_clashing_names() -> None:
    errors: list[FieldError] = []

    result = plan_manual_fields(["year", 7], ["title"], ["year", "bogus"], errors)

    assert result == ["title"]
    assert [error.field for error in errors] == ["revert_to_automatic"]


def test_every_problem_is_reported_at_once_and_nothing_is_planned() -> None:
    update = make_unchanged_update(title=" ", year=1500, release_type="mixtape", languages=[], artists=[])

    found = problems(update)

    assert {"title", "year", "release_type", "languages", "artists"} <= set(found)


def test_the_plan_carries_the_genres_languages_and_links_as_the_rows_expect_them() -> None:
    update = make_unchanged_update(
        languages=["англійська"],
        genres=[GenreInput(name="поп")],
        platform_links={"spotify": SPOTIFY_LINK["url"]},
    )

    planned = plan(update)

    assert planned.languages == ["англійська"]
    assert planned.genres == [{"name": "поп", "hashtag": None, "source": "manual"}]
    assert set(planned.platform_links) == {"spotify"}
    assert planned.removed_platforms == ["tidal", "youtubeMusic"]
    assert set(planned.changed_fields) == {"languages", "genres", "platform_links"}


def test_the_constants_name_the_three_release_types() -> None:
    assert fields.RELEASE_TYPES == ("single", "album", "ep", "compilation")
