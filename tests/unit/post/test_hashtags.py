"""`default_hashtag()`: the post-format examples verbatim, plus the rule that digits-only or empty
input emits nothing."""

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
def test_default_hashtag_matches_the_post_format_examples(name: str, expected: str) -> None:
    assert default_hashtag(name) == expected


@pytest.mark.parametrize("name", ["1995", "  ", "", "%%%", "42"])
def test_default_hashtag_returns_none_for_digits_only_or_empty(name: str) -> None:
    assert default_hashtag(name) is None


def test_default_hashtag_preserves_first_word_casing_exactly() -> None:
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
def test_multi_word_genres_use_the_same_gluing_rule_as_artists(genre_name: str, expected: str) -> None:
    assert default_hashtag(genre_name) == expected
