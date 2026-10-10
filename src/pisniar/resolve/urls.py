"""URL normalization and syntactic classification, the single bridge between `bot` and `resolve`. `normalized_url`
becomes the UNIQUE `source_url_normalized`, so changing this logic means a data migration, not just a code change."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit, urlunsplit

from pisniar.resolve.matching import is_valid_youtube_video_id

INTERNATIONAL_PREFIX_PATTERN = re.compile(r"^/intl-[a-z]{2}(-[a-z0-9]+)?(?=/)", re.IGNORECASE)
LANGUAGE_PREFIX_PATTERN = re.compile(r"^/[a-z]{2}(?=/)", re.IGNORECASE)

ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# a btree entry tops out near 2704 bytes, and `source_url_normalized` is UNIQUE — going over raises
# ProgramLimitExceededError, not IntegrityError.
MAX_NORMALIZED_URL_BYTES = 2000


def is_safe_url(url: str) -> bool:
    """canonical for the whole project: checked on a candidate's way in and again by `post.sanitize.is_safe_href` at
    render time."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    return scheme in ALLOWED_URL_SCHEMES


def is_within_length_limit(url: str) -> bool:
    return len(url.encode("utf-8")) <= MAX_NORMALIZED_URL_BYTES


class UrlKind(StrEnum):
    TRACK = "track"
    NOT_TRACK = "not_track"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedUrl:
    """`platform` and `track_id` are set only for `TRACK`; `not_track_reason` (e.g. "album", "artist", "playlist") only
    for `NOT_TRACK`."""

    kind: UrlKind
    normalized_url: str
    platform: str | None = None
    track_id: str | None = None
    not_track_reason: str | None = None


def join_path_segments(*segments: str) -> str:
    return "/" + "/".join(segments)


def build_https_url(
    host: str,
    path: str,
    query: str = "",
) -> str:
    return urlunsplit(("https", host, path, query, ""))


def youtube_watch_url(video_id: str) -> str:
    """the one spelling of a YouTube watch URL; the id is validated by the callers that need it."""
    return build_https_url("www.youtube.com", "/watch", f"v={video_id}")


def split_path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def classify_spotify(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    stripped = INTERNATIONAL_PREFIX_PATTERN.sub("", path, count=1)
    segments = split_path_segments(stripped)
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, stripped))

    kind_segment, entity_id = segments[0], segments[1]
    normalized = build_https_url(host, join_path_segments(kind_segment, entity_id))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="spotify", track_id=entity_id)
    if kind_segment in {"album", "artist", "playlist", "show", "episode"}:
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def classify_youtube(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    platform = "youtubeMusic" if host == "music.youtube.com" else "youtube"

    if host == "youtu.be":
        segments = split_path_segments(path)
        if not segments:
            return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, path))
        video_id = segments[0]
        normalized = youtube_watch_url(video_id)
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="youtube", track_id=video_id)

    if path == "/watch":
        video_ids = query.get("v")
        if video_ids:
            video_id = video_ids[0]
            normalized = build_https_url(host, "/watch", f"v={video_id}")
            return ClassifiedUrl(UrlKind.TRACK, normalized, platform=platform, track_id=video_id)
        return ClassifiedUrl(UrlKind.NOT_TRACK, build_https_url(host, path), not_track_reason="playlist")

    if path == "/playlist":
        return ClassifiedUrl(UrlKind.NOT_TRACK, build_https_url(host, path), not_track_reason="playlist")

    if path.startswith(("/channel/", "/@", "/c/", "/user/", "/browse/")):
        return ClassifiedUrl(UrlKind.NOT_TRACK, build_https_url(host, path), not_track_reason="artist")

    return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, path))


def classify_deezer(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    canonical_host = "www.deezer.com"
    segments = split_path_segments(LANGUAGE_PREFIX_PATTERN.sub("", path, count=1))
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(canonical_host, path))

    kind_segment, entity_id = segments[0], segments[1]
    normalized = build_https_url(canonical_host, join_path_segments(kind_segment, entity_id))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="deezer", track_id=entity_id)
    if kind_segment in {"album", "artist", "playlist"}:
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def classify_apple_music(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    segments = split_path_segments(path)
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, path))

    country, kind_segment = segments[0], segments[1]
    if kind_segment == "song" and len(segments) >= 4:
        track_id = segments[3]
        normalized = build_https_url(host, join_path_segments(country, "song", segments[2], track_id))
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="appleMusic", track_id=track_id)

    if kind_segment == "album":
        album_segments = segments[:4] if len(segments) >= 4 else segments[:2]
        album_path = join_path_segments(*album_segments)
        track_ids = query.get("i")
        if track_ids:
            track_id = track_ids[0]
            return ClassifiedUrl(
                UrlKind.TRACK,
                build_https_url(host, album_path, f"i={track_id}"),
                platform="appleMusic",
                track_id=track_id,
            )
        return ClassifiedUrl(UrlKind.NOT_TRACK, build_https_url(host, album_path), not_track_reason="album")

    if kind_segment in {"artist", "playlist"}:
        entity_segments = segments[:3] if len(segments) >= 3 else segments[:2]
        normalized = build_https_url(host, join_path_segments(*entity_segments))
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)

    return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, path))


