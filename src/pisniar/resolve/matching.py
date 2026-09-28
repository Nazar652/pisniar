"""Name-search verification: exact artist+title match after normalization,
plus a duration tolerance.
"""

from __future__ import annotations

import re
import unicodedata

# Across 9 reference tracks the widest gap between two platforms holding the
# same recording is 965 ms, so 3000 ms has headroom.
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
_SPOTIFY_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_valid_isrc(value: str | None) -> bool:
    """Checked before an ISRC goes into a URL path (`/track/isrc:...`), where a
    stray `/` or `?` from a source would change which endpoint we call."""
    return value is not None and _ISRC_RE.match(value) is not None


def is_valid_spotify_id(value: str | None) -> bool:
    """Source-supplied and pasted into a URL path, same as an ISRC."""
    return value is not None and _SPOTIFY_ID_RE.match(value) is not None


def is_valid_youtube_video_id(value: str | None) -> bool:
    """Source-supplied and pasted into a URL, and into `yt-dlp`."""
    return value is not None and _YOUTUBE_VIDEO_ID_RE.match(value) is not None


def strip_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def normalize_text(value: str) -> str:
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


def known_duration(value: int | None) -> int | None:
    """song.link's YouTube entity answers `duration: 0`, and 0 ms is an absence,
    not a track length."""
    return value if value is not None and value > 0 else None


def is_match(
    *,
    reference_artist: str,
    reference_title: str,
    reference_duration_ms: int | None,
    candidate_artist: str,
    candidate_title: str,
    candidate_duration_ms: int | None,
) -> bool:
    """Never fuzzy or substring; the duration counts only when both sides know it."""
    if normalize_text(reference_title) != normalize_text(candidate_title):
        return False
    if normalize_artist_set(reference_artist) != normalize_artist_set(candidate_artist):
        return False

    reference_duration = known_duration(reference_duration_ms)
    candidate_duration = known_duration(candidate_duration_ms)
    return not (
        reference_duration is not None
        and candidate_duration is not None
        and abs(reference_duration - candidate_duration) > DURATION_TOLERANCE_MS
    )
