"""Name-search verification (contract p.12): exact artist+title match after
normalization, plus a duration tolerance. Constants calibrated live against
3 of the 9 spike reference tracks during 2.6 — see `test_matching.py`.
"""

from __future__ import annotations

import re
import unicodedata

# Calibrated live on 3 of the 9 spike reference tracks (decision-log.md):
# correct cross-platform matches differed by <=1000ms; 3000ms has headroom.
DURATION_TOLERANCE_MS = 3000

_SUFFIX_PATTERNS = (
    r"\(official video\)",
    r"\(official audio\)",
    r"\(official music video\)",
    r"\(official lyric video\)",
    r"\(lyric video\)",
    r"\(visualizer\)",
    r"\(audio\)",
    r"\(official\)",
    r"- topic$",
)
_SUFFIX_RE = re.compile("|".join(_SUFFIX_PATTERNS), re.IGNORECASE)
_FEAT_RE = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE)
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_ARTIST_SEPARATOR_RE = re.compile(r",|&|/| x | vs\.? | with ", re.IGNORECASE)
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def is_valid_isrc(value: str | None) -> bool:
    """`CCXXXYYNNNNN`. Checked before an ISRC is pasted into a URL path
    (`/track/isrc:...`), where a source-supplied stray `/` or `?` would
    otherwise change which endpoint we call."""
    return value is not None and _ISRC_RE.match(value) is not None


def strip_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def normalize_text(value: str) -> str:
    """casefold + NFC + strip diacritics + strip `(Official Video)`/`- Topic`/
    `feat. ...` tails + strip punctuation + collapse whitespace."""
    normalized = strip_diacritics(unicodedata.normalize("NFC", value)).casefold()
    normalized = _SUFFIX_RE.sub("", normalized)
    normalized = _FEAT_RE.sub("", normalized)
    normalized = _PUNCTUATION_RE.sub("", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def normalize_artist_set(value: str) -> frozenset[str]:
    """Order- and separator-independent: `"A & B"` and `"A, B"` normalize to
    the same set, so a platform crediting artists differently still matches."""
    parts = _ARTIST_SEPARATOR_RE.split(value)
    return frozenset(normalize_text(part) for part in parts if normalize_text(part))


def is_match(
    *,
    reference_artist: str,
    reference_title: str,
    reference_duration_ms: int | None,
    candidate_artist: str,
    candidate_title: str,
    candidate_duration_ms: int | None,
) -> bool:
    """Exact match after normalization on both artist and title, plus a
    duration check when both sides know it. Never fuzzy/substring (contract
    p.12: "точний збіг після нормалізації")."""
    if normalize_text(reference_title) != normalize_text(candidate_title):
        return False
    if normalize_artist_set(reference_artist) != normalize_artist_set(candidate_artist):
        return False
    return not (
        reference_duration_ms is not None
        and candidate_duration_ms is not None
        and abs(reference_duration_ms - candidate_duration_ms) > DURATION_TOLERANCE_MS
    )