def classify_tidal(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    segments = [segment for segment in split_path_segments(path) if segment != "browse"]
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, path))

    kind_segment, entity_id = segments[0], segments[1]
    normalized = build_https_url(host, join_path_segments(kind_segment, entity_id))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="tidal", track_id=entity_id)
    if kind_segment in {"album", "artist", "playlist"}:
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def classify_bandcamp(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    segments = split_path_segments(path)
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, path))

    kind_segment, slug = segments[0], segments[1]
    normalized = build_https_url(host, join_path_segments(kind_segment, slug))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="bandcamp", track_id=slug)
    if kind_segment == "album":
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason="album")
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def classify_soundcloud(
    host: str,
    path: str,
    query: dict[str, list[str]],
) -> ClassifiedUrl:
    canonical_host = "soundcloud.com"
    segments = split_path_segments(path)
    if not segments:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(canonical_host, path))
    if len(segments) < 2:
        return ClassifiedUrl(
            UrlKind.NOT_TRACK,
            build_https_url(canonical_host, join_path_segments(segments[0])),
            not_track_reason="artist",
        )

    artist, second = segments[0], segments[1]
    if second == "sets":
        normalized = build_https_url(canonical_host, join_path_segments(*segments[:3]))
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason="album")
    if second in {"likes", "tracks", "albums", "reposts", "comments", "followers", "following"}:
        normalized = build_https_url(canonical_host, join_path_segments(artist))
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason="artist")

    normalized = build_https_url(canonical_host, join_path_segments(artist, second))
    return ClassifiedUrl(UrlKind.TRACK, normalized, platform="soundcloud", track_id=f"{artist}/{second}")


HOST_CLASSIFIERS: dict[str, Callable[[str, str, dict[str, list[str]]], ClassifiedUrl]] = {
    "open.spotify.com": classify_spotify,
    "play.spotify.com": classify_spotify,
    "www.youtube.com": classify_youtube,
    "youtube.com": classify_youtube,
    "m.youtube.com": classify_youtube,
    "music.youtube.com": classify_youtube,
    "youtu.be": classify_youtube,
    "www.deezer.com": classify_deezer,
    "deezer.com": classify_deezer,
    "music.apple.com": classify_apple_music,
    "tidal.com": classify_tidal,
    "listen.tidal.com": classify_tidal,
    "www.soundcloud.com": classify_soundcloud,
    "soundcloud.com": classify_soundcloud,
    "m.soundcloud.com": classify_soundcloud,
}


def classify_url(raw_url: str) -> ClassifiedUrl:
    """never raises: unparseable or unrecognized input comes back as `UNKNOWN`."""
    trimmed = raw_url.strip()
    try:
        split = urlsplit(trimmed)
    except ValueError:
        return ClassifiedUrl(UrlKind.UNKNOWN, trimmed)

    if split.scheme not in {"http", "https"} or not split.netloc:
        return ClassifiedUrl(UrlKind.UNKNOWN, trimmed)

    host = split.hostname.lower() if split.hostname else ""
    if host.endswith(".bandcamp.com"):
        return classify_bandcamp(host, split.path, parse_qs(split.query))

    classifier = HOST_CLASSIFIERS.get(host)
    if classifier is None:
        return ClassifiedUrl(UrlKind.UNKNOWN, build_https_url(host, split.path))

    return classifier(host, split.path, parse_qs(split.query))


def normalize_url(raw_url: str) -> str:
    return classify_url(raw_url).normalized_url


def stored_link_url(platform_links: Mapping[str, object], platform: str) -> str | None:
    """the url of a stored `platform_links` entry; `None` when it is absent or has no url."""
    link = platform_links.get(platform)
    if not isinstance(link, dict):
        return None

    url = link.get("url")
    if not isinstance(url, str) or not url:
        return None
    return url


def track_video_id_of(url: str) -> str | None:
    """the validated `videoId` of a YouTube track url, else `None`."""
    classified = classify_url(url)
    video_id = classified.track_id
    if classified.kind is not UrlKind.TRACK or not is_valid_youtube_video_id(video_id):
        return None
    return video_id
