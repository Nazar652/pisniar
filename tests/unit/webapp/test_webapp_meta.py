"""`/api/meta`: the platform table is the server's own validation table, not a copy."""

from __future__ import annotations

import pytest

import pisniar.webapp.meta as meta_module
from pisniar.db.models import MANUAL_FIELD_NAMES
from pisniar.resolve.genres import DEEZER_GENRE_NAMES
from pisniar.resolve.link_policy import ALLOWED_HOST_SUFFIXES, ALLOWED_HOSTS, CLASSIFIED_PLATFORMS
from pisniar.webapp.meta import LANGUAGE_HINTS, build_meta


def test_every_platform_and_host_of_the_link_policy_is_offered() -> None:
    meta = build_meta()

    assert {rule.key for rule in meta.platforms} == set(ALLOWED_HOSTS)
    for rule in meta.platforms:
        assert set(rule.hosts) == set(ALLOWED_HOSTS[rule.key])
        assert rule.hosts == sorted(rule.hosts)
        assert rule.host_suffixes == list(ALLOWED_HOST_SUFFIXES.get(rule.key, ()))
        assert rule.requires_track_link == (rule.key in CLASSIFIED_PLATFORMS)
        assert rule.display_name


def test_a_host_added_to_the_policy_shows_up_without_touching_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extended = {**ALLOWED_HOSTS, "tidal": ALLOWED_HOSTS["tidal"] | {"new.tidal.example"}}
    monkeypatch.setattr(meta_module, "ALLOWED_HOSTS", extended)

    rules = {rule.key: rule for rule in build_meta().platforms}

    assert "new.tidal.example" in rules["tidal"].hosts


def test_the_release_types_are_the_ones_the_caption_knows() -> None:
    options = {option.value: option.label for option in build_meta().release_types}

    assert options == {
        "single": "сингл",
        "album": "альбом",
        "ep": "міні-альбом",
        "compilation": "збірка",
    }


def test_genre_hints_are_the_deezer_dictionary_names_with_their_hashtags() -> None:
    hints = build_meta().genres

    assert {hint.name for hint in hints} == {
        ukrainian for _, ukrainian in DEEZER_GENRE_NAMES.values()
    }
    by_name = {hint.name: hint.default_hashtag for hint in hints}
    assert by_name["рок"] == "рок"
    assert by_name["хіп-хоп"] == "хіпхоп"
    assert [hint.name for hint in hints] == sorted(hint.name for hint in hints)


def test_language_hints_start_with_the_default_language() -> None:
    assert build_meta().languages[0] == "українська"
    assert build_meta().languages == list(LANGUAGE_HINTS)


def test_the_manual_field_names_and_the_caption_limit() -> None:
    meta = build_meta()

    assert meta.manual_field_names == list(MANUAL_FIELD_NAMES)
    assert meta.caption_limit == 1024
