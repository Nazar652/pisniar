"""Shared domain types for the resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# the `source` the bot seeds a submitted YouTube clip with, before any
# network call — checked by name in three places, so it lives here.
USER_INPUT_SOURCE = "user_input"


class MatchClass(StrEnum):
    """`REJECTED` is deliberately absent: a rejected candidate is never written to `platform_links` at all."""

    EXACT_ISRC = "exact_isrc"
    AGGREGATOR = "aggregator"
    CONFIRMED = "confirmed"
    MANUAL = "manual"


@dataclass(frozen=True)
class PlatformLink:
    url: str
    source: str
    match: MatchClass


@dataclass(frozen=True)
class ArtistInfo:
    name: str
    hashtag: str | None = None
    spotify_id: str | None = None


@dataclass(frozen=True)
class GenreInfo:
    name: str
    hashtag: str | None
    source: str


@dataclass(frozen=True)
class TrackCandidate:
    """a name-search hit that still needs `matching.is_match()` before it can become a `PlatformLink`."""

    platform: str
    url: str
    source: str
    artist_name: str
    title: str
    duration_ms: int | None
    isrc: str | None = None


@dataclass
class ResolvedTrack:
    """mutable accumulator `merge.py` folds source results into, field by field."""

    isrc: str | None = None
    title: str | None = None
    artists: list[ArtistInfo] = field(default_factory=list)
    year: int | None = None
    release_type: str | None = None
    release_title: str | None = None
    cover_url: str | None = None
    duration_ms: int | None = None
    album_id_spotify: str | None = None
    deezer_album_id: int | None = None
    platform_links: dict[str, PlatformLink] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # sources that raised instead of answering. Kept apart from
    # `warnings` because the admin is alerted about these, not just logged.
    source_failures: list[str] = field(default_factory=list)
    # automatic links the host list refused; the admin is told about these too
    link_rejections: list[str] = field(default_factory=list)

    # internal bookkeeping for `merge.py`'s priority rules — the highest
    # priority rank that has won each field/platform so far.
    field_priorities: dict[str, int] = field(default_factory=dict)
    platform_link_priorities: dict[str, int] = field(default_factory=dict)
