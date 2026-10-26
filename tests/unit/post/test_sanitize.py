"""`sanitize`: HTML escaping, control-character stripping and href scheme
validation; five external sources fill hrefs and free text.
"""

from __future__ import annotations

import pytest

from pisniar.post.sanitize import (
    escape_attribute,
    escape_text,
    is_safe_href,
    sanitize_description_text,
    sanitize_metadata_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<script>", "&lt;script&gt;"),
        ("Tom & Jerry", "Tom &amp; Jerry"),
        ('He said "hi"', 'He said "hi"'),
        ("normal text", "normal text"),
    ],
)
def test_escape_text_escapes_angle_brackets_and_ampersand_not_quotes(raw: str, expected: str) -> None:
    assert escape_text(raw) == expected


def test_escape_attribute_also_escapes_quotes() -> None:
    assert escape_attribute('a "quoted" value') == "a &quot;quoted&quot; value"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://open.spotify.com/track/x", True),
        ("http://example.com", True),
        ("javascript:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("ftp://example.com/x", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_is_safe_href_only_allows_http_and_https(url: str, expected: bool) -> None:
    assert is_safe_href(url) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Track\u202ename", "Trackname"),
        ("Track\u202aname", "Trackname"),
        ("Track\u2066name", "Trackname"),
        ("Track\u200bname", "Trackname"),
        ("Track\ufeffname", "Trackname"),
        ("Track\u200fname", "Trackname"),
    ],
)
def test_sanitize_metadata_text_drops_bidi_and_zero_width_controls(raw: str, expected: str) -> None:
    """`U+202E` visually reverses the rest of the line, so the draft the
    admin approves and the post the channel gets read differently."""
    assert sanitize_metadata_text(raw) == expected


@pytest.mark.parametrize("break_character", ["\n", "\r", "\t", "\u2028", "\u2029"])
def test_sanitize_metadata_text_folds_line_breaks_into_spaces(break_character: str) -> None:
    """a newline inside a title lets it forge its own hashtag block."""
    assert sanitize_metadata_text(f"Track{break_character}#Fake") == "Track #Fake"


def test_sanitize_metadata_text_leaves_ordinary_text_untouched() -> None:
    assert sanitize_metadata_text("Димна суміш — 0%Mercury") == "Димна суміш — 0%Mercury"


def test_sanitize_description_text_keeps_the_joiner_that_metadata_text_drops() -> None:
    family = "\U0001f468‍\U0001f469"

    assert sanitize_description_text(family) == family
    assert sanitize_metadata_text(family) == "\U0001f468\U0001f469"


@pytest.mark.parametrize("control", ["‮", "‪", "⁦", "⁩", "‎", "‏", "﻿", "\x07", "​"])
def test_sanitize_description_text_removes_bidi_and_control_characters(control: str) -> None:
    assert sanitize_description_text(f"a{control}b") == "ab"


@pytest.mark.parametrize("break_character", ["\n", "\r", "\t", " ", " "])
def test_sanitize_description_text_folds_line_breaks_into_spaces(break_character: str) -> None:
    assert sanitize_description_text(f"a{break_character}b") == "a b"
