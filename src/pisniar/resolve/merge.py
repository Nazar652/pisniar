"""Deterministic merge into `ResolvedTrack`: every field has its own source
priority table, and a field is only overwritten by a strictly higher-priority source.
"""

from __future__ import annotations

from collections.abc import Iterable

from pisniar.logging import get_logger
from pisniar.resolve.cache import normalize_key_part
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack
from pisniar.resolve.urls import is_safe_url

logger = get_logger(__name__)

# Scalars a re-resolve may legitimately come back without: a source that
# timed out means "unknown", never "the stored value is wrong".
STORED_SCALAR_FIELDS = (
    "isrc",
    "title",
    "year",
    "release_type",
    "release_title",
    "album_id_spotify",
    "cover_url",
    # The download checks against it, so a `/retry` with a failed source
    # must not null it.
    "duration_ms",
)

# Higher number wins. "songlink:spotify"/"songlink:youtube" are qualified —
# the same adapter's metadata quality differs sharply by source entity.
ISRC_SOURCE_PRIORITY = {"spotify": 3, "songlink:spotify": 2, "musicbrainz": 1}
TITLE_ARTIST_SOURCE_PRIORITY = {
    "spotify": 5,
    "songlink:spotify": 4,
    "deezer": 3,
    "itunes": 2,
    "songlink:youtube": 1,
}
# A YouTube entity's title is the uploader's caption — it repeats the artist
# name and carries tails like "(Official Video)", so it never earns a rewrite.
WEAK_TITLE_ARTIST_SOURCE_PRIORITY = TITLE_ARTIST_SOURCE_PRIORITY["songlink:youtube"]
YEAR_RELEASE_SOURCE_PRIORITY = {"spotify": 3, "deezer": 2, "musicbrainz": 1}
COVER_SOURCE_PRIORITY = {"spotify": 3, "songlink:spotify": 2, "songlink:youtube": 2, "deezer": 1}


PLATFORM_LINK_PRIORITY = {
    MatchClass.MANUAL: 4,
    MatchClass.EXACT_ISRC: 3,
    MatchClass.AGGREGATOR: 2,
    MatchClass.CONFIRMED: 1,
}


def platform_link_priority(link: PlatformLink) -> int:
    return PLATFORM_LINK_PRIORITY[link.match]


def merge_field(
    track: ResolvedTrack,
    field_name: str,
    value: object,
    source: str,
    priority_table: dict[str, int],
) -> None:
    if value is None or value == [] or value == "":
        return

    new_priority = priority_table.get(source, 0)
    if new_priority <= track.field_priorities.get(field_name, -1):
        return

    setattr(track, field_name, value)
    track.field_priorities[field_name] = new_priority


def merge_platform_link(track: ResolvedTrack, platform: str, link: PlatformLink) -> None:
    """The single funnel every candidate passes through, so the scheme check
    lives here and not only in `post.render`."""
    if not is_safe_url(link.url):
        logger.warning(
            "platform_link_rejected_unsafe_scheme",
            platform=platform,
            source=link.source,
            url=link.url,
        )
        track.warnings.append(f"{platform}: посилання відхилено, небезпечна схема ({link.source})")
        return

    new_priority = platform_link_priority(link)
    if new_priority <= track.platform_link_priorities.get(platform, -1):
        return

    track.platform_links[platform] = link
    track.platform_link_priorities[platform] = new_priority


def merge_stored_platform_links(track: ResolvedTrack, stored: dict[str, object]) -> None:
    """Applied after the fresh links, so a fresh result wins a tie and only a
    strictly stronger stored class (`manual`) overrides it."""
    for platform, raw_link in stored.items():
        link = _platform_link_from_stored(raw_link)
        if link is not None:
            merge_platform_link(track, platform, link)


def merge_stored_scalars(track: ResolvedTrack, stored: dict[str, object]) -> None:
    """Otherwise one transient source failure nulls the ISRC, and the
    duplicate-track warning for that track never fires again."""
    kept: list[str] = []
    for field_name in STORED_SCALAR_FIELDS:
        stored_value = stored.get(field_name)
        if getattr(track, field_name) is None and stored_value is not None:
            setattr(track, field_name, stored_value)
            kept.append(field_name)

    if kept:
        logger.warning("stored_scalars_kept_after_empty_resolve", fields=kept)


def keep_stored_title_and_artists_after_source_failure(
    track: ResolvedTrack, stored_title: object, stored_artists: list[object]
) -> None:
    """A resolve that lost a source comes out no worse than what is stored, but
    only a weak fresh value yields: Spotify still wins while iTunes times out."""
    if not track.source_failures:
        return

    kept: list[str] = []

    title = _stored_str(stored_title)
    if title is not None and _fresh_value_is_weak(track, "title"):
        track.title = title
        kept.append("title")

    artists = _artists_from_stored(stored_artists)
    if artists and _fresh_value_is_weak(track, "artists"):
        track.artists = artists
        kept.append("artists")

    if kept:
        logger.warning(
            "stored_fields_kept_after_degraded_resolve",
            fields=kept,
            source_failures=track.source_failures,
        )


