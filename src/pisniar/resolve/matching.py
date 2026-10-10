"""Name-search verification: exact artist+title match after normalization, plus a duration tolerance."""

from __future__ import annotations

import re
import unicodedata
from typing import TypeGuard

from pisniar.resolve.types import TrackCandidate

# across 9 reference tracks the widest gap between two platforms holding the same recording is 965 ms, so 3000 ms has
# headroom.
DURATION_TOLERANCE_MS = 3000

SUFFIX_PATTERN_SOURCES = (
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
SUFFIX_PATTERN = re.compile("|".join(SUFFIX_PATTERN_SOURCES), re.IGNORECASE)
FEATURING_PATTERN = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE)
PUNCTUATION_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)
WHITESPACE_PATTERN = re.compile(r"\s+")
ARTIST_SEPARATOR_PATTERN = re.compile(r",|&|/| x | vs\.? | with ", re.IGNORECASE)
ISRC_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
SPOTIFY_ID_PATTERN = re.compile(r"^[0-9A-Za-z]{22}$")
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_valid_isrc(value: str | None) -> bool:
    """checked before an ISRC goes into a URL path (`/track/isrc:...`), where a stray `/` or `?` from a source would
    change which endpoint we call."""
    return value is not None and ISRC_PATTERN.match(value) is not None


def is_valid_spotify_id(value: str | None) -> bool:
    """source-supplied and pasted into a URL path, same as an ISRC."""
    return value is not None and SPOTIFY_ID_PATTERN.match(value) is not None


def is_valid_youtube_video_id(value: str | None) -> TypeGuard[str]:
    """source-supplied and pasted into a URL, and into `yt-dlp`."""
    return value is not None and YOUTUBE_VIDEO_ID_PATTERN.match(value) is not None


def strip_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def normalize_text(value: str) -> str:
    normalized = strip_diacritics(unicodedata.normalize("NFC", value)).casefold()
    normalized = SUFFIX_PATTERN.sub("", normalized)
    normalized = FEATURING_PATTERN.sub("", normalized)
    normalized = PUNCTUATION_PATTERN.sub("", normalized)
    return WHITESPACE_PATTERN.sub(" ", normalized).strip()


def normalize_artist_set(value: str) -> frozenset[str]:
    """order- and separator-independent: `"A & B"` and `"A, B"` normalize to the same set, so a platform crediting
    artists differently still matches."""
    parts = ARTIST_SEPARATOR_PATTERN.split(value)
    return frozenset(normalize_text(part) for part in parts if normalize_text(part))


def known_duration(value: int | None) -> int | None:
    """song.link's YouTube entity answers `duration: 0`, and 0 ms is an absence, not a track length."""
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
    """never fuzzy or substring; the duration counts only when both sides know it."""
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


def find_matching_candidate(
    candidates: list[TrackCandidate],
    *,
    artist: str,
    title: str,
    duration_ms: int | None,
) -> TrackCandidate | None:
    """the first candidate that passes `is_match()`; the order is the source's relevance order."""
    for candidate in candidates:
        if is_match(
            reference_artist=artist,
            reference_title=title,
            reference_duration_ms=duration_ms,
            candidate_artist=candidate.artist_name,
            candidate_title=candidate.title,
            candidate_duration_ms=candidate.duration_ms,
        ):
            return candidate
    return None


def rejected_candidates_warning(
    source_label: str,
    candidate_count: int,
    artist: str,
    title: str,
) -> str:
    return f"{source_label}: {candidate_count} кандидат(и) відхилено звіркою для «{artist} — {title}»"
