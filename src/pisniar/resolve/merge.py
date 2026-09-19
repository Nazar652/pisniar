"""Deterministic merge into `ResolvedTrack` (contract p.15 + revision-4
addendum). Every field has its own source-priority order; a field is only
overwritten by a strictly higher-priority source, and `platform_links`
follows the four-tier `manual > exact_isrc > aggregator > confirmed` order.
"""

from __future__ import annotations

from pisniar.logging import get_logger
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack
from pisniar.resolve.urls import is_safe_url

logger = get_logger(__name__)

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
YEAR_RELEASE_SOURCE_PRIORITY = {"spotify": 3, "deezer": 2, "musicbrainz": 1}
COVER_SOURCE_PRIORITY = {"spotify": 3, "songlink:spotify": 2, "songlink:youtube": 2, "deezer": 1}


PLATFORM_LINK_PRIORITY = {
    MatchClass.MANUAL: 4,
    MatchClass.EXACT_ISRC: 3,
    MatchClass.AGGREGATOR: 2,
    MatchClass.CONFIRMED: 1,
}


def platform_link_priority(link: PlatformLink) -> int:
    """`manual`(4) > direct-ISRC API(3) > song.link cluster(2) > confirmed
    name-search(1). Read straight off `match` since D-39 — no source-name
    special case left to drift out of sync with the class."""
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
    """The single funnel every candidate passes through — which is why the
    scheme check lives here and not only in `post.render` (F-8)."""
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
    """Folds what is already in `track.platform_links` (JSONB) back into a
    fresh resolve. Applied after the fresh links, so a fresh result wins a
    tie and only a strictly stronger stored class (`manual`) overrides it."""
    for platform, raw_link in stored.items():
        link = _platform_link_from_stored(raw_link)
        if link is not None:
            merge_platform_link(track, platform, link)


def merge_stored_artists(track: ResolvedTrack, stored: list[object]) -> None:
    """Carries the admin's `hashtag` override (the only one D-29 provides)
    and `spotify_id` across a re-resolve that came back without them."""
    overrides = {
        name: entry
        for entry in stored
        if isinstance(entry, dict) and (name := entry.get("name")) and isinstance(name, str)
    }
    if not track.artists:
        track.artists = [
            ArtistInfo(
                name=name,
                hashtag=_stored_str(entry.get("hashtag")),
                spotify_id=_stored_str(entry.get("spotify_id")),
            )
            for name, entry in overrides.items()
        ]
        return

    track.artists = [
        _artist_with_stored_overrides(artist, overrides.get(artist.name))
        for artist in track.artists
    ]


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
