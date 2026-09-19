"""`deezer_genre_name()`: id-keyed lookup (never name-keyed — Deezer
localizes names by request IP), unclassified sentinel, and unknown ids."""

from __future__ import annotations

import pytest

from pisniar.resolve.genres import DEEZER_GENRE_NAMES, deezer_genre_name


@pytest.mark.parametrize(
    ("genre_id", "expected_ukrainian"),
    [(152, "рок"), (464, "метал"), (85, "альтернатива"), (116, "хіп-хоп")],
)
def test_deezer_genre_name_maps_known_ids(genre_id: int, expected_ukrainian: str) -> None:
    assert deezer_genre_name(genre_id, "irrelevant") == expected_ukrainian


def test_deezer_genre_name_ignores_localized_english_name_argument() -> None:
    # id 85 is "Alternative" in English, but a Romanian-IP request returns
    # "Alternativă" for the same id — the dictionary must not care either way.
    assert deezer_genre_name(85, "Alternativă") == "альтернатива"


def test_deezer_genre_name_returns_none_for_unclassified_sentinel() -> None:
    assert deezer_genre_name(0, "All") is None


def test_deezer_genre_name_returns_none_for_unknown_id() -> None:
    assert deezer_genre_name(999999, "Some New Genre") is None


def test_every_dictionary_entry_has_a_non_empty_ukrainian_name() -> None:
    for genre_id, (english_name, ukrainian_name) in DEEZER_GENRE_NAMES.items():
        assert english_name.strip()
        assert ukrainian_name.strip()
        assert genre_id > 0
