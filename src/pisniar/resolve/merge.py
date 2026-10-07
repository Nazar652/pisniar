"""Deterministic merge into `ResolvedTrack`: every field has its own source
priority table, and a field is only overwritten by a strictly higher-priority source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pisniar.db.models import MANUAL_FIELD_NAMES
from pisniar.logging import get_logger
from pisniar.resolve import link_policy
from pisniar.resolve.cache import normalize_key_part
from pisniar.resolve.json_utils import as_nonempty_str
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack
from pisniar.resolve.urls import is_safe_url

logger = get_logger(__name__)

# scalars a re-resolve may legitimately come back without: a source that
# timed out means "unknown", never "the stored value is wrong".
STORED_SCALAR_FIELDS = (
    "isrc",
    "title",
    "year",
    "release_type",
    "release_title",
    "album_id_spotify",
    "cover_url",
    # the download checks against it, so a `/retry` with a failed source
    # must not null it.
    "duration_ms",
)

# higher number wins. "songlink:spotify"/"songlink:youtube" are qualified —
# the same adapter's metadata quality differs sharply by source entity.
ISRC_SOURCE_PRIORITY = {"spotify": 3, "songlink:spotify": 2, "musicbrainz": 1}
TITLE_ARTIST_SOURCE_PRIORITY = {
    "spotify": 5,
    "songlink:spotify": 4,
    "deezer": 3,
    "itunes": 2,
    "songlink:youtube": 1,
}
# a YouTube entity's title is the uploader's caption — it repeats the artist
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


def merge_platform_link(
    track: ResolvedTrack, platform: str, link: PlatformLink, *, check_allowlist: bool = True
) -> None:
    """the single funnel every candidate passes through; only fresh automatic links meet
    the host list, since a manual link is validated on entry and a stored one was accepted."""
    if not is_safe_url(link.url):
        logger.warning(
            "platform_link_rejected_unsafe_scheme",
            platform=platform,
            source=link.source,
            url=link.url,
        )
        track.warnings.append(f"{platform}: посилання відхилено, небезпечна схема ({link.source})")
        return

    if check_allowlist and link.match is not MatchClass.MANUAL:
        problem = link_policy.automatic_link_problem(platform, link.url)
        if problem is not None:
            logger.warning(
                "platform_link_rejected_by_allowlist",
                platform=platform,
                source=link.source,
                url=link.url,
                problem=problem,
            )
            track.link_rejections.append(f"{platform}: {problem} ({link.source})")
            return

    new_priority = platform_link_priority(link)
    if new_priority <= track.platform_link_priorities.get(platform, -1):
        return

    track.platform_links[platform] = link
    track.platform_link_priorities[platform] = new_priority


def merge_stored_platform_links(track: ResolvedTrack, stored: dict[str, object]) -> None:
    """applied after the fresh links, so a fresh result wins a tie and only a
    strictly stronger stored class (`manual`) overrides it."""
    for platform, raw_link in stored.items():
        link = platform_link_from_stored(raw_link)
        if link is not None:
            merge_platform_link(track, platform, link, check_allowlist=False)


def drop_removed_platforms(track: ResolvedTrack, removed_platforms: Iterable[object]) -> None:
    """after every platform merge: a link the admin removed must not come back from any source."""
    for platform in removed_platforms:
        if isinstance(platform, str):
            track.platform_links.pop(platform, None)
            track.platform_link_priorities.pop(platform, None)


def apply_manual_fields(
    track: ResolvedTrack, stored: Mapping[str, object], manual_fields: Sequence[object]
) -> None:
    """runs last: the stored value replaces the fresh one unconditionally, `None`
    included, which no earlier merge step could restore."""
    overridden: list[str] = []
    for name in MANUAL_FIELD_NAMES:
        if name not in manual_fields:
            continue

        stored_value = stored.get(name)
        if name == "artists":
            stored_value = (
                artists_from_stored(stored_value) if isinstance(stored_value, list) else []
            )
        if getattr(track, name) != stored_value:
            overridden.append(name)
        setattr(track, name, stored_value)

    if overridden:
        logger.warning("manual_fields_kept", fields=overridden)


def merge_stored_scalars(track: ResolvedTrack, stored: dict[str, object]) -> None:
    """otherwise one transient source failure nulls the ISRC, and the
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
    """a resolve that lost a source comes out no worse than what is stored, but
    only a weak fresh value yields: Spotify still wins while iTunes times out."""
    if not track.source_failures:
        return

    kept: list[str] = []

    title = as_nonempty_str(stored_title)
    if title is not None and fresh_value_is_weak(track, "title"):
        track.title = title
        kept.append("title")

    artists = artists_from_stored(stored_artists)
    if artists and fresh_value_is_weak(track, "artists"):
        track.artists = artists
        kept.append("artists")

    if kept:
        logger.warning(
            "stored_fields_kept_after_degraded_resolve",
            fields=kept,
            source_failures=track.source_failures,
        )


