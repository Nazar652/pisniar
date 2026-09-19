"""Default hashtag rule (D-29): NFC -> split on whitespace -> drop
disallowed characters per word -> first word unchanged, rest capitalized
-> glue together. A purely-digit or empty result emits no hashtag at all
(Telegram doesn't highlight `#1995`, and a bare `#` breaks the layout).
"""

from __future__ import annotations

import re
import unicodedata

_DISALLOWED_CHARACTER_RE = re.compile(r"[^\w]", re.UNICODE)


def _clean_word(word: str) -> str:
    return _DISALLOWED_CHARACTER_RE.sub("", word)


def _capitalize_first_letter(word: str) -> str:
    if not word:
        return word
    return word[0].upper() + word[1:]


def default_hashtag(name: str) -> str | None:
    """`None` for a purely-digit or empty result — caller must show a
    warning and leave the hashtag unset rather than emit a broken one."""
    normalized = unicodedata.normalize("NFC", name)
    words = [_clean_word(word) for word in normalized.split() if _clean_word(word)]
    if not words:
        return None

    glued = words[0] + "".join(_capitalize_first_letter(word) for word in words[1:])
    if not glued or glued.isdigit():
        return None
    return glued
