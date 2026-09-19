"""HTML safety for caption rendering (contract p.4/security-review scope):
escaping untrusted metadata, stripping invisible control characters, and
validating link schemes before anything from a source becomes literal HTML.
"""

from __future__ import annotations

import unicodedata
from html import escape

from pisniar.resolve.urls import is_safe_url

# Cc/Cf cover RLO/LRO, the isolates, zero-width joiners and the BOM.
CONTROL_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf"})
LINE_BREAK_CHARACTERS = frozenset("\n\r\t\v\f\u2028\u2029")


def sanitize_metadata_text(value: str) -> str:
    """Fold line breaks into spaces and drop Unicode Cc/Cf. `U+202E` flips
    the visible order of a title, and a raw newline lets it forge a hashtag
    block — the admin would approve one draft and the channel get another."""
    characters: list[str] = []
    for character in value:
        if character in LINE_BREAK_CHARACTERS:
            characters.append(" ")
        elif unicodedata.category(character) not in CONTROL_CHARACTER_CATEGORIES:
            characters.append(character)
    return "".join(characters)


def escape_text(value: str) -> str:
    """Escape `<`, `>`, `&` for HTML text content. `quote=False`: this is
    never placed inside an attribute, so `"`/`'` don't need escaping here."""
    return escape(value, quote=False)


def escape_attribute(value: str) -> str:
    """Escape for use inside an HTML attribute value (e.g. `href="..."`)."""
    return escape(value, quote=True)


def is_safe_href(url: str) -> bool:
    """Render-time guard. Delegates to `resolve.urls.is_safe_url` so the
    check that runs on a candidate's way in and the one that runs on its way
    out can never drift apart."""
    return is_safe_url(url)