def merge_stored_artists(track: ResolvedTrack, stored: list[object]) -> None:
    """carries the admin's `hashtag` override and `spotify_id` across a
    re-resolve that came back without them."""
    overrides = {
        normalize_key_part(name): entry
        for entry in stored
        if isinstance(entry, dict) and (name := entry.get("name")) and isinstance(name, str)
    }
    if not track.artists:
        track.artists = artists_from_stored(overrides.values())
        return

    matched_names = {
        key for artist in track.artists if (key := normalize_key_part(artist.name)) in overrides
    }
    if not matched_names and has_hashtag_override(overrides):
        # spotify lists artists separately, `songlink:spotify` joins them
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
        track.artists = artists_from_stored(overrides.values())
        return

    track.artists = [
        artist_with_stored_overrides(artist, overrides.get(normalize_key_part(artist.name)))
        for artist in track.artists
    ]


def fresh_value_is_weak(track: ResolvedTrack, field_name: str) -> bool:
    """unknown provenance counts as weak: only `merge_field` records a priority,
    so a missing entry means no source vouched for the value."""
    return track.field_priorities.get(field_name, 0) <= WEAK_TITLE_ARTIST_SOURCE_PRIORITY


def artists_from_stored(entries: Iterable[object]) -> list[ArtistInfo]:
    """takes raw JSONB entries, so the stored-title guard can read artists
    straight off the row."""
    return [
        ArtistInfo(
            name=name,
            hashtag=as_nonempty_str(entry.get("hashtag")),
            spotify_id=as_nonempty_str(entry.get("spotify_id")),
        )
        for entry in entries
        if isinstance(entry, dict) and (name := as_nonempty_str(entry.get("name")))
    ]


def has_hashtag_override(overrides: dict[str, dict[str, object]]) -> bool:
    return any(as_nonempty_str(entry.get("hashtag")) for entry in overrides.values())


def artist_with_stored_overrides(
    artist: ArtistInfo, stored: dict[str, object] | None
) -> ArtistInfo:
    if stored is None:
        return artist
    return ArtistInfo(
        name=artist.name,
        hashtag=artist.hashtag or as_nonempty_str(stored.get("hashtag")),
        spotify_id=artist.spotify_id or as_nonempty_str(stored.get("spotify_id")),
    )


def platform_link_from_stored(raw_link: object) -> PlatformLink | None:
    if not isinstance(raw_link, dict):
        return None
    url = as_nonempty_str(raw_link.get("url"))
    if url is None:
        return None

    try:
        match = MatchClass(raw_link.get("match"))
    except ValueError:
        # an unreadable class must not outrank a fresh result, but the link
        # itself is still real data — keep it at the bottom tier.
        match = MatchClass.CONFIRMED
    source = as_nonempty_str(raw_link.get("source")) or "stored"
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
