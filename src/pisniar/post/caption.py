"""Builds a `CaptionDocument` at the media caption limit (1024) and, on overflow, degrades it deterministically: genres
from the tail, then platforms from the tail (Spotify/YT Music last), then the release title. Nothing else."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from pisniar.channel import CHANNEL_LINK_TEXT, CHANNEL_LINK_URL
from pisniar.post.hashtags import default_hashtag
from pisniar.post.render import CaptionDocument, visible_length
from pisniar.resolve.matching import is_valid_spotify_id
from pisniar.resolve.types import ArtistInfo, GenreInfo, MatchClass, PlatformLink

# a cover drops the caption limit from 4096 to 1024. Applied while
# building, so the admin approves the exact text subscribers see.
MEDIA_CAPTION_LIMIT = 1024

# only a hand-placed `youtube` link is a clip; an automatic hit is usually the "— Topic" upload, audio over a still.
CLIP_PLATFORM = "youtube"
CLIP_LABEL_SUFFIX = " (кліп)"

RELEASE_TYPE_NAMES = {
    "single": "сингл",
    "album": "альбом",
    "ep": "міні-альбом",
    "compilation": "збірка",
}
# the types whose release title (and album link) the caption shows
RELEASE_TYPES_WITH_TITLE = ("album", "ep")

PLATFORM_DISPLAY_ORDER = ["spotify", "youtubeMusic", "appleMusic", "bandcamp", "deezer", "tidal"]
PLATFORM_PROTECTED_UNTIL_LAST = {"spotify", "youtubeMusic"}
PLATFORM_DISPLAY_NAMES = {
    "spotify": "Spotify",
    "youtubeMusic": "YouTube Music",
    "youtube": "YouTube",
    "appleMusic": "Apple Music",
    "bandcamp": "Bandcamp",
    "deezer": "Deezer",
    "tidal": "TIDAL",
    "pandora": "Pandora",
    "amazonMusic": "Amazon Music",
    "amazonStore": "Amazon",
    "napster": "Napster",
    "audius": "Audius",
    "boomplay": "Boomplay",
    "anghami": "Anghami",
    "itunes": "iTunes",
    "qobuz": "Qobuz",
    "soundcloud": "SoundCloud",
    "spinrilla": "Spinrilla",
    "audiomack": "Audiomack",
}


@dataclass(frozen=True)
class CaptionDegradation:
    """what did not fit into the limit, in the order it was given up."""

    dropped_genres: tuple[str, ...] = ()
    dropped_platforms: tuple[str, ...] = ()
    release_title_dropped: bool = False
    over_limit: bool = False


class CaptionResult(NamedTuple):
    document: CaptionDocument
    warnings: list[str]
    degradation: CaptionDegradation


def platform_display_name(platform: str) -> str:
    return PLATFORM_DISPLAY_NAMES.get(platform, platform)


def clip_platform(platform_links: dict[str, PlatformLink]) -> str | None:
    """`MANUAL`, not `source == "user_input"`: a hand-pasted link with `source: "manual"` is a clip too."""
    link = platform_links.get(CLIP_PLATFORM)
    if link is None or link.match is not MatchClass.MANUAL:
        return None
    return CLIP_PLATFORM


def platforms_in_display_order(platform_links: dict[str, PlatformLink]) -> list[str]:
    clip = clip_platform(platform_links)
    ordered = [platform for platform in PLATFORM_DISPLAY_ORDER if platform in platform_links]
    rest = sorted(
        platform for platform in platform_links if platform not in PLATFORM_DISPLAY_ORDER and platform != clip
    )

    head = [clip] if clip is not None else []
    return head + ordered + rest


ARTIST_WITHOUT_HASHTAG_WARNING = "хештег для артиста «{name}» не згенеровано (лише цифри)"
LANGUAGE_WITHOUT_HASHTAG_WARNING = "хештег для мови «{name}» не згенеровано"
GENRE_WITHOUT_HASHTAG_WARNING = "хештег для жанру «{name}» не згенеровано (лише цифри)"


def collect_hashtags(
    entries: Iterable[tuple[str | None, str]],
    missing_warning: str,
    warnings: list[str],
) -> list[str]:
    """each entry is `(stored override, name)`; a name that yields no hashtag is
    reported with `missing_warning` and skipped."""
    hashtags: list[str] = []
    for override, name in entries:
        hashtag = override or default_hashtag(name)
        if hashtag is None:
            warnings.append(missing_warning.format(name=name))
            continue
        hashtags.append(f"#{hashtag}")
    return hashtags


def build_document(
    *,
    artists: list[ArtistInfo],
    title: str,
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
    languages: list[str],
    genres: list[GenreInfo],
    platform_links: dict[str, PlatformLink],
    include_release_title: bool,
    warnings: list[str],
) -> CaptionDocument:
    document = CaptionDocument()

    artist_names = ", ".join(artist.name for artist in artists)
    document.append_text(f"{artist_names} — {title}")
    document.append_line_break()
    document.append_line_break()

    artist_hashtags = collect_hashtags(
        [(artist.hashtag, artist.name) for artist in artists],
        ARTIST_WITHOUT_HASHTAG_WARNING,
        warnings,
    )
    document.append_text(" ".join(artist_hashtags))
    if year is not None:
        document.append_text(f" · {year}")
    if release_type is not None:
        release_type_name = RELEASE_TYPE_NAMES.get(release_type, release_type)
        document.append_text(f" · {release_type_name}")
        if include_release_title and release_title and release_type in RELEASE_TYPES_WITH_TITLE:
            document.append_text(" «")
            if album_id_spotify and is_valid_spotify_id(album_id_spotify):
                document.append_link(release_title, f"https://open.spotify.com/album/{album_id_spotify}")
            else:
                if album_id_spotify:
                    # a stray space in the id is a Telegram 400 on the whole
                    # post; plain text loses a link, not the post.
                    warnings.append(f"album_id_spotify «{album_id_spotify}» не base62, лінк знято")
                document.append_text(release_title)
            document.append_text("»")

    # languages lead the line and are never dropped: they are always
    # present, while genres are the first thing degradation gives up.
    groups = [
        collect_hashtags([(None, language) for language in languages], LANGUAGE_WITHOUT_HASHTAG_WARNING, warnings),
        collect_hashtags(
            [(genre.hashtag, genre.name) for genre in genres],
            GENRE_WITHOUT_HASHTAG_WARNING,
            warnings,
        ),
    ]
    hashtag_line = " · ".join(" ".join(group) for group in groups if group)
    if hashtag_line:
        document.append_line_break()
        document.append_text(hashtag_line)

    clip = clip_platform(platform_links)
    ordered_platforms = platforms_in_display_order(platform_links)
    if ordered_platforms:
        document.append_line_break()
        document.append_line_break()
        for index, platform in enumerate(ordered_platforms):
            if index > 0:
                document.append_text(" · ")

            label = platform_display_name(platform)
            if platform == clip:
                label += CLIP_LABEL_SUFFIX
            document.append_link(label, platform_links[platform].url)

    document.append_line_break()
    document.append_line_break()
    document.append_link(CHANNEL_LINK_TEXT, CHANNEL_LINK_URL)

    return document


def build_caption_with_report(
    *,
    artists: list[ArtistInfo],
    title: str,
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
    languages: list[str],
    genres: list[GenreInfo],
    platform_links: dict[str, PlatformLink],
    limit: int = MEDIA_CAPTION_LIMIT,
) -> CaptionResult:
    remaining_genres = list(genres)
    dropped_genres: list[str] = []
    dropped_platforms: list[str] = []
    remaining_platforms = dict(platform_links)
    include_release_title = True

    def attempt() -> tuple[CaptionDocument, list[str]]:
        warnings: list[str] = []
        document = build_document(
            artists=artists,
            title=title,
            year=year,
            release_type=release_type,
            release_title=release_title,
            album_id_spotify=album_id_spotify,
            languages=languages,
            genres=remaining_genres,
            platform_links=remaining_platforms,
            include_release_title=include_release_title,
            warnings=warnings,
        )
        return document, warnings

    document, warnings = attempt()

    while remaining_genres and visible_length(document) > limit:
        dropped_genres.append(remaining_genres.pop().name)
        document, warnings = attempt()

    while remaining_platforms and visible_length(document) > limit:
        ordered = platforms_in_display_order(remaining_platforms)
        droppable = [platform for platform in reversed(ordered) if platform not in PLATFORM_PROTECTED_UNTIL_LAST]
        platform_to_drop = droppable[0] if droppable else ordered[-1]
        del remaining_platforms[platform_to_drop]
        dropped_platforms.append(platform_to_drop)
        document, warnings = attempt()

    release_title_was_dropped = False
    if include_release_title and visible_length(document) > limit:
        include_release_title = False
        release_title_was_dropped = bool(release_title and release_type in RELEASE_TYPES_WITH_TITLE)
        document, warnings = attempt()

    over_limit = visible_length(document) > limit
    if over_limit:
        warnings.append("caption перевищує ліміт навіть після деградації")

    degradation = CaptionDegradation(
        dropped_genres=tuple(dropped_genres),
        dropped_platforms=tuple(dropped_platforms),
        release_title_dropped=release_title_was_dropped,
        over_limit=over_limit,
    )
    return CaptionResult(document, warnings, degradation)


def build_caption(
    *,
    artists: list[ArtistInfo],
    title: str,
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
    languages: list[str],
    genres: list[GenreInfo],
    platform_links: dict[str, PlatformLink],
    limit: int = MEDIA_CAPTION_LIMIT,
) -> tuple[CaptionDocument, list[str]]:
    result = build_caption_with_report(
        artists=artists,
        title=title,
        year=year,
        release_type=release_type,
        release_title=release_title,
        album_id_spotify=album_id_spotify,
        languages=languages,
        genres=genres,
        platform_links=platform_links,
        limit=limit,
    )
    return result.document, result.warnings
