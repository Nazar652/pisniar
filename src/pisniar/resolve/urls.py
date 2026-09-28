"""URL normalization and syntactic classification, the single bridge between
`bot` and `resolve`. `normalized_url` becomes the UNIQUE `source_url_normalized`,
so changing this logic means a data migration, not just a code change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit, urlunsplit

INTL_PREFIX_RE = re.compile(r"^/intl-[a-z]{2}(-[a-z0-9]+)?(?=/)", re.IGNORECASE)
LANG_PREFIX_RE = re.compile(r"^/[a-z]{2}(?=/)", re.IGNORECASE)

ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# A btree entry tops out near 2704 bytes, and `source_url_normalized` is
# UNIQUE — going over raises ProgramLimitExceededError, not IntegrityError.
MAX_NORMALIZED_URL_BYTES = 2000


def is_safe_url(url: str) -> bool:
    """Canonical for the whole project: checked on a candidate's way in and
    again by `post.sanitize.is_safe_href` at render time."""
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
    """`platform` and `track_id` are set only for `TRACK`; `not_track_reason`
    (e.g. "album", "artist", "playlist") only for `NOT_TRACK`."""

    kind: UrlKind
    normalized_url: str
    platform: str | None = None
    track_id: str | None = None
    not_track_reason: str | None = None


def _path(*segments: str) -> str:
    return "/" + "/".join(segments)


def _url(host: str, path: str, query: str = "") -> str:
    return urlunsplit(("https", host, path, query, ""))


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _classify_spotify(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    stripped = INTL_PREFIX_RE.sub("", path, count=1)
    segments = _segments(stripped)
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, stripped))

    kind_segment, entity_id = segments[0], segments[1]
    normalized = _url(host, _path(kind_segment, entity_id))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="spotify", track_id=entity_id)
    if kind_segment in {"album", "artist", "playlist", "show", "episode"}:
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def _classify_youtube(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    platform = "youtubeMusic" if host == "music.youtube.com" else "youtube"

    if host == "youtu.be":
        segments = _segments(path)
        if not segments:
            return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, path))
        video_id = segments[0]
        normalized = _url("www.youtube.com", "/watch", f"v={video_id}")
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="youtube", track_id=video_id)

    if path == "/watch":
        video_ids = query.get("v")
        if video_ids:
            video_id = video_ids[0]
            normalized = _url(host, "/watch", f"v={video_id}")
            return ClassifiedUrl(UrlKind.TRACK, normalized, platform=platform, track_id=video_id)
        return ClassifiedUrl(UrlKind.NOT_TRACK, _url(host, path), not_track_reason="playlist")

    if path == "/playlist":
        return ClassifiedUrl(UrlKind.NOT_TRACK, _url(host, path), not_track_reason="playlist")

    if path.startswith(("/channel/", "/@", "/c/", "/user/", "/browse/")):
        return ClassifiedUrl(UrlKind.NOT_TRACK, _url(host, path), not_track_reason="artist")

    return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, path))


def _classify_deezer(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    canonical_host = "www.deezer.com"
    segments = _segments(LANG_PREFIX_RE.sub("", path, count=1))
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(canonical_host, path))

    kind_segment, entity_id = segments[0], segments[1]
    normalized = _url(canonical_host, _path(kind_segment, entity_id))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="deezer", track_id=entity_id)
    if kind_segment in {"album", "artist", "playlist"}:
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def _classify_apple_music(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    segments = _segments(path)
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, path))

    country, kind_segment = segments[0], segments[1]
    if kind_segment == "song" and len(segments) >= 4:
        track_id = segments[3]
        normalized = _url(host, _path(country, "song", segments[2], track_id))
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="appleMusic", track_id=track_id)

    if kind_segment == "album":
        album_segments = segments[:4] if len(segments) >= 4 else segments[:2]
        album_path = _path(*album_segments)
        track_ids = query.get("i")
        if track_ids:
            track_id = track_ids[0]
            return ClassifiedUrl(
                UrlKind.TRACK,
                _url(host, album_path, f"i={track_id}"),
                platform="appleMusic",
                track_id=track_id,
            )
        return ClassifiedUrl(UrlKind.NOT_TRACK, _url(host, album_path), not_track_reason="album")

    if kind_segment in {"artist", "playlist"}:
        entity_segments = segments[:3] if len(segments) >= 3 else segments[:2]
        normalized = _url(host, _path(*entity_segments))
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)

    return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, path))


def _classify_tidal(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    segments = [segment for segment in _segments(path) if segment != "browse"]
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, path))

    kind_segment, entity_id = segments[0], segments[1]
    normalized = _url(host, _path(kind_segment, entity_id))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="tidal", track_id=entity_id)
    if kind_segment in {"album", "artist", "playlist"}:
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason=kind_segment)
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def _classify_bandcamp(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    segments = _segments(path)
    if len(segments) < 2:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, path))

    kind_segment, slug = segments[0], segments[1]
    normalized = _url(host, _path(kind_segment, slug))
    if kind_segment == "track":
        return ClassifiedUrl(UrlKind.TRACK, normalized, platform="bandcamp", track_id=slug)
    if kind_segment == "album":
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason="album")
    return ClassifiedUrl(UrlKind.UNKNOWN, normalized)


def _classify_soundcloud(host: str, path: str, query: dict[str, list[str]]) -> ClassifiedUrl:
    canonical_host = "soundcloud.com"
    segments = _segments(path)
    if not segments:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(canonical_host, path))
    if len(segments) < 2:
        return ClassifiedUrl(
            UrlKind.NOT_TRACK, _url(canonical_host, _path(segments[0])), not_track_reason="artist"
        )

    artist, second = segments[0], segments[1]
    if second == "sets":
        normalized = _url(canonical_host, _path(*segments[:3]))
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason="album")
    if second in {"likes", "tracks", "albums", "reposts", "comments", "followers", "following"}:
        normalized = _url(canonical_host, _path(artist))
        return ClassifiedUrl(UrlKind.NOT_TRACK, normalized, not_track_reason="artist")

    normalized = _url(canonical_host, _path(artist, second))
    return ClassifiedUrl(
        UrlKind.TRACK, normalized, platform="soundcloud", track_id=f"{artist}/{second}"
    )


_HOST_CLASSIFIERS: dict[str, Callable[[str, str, dict[str, list[str]]], ClassifiedUrl]] = {
    "open.spotify.com": _classify_spotify,
    "play.spotify.com": _classify_spotify,
    "www.youtube.com": _classify_youtube,
    "youtube.com": _classify_youtube,
    "m.youtube.com": _classify_youtube,
    "music.youtube.com": _classify_youtube,
    "youtu.be": _classify_youtube,
    "www.deezer.com": _classify_deezer,
    "deezer.com": _classify_deezer,
    "music.apple.com": _classify_apple_music,
    "tidal.com": _classify_tidal,
    "listen.tidal.com": _classify_tidal,
    "www.soundcloud.com": _classify_soundcloud,
    "soundcloud.com": _classify_soundcloud,
    "m.soundcloud.com": _classify_soundcloud,
}


def classify_url(raw_url: str) -> ClassifiedUrl:
    """Never raises: unparseable or unrecognized input comes back as `UNKNOWN`."""
    trimmed = raw_url.strip()
    try:
        split = urlsplit(trimmed)
    except ValueError:
        return ClassifiedUrl(UrlKind.UNKNOWN, trimmed)

    if split.scheme not in {"http", "https"} or not split.netloc:
        return ClassifiedUrl(UrlKind.UNKNOWN, trimmed)

    host = split.hostname.lower() if split.hostname else ""
    if host.endswith(".bandcamp.com"):
        return _classify_bandcamp(host, split.path, parse_qs(split.query))

    classifier = _HOST_CLASSIFIERS.get(host)
    if classifier is None:
        return ClassifiedUrl(UrlKind.UNKNOWN, _url(host, split.path))

    return classifier(host, split.path, parse_qs(split.query))


def normalize_url(raw_url: str) -> str:
    return classify_url(raw_url).normalized_url
