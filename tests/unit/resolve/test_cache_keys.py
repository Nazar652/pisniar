"""`cache.build_request_key`: one normalization for every adapter (F-17),
an escaped separator, case-sensitive identifiers, and a bounded length so
an over-long key can never reach the btree limit as a non-IntegrityError
(F-22)."""

from __future__ import annotations

from pisniar.resolve.cache import (
    MAX_REQUEST_KEY_BYTES,
    build_request_key,
    normalize_key_part,
)


def test_internal_whitespace_is_collapsed_not_only_trimmed() -> None:
    """`strip().casefold()` left `"A  B"` and `"A B"` as two cache rows."""
    assert build_request_key("deezer:search:", "  Sick   Solution ", "Horn ") == build_request_key(
        "deezer:search:", "Sick Solution", "Horn"
    )


def test_separator_inside_a_value_cannot_forge_another_field() -> None:
    """Without escaping, artist `"A|B"` + title `"C"` and artist `"A"` +
    title `"B|C"` collapse onto the same row."""
    first = build_request_key("deezer:search:", "A|B", "C")
    second = build_request_key("deezer:search:", "A", "B|C")

    assert first != second


def test_backslash_is_escaped_so_the_escaping_itself_is_unambiguous() -> None:
    assert normalize_key_part("a\\|b") != normalize_key_part("a|b")


def test_identifiers_keep_their_case() -> None:
    """Spotify ids are base62 — folding case would merge two tracks."""
    lower = build_request_key("spotify:track:", "aBcD", casefold=False)
    upper = build_request_key("spotify:track:", "AbCd", casefold=False)

    assert lower != upper


def test_free_text_parts_are_case_insensitive() -> None:
    assert build_request_key("itunes:search:", "UA", "Krovna", "Berserker") == build_request_key(
        "itunes:search:", "ua", "krovna", "berserker"
    )


def test_over_long_key_is_truncated_deterministically_and_still_hits() -> None:
    long_title = "довга назва " * 500

    first = build_request_key("ytmusic:search:", "Artist", long_title)
    second = build_request_key("ytmusic:search:", "Artist", long_title)

    assert first == second
    assert len(first.encode("utf-8")) <= MAX_REQUEST_KEY_BYTES


def test_two_different_over_long_keys_stay_distinct() -> None:
    first = build_request_key("ytmusic:search:", "Artist", "a" * 4000)
    second = build_request_key("ytmusic:search:", "Artist", "b" * 4000)

    assert first != second
