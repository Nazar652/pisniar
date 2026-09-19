"""`matching.is_match()`: normalization and the calibrated duration
tolerance. Fixture numbers are the real iTunes/ytmusicapi values recorded
live during 2.6 calibration (see `matching.py` module docstring)."""

from __future__ import annotations

import pytest

from pisniar.resolve.matching import (
    DURATION_TOLERANCE_MS,
    is_match,
    is_valid_isrc,
    normalize_text,
)


def test_duration_tolerance_is_three_seconds() -> None:
    assert DURATION_TOLERANCE_MS == 3000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("But Wait... (Official Video)", "but wait"),
        ("Berserker - Topic", "berserker"),
        ("Покаяння", "покаяння"),
        ("Покаяння (Official Audio)", "покаяння"),
        ("Song feat. Someone Else", "song"),
        ("Song ft. Someone", "song"),
    ],
)
def test_normalize_text_strips_noise(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalize_text_strips_diacritics() -> None:
    assert normalize_text("Alternativă") == normalize_text("Alternativa")


@pytest.mark.parametrize(
    ("reference_artist", "candidate_artist"),
    [
        ("Burned Time Machine, 0%Mercury", "Burned Time Machine & 0%Mercury"),
        ("hackedface", "Hackedface"),
    ],
)
def test_is_match_accepts_real_calibration_candidates(
    reference_artist: str, candidate_artist: str
) -> None:
    # iTunes/ytmusicapi live results for "Burned Time Machine — Покаяння":
    # 224125ms and 225000ms for the same track (delta well under tolerance).
    assert is_match(
        reference_artist=reference_artist,
        reference_title="Покаяння",
        reference_duration_ms=224125,
        candidate_artist=candidate_artist,
        candidate_title="Покаяння",
        candidate_duration_ms=225000,
    )


def test_is_match_rejects_same_word_different_song() -> None:
    # ytmusicapi live result for "Krovna — Berserker": a different artist's
    # track whose title merely contains the word "Berserker".
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=263000,
        candidate_artist="Runes of the North",
        candidate_title="Bjarkamál (The Berserker's Call)",
        candidate_duration_ms=210000,
    )


def test_is_match_rejects_duration_outside_tolerance() -> None:
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=263000,
        candidate_artist="Krovna",
        candidate_title="Berserker",
        candidate_duration_ms=263000 + DURATION_TOLERANCE_MS + 1,
    )


def test_is_match_accepts_duration_at_tolerance_boundary() -> None:
    assert is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=263000,
        candidate_artist="Krovna",
        candidate_title="Berserker",
        candidate_duration_ms=263000 + DURATION_TOLERANCE_MS,
    )


def test_is_match_ignores_duration_when_either_side_is_unknown() -> None:
    assert is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=None,
        candidate_artist="Krovna",
        candidate_title="Berserker",
        candidate_duration_ms=999999,
    )


def test_is_match_rejects_title_mismatch() -> None:
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=None,
        candidate_artist="Krovna",
        candidate_title="Some Other Song",
        candidate_duration_ms=None,
    )


@pytest.mark.parametrize(
    "isrc",
    ["GBAYE9200070", "SE6A92327148", "UAA1Y2500001"],
)
def test_is_valid_isrc_accepts_well_formed_identifiers(isrc: str) -> None:
    assert is_valid_isrc(isrc) is True


@pytest.mark.parametrize(
    "isrc",
    [
        None,
        "",
        "GBAYE920007",
        "gbaye9200070",
        "GBAYE92000700",
        "GB/../track/1",
        "GBAYE9200070?x=1",
        "GBAYE920007A",
    ],
)
def test_is_valid_isrc_rejects_anything_that_could_reshape_a_url(isrc: str | None) -> None:
    """F-16: the value is pasted into `/track/isrc:<isrc>`, so a stray `/`
    or `?` from a source would change which endpoint we call."""
    assert is_valid_isrc(isrc) is False
