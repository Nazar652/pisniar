"""Default hashtag rule. A purely-digit or empty result emits none:
Telegram doesn't highlight `#1995`, and a bare `#` breaks the layout.
"""

from __future__ import annotations

import re
import unicodedata

DISALLOWED_CHARACTER_PATTERN = re.compile(r"[^\w]", re.UNICODE)


def clean_word(word: str) -> str:
    return DISALLOWED_CHARACTER_PATTERN.sub("", word)


def capitalize_first_letter(word: str) -> str:
    if not word:
        return word
    return word[0].upper() + word[1:]


def default_hashtag(name: str) -> str | None:
    """`None` for a purely-digit or empty result — caller must show a
    warning and leave the hashtag unset rather than emit a broken one."""
    normalized = unicodedata.normalize("NFC", name)
    words = [clean_word(word) for word in normalized.split() if clean_word(word)]
    if not words:
        return None

    glued = words[0] + "".join(capitalize_first_letter(word) for word in words[1:])
    if not glued or glued.isdigit():
        return None
    return glued
