"""`default_hashtag()`: the D-29 examples from `post-format-draft.md`,
verbatim, plus the digits-only/empty no-emit rule (S2-A10/S2-A11)."""

from __future__ import annotations

import pytest

from pisniar.post.hashtags import default_hashtag


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Sick Solution", "SickSolution"),
        ("hackedface", "hackedface"),
        ("ПРОЗАК", "ПРОЗАК"),
        ("Димна суміш", "ДимнаСуміш"),
        ("0%Mercury", "0Mercury"),
        ("Burned Time Machine", "BurnedTimeMachine"),
        ("Krovna", "Krovna"),
        ("Тол", "Тол"),
    ],
)
def test_default_hashtag_matches_d29_examples(name: str, expected: str) -> None:
    assert default_hashtag(name) == expected


@pytest.mark.parametrize("name", ["1995", "  ", "", "%%%", "42"])
def test_default_hashtag_returns_none_for_digits_only_or_empty(name: str) -> None:
    assert default_hashtag(name) is None


def test_default_hashtag_preserves_first_word_casing_exactly() -> None:
    # First word untouched even when lowercase and later words exist —
    # only words after the first get capitalized.
    assert default_hashtag("van Basten Duo") == "vanBastenDuo"


@pytest.mark.parametrize(
    ("genre_name", "expected"),
    [
        ("соул і фанк", "соулІФанк"),
        ("африканська музика", "африканськаМузика"),
        ("латиноамериканська музика", "латиноамериканськаМузика"),
        ("хіп-хоп", "хіпхоп"),
        ("рок", "рок"),
    ],
)
def test_multi_word_genres_use_the_same_gluing_rule_as_artists(
    genre_name: str, expected: str
) -> None:
    """F-27: genres go through `default_hashtag()` exactly like artists, so
    a two-word genre becomes one readable capitalized hashtag (D-29)."""
    assert default_hashtag(genre_name) == expected