def merge_stored_artists(track: ResolvedTrack, stored: list[object]) -> None:
    """Carries the admin's `hashtag` override and `spotify_id` across a
    re-resolve that came back without them."""
    overrides = {
        normalize_key_part(name): entry
        for entry in stored
        if isinstance(entry, dict) and (name := entry.get("name")) and isinstance(name, str)
    }
    if not track.artists:
        track.artists = _artists_from_stored(overrides.values())
        return

    matched_names = {
        key for artist in track.artists if (key := normalize_key_part(artist.name)) in overrides
    }
    if not matched_names and _has_hashtag_override(overrides):
        # Spotify lists artists separately, `songlink:spotify` joins them
        # into one string; a key miss must not drop an override.
        logger.warning(
            "stored_artists_kept_after_name_shape_change",
            stored_names=sorted(overrides),
            fresh_names=[artist.name for artist in track.artists],
        )
        track.warnings.append(
            "артистів збережено зі старого резолву: свіжі імена не збіглися, "
            "а в збережених є ручний хештег"
        )
        track.artists = _artists_from_stored(overrides.values())
        return

    track.artists = [
        _artist_with_stored_overrides(artist, overrides.get(normalize_key_part(artist.name)))
        for artist in track.artists
    ]


def _fresh_value_is_weak(track: ResolvedTrack, field_name: str) -> bool:
    """Unknown provenance counts as weak: only `merge_field` records a priority,
    so a missing entry means no source vouched for the value."""
    return track.field_priorities.get(field_name, 0) <= WEAK_TITLE_ARTIST_SOURCE_PRIORITY


def _artists_from_stored(entries: Iterable[object]) -> list[ArtistInfo]:
    """Takes raw JSONB entries, so the stored-title guard can read artists
    straight off the row."""
    return [
        ArtistInfo(
            name=name,
            hashtag=_stored_str(entry.get("hashtag")),
            spotify_id=_stored_str(entry.get("spotify_id")),
        )
        for entry in entries
        if isinstance(entry, dict) and (name := _stored_str(entry.get("name")))
    ]


def _has_hashtag_override(overrides: dict[str, dict[str, object]]) -> bool:
    return any(_stored_str(entry.get("hashtag")) for entry in overrides.values())


def _artist_with_stored_overrides(
    artist: ArtistInfo, stored: dict[str, object] | None
) -> ArtistInfo:
    if stored is None:
        return artist
    return ArtistInfo(
        name=artist.name,
        hashtag=artist.hashtag or _stored_str(stored.get("hashtag")),
        spotify_id=artist.spotify_id or _stored_str(stored.get("spotify_id")),
    )


def _stored_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _platform_link_from_stored(raw_link: object) -> PlatformLink | None:
    if not isinstance(raw_link, dict):
        return None
    url = _stored_str(raw_link.get("url"))
    if url is None:
        return None

    try:
        match = MatchClass(raw_link.get("match"))
    except ValueError:
        # An unreadable class must not outrank a fresh result, but the link
        # itself is still real data — keep it at the bottom tier.
        match = MatchClass.CONFIRMED
    source = _stored_str(raw_link.get("source")) or "stored"
    return PlatformLink(url=url, source=source, match=match)


def merge_isrc(track: ResolvedTrack, isrc: str | None, source: str) -> None:
    merge_field(track, "isrc", isrc, source, ISRC_SOURCE_PRIORITY)


def merge_title(track: ResolvedTrack, title: str | None, source: str) -> None:
    merge_field(track, "title", title, source, TITLE_ARTIST_SOURCE_PRIORITY)


def merge_artists(track: ResolvedTrack, artist_names: list[str], source: str) -> None:
    if not artist_names:
        return
    merge_field(
        track,
        "artists",
        [ArtistInfo(name=name) for name in artist_names],
        source,
        TITLE_ARTIST_SOURCE_PRIORITY,
    )


def merge_year(track: ResolvedTrack, year: int | None, source: str) -> None:
    merge_field(track, "year", year, source, YEAR_RELEASE_SOURCE_PRIORITY)


def merge_release_type(track: ResolvedTrack, release_type: str | None, source: str) -> None:
    merge_field(track, "release_type", release_type, source, YEAR_RELEASE_SOURCE_PRIORITY)


def merge_release_title(track: ResolvedTrack, release_title: str | None, source: str) -> None:
    merge_field(track, "release_title", release_title, source, YEAR_RELEASE_SOURCE_PRIORITY)


def merge_cover_url(track: ResolvedTrack, cover_url: str | None, source: str) -> None:
    merge_field(track, "cover_url", cover_url, source, COVER_SOURCE_PRIORITY)
