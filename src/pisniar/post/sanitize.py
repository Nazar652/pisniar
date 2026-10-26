"""HTML safety for untrusted source metadata before it becomes caption HTML."""

from __future__ import annotations

import unicodedata
from html import escape

from pisniar.resolve.urls import is_safe_url

# Cc/Cf cover RLO/LRO, the isolates, zero-width joiners and the BOM.
CONTROL_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf"})
LINE_BREAK_CHARACTERS = frozenset("\n\r\t\v\f\u2028\u2029")
ZERO_WIDTH_JOINER = "\u200d"


def sanitize_metadata_text(value: str) -> str:
    """`U+202E` flips a title's visible order and a raw newline forges a hashtag block: the admin would approve one
    draft and the channel get another."""
    characters: list[str] = []
    for character in value:
        if character in LINE_BREAK_CHARACTERS:
            characters.append(" ")
        elif unicodedata.category(character) not in CONTROL_CHARACTER_CATEGORIES:
            characters.append(character)
    return "".join(characters)


def sanitize_description_text(value: str) -> str:
    """as `sanitize_metadata_text`, but the zero-width joiner stays: it holds a family or flag emoji together. Bidi
    controls and the BOM go; a line break only ever comes from the description parser."""
    characters: list[str] = []
    for character in value:
        if character in LINE_BREAK_CHARACTERS:
            characters.append(" ")
        elif character == ZERO_WIDTH_JOINER or unicodedata.category(character) not in CONTROL_CHARACTER_CATEGORIES:
            characters.append(character)
    return "".join(characters)


def escape_text(value: str) -> str:
    """`quote=False`: never placed inside an attribute."""
    return escape(value, quote=False)


def escape_attribute(value: str) -> str:
    return escape(value, quote=True)


def is_safe_href(url: str) -> bool:
    """delegates, so the check on a candidate's way in and on its way out can never drift apart."""
    return is_safe_url(url)
