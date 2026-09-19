"""Assembling a `CaptionDocument` from track data (D-29 template) and, on
overflow, degrading it deterministically: genres from the tail, then
platforms from the tail (Spotify/YT Music protected until last), then the
release title. First line/artist hashtags/channel link are never touched.
"""

from __future__ import annotations

from pisniar.post.hashtags import default_hashtag
from pisniar.post.render import CaptionDocument, visible_length
from pisniar.resolve.matching import is_valid_spotify_id
from pisniar.resolve.types import ArtistInfo, GenreInfo, PlatformLink

CHANNEL_LINK_TEXT = "Піснярня 🇺🇦"
CHANNEL_LINK_URL = "https://t.me/pisniarnia"

RELEASE_TYPE_NAMES = {"single": "сингл", "album": "альбом", "compilation": "збірка"}

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
    "yandex": "Yandex Music",
    "audius": "Audius",
    "boomplay": "Boomplay",
    "anghami": "Anghami",
    "itunes": "iTunes",
    "qobuz": "Qobuz",
    "soundcloud": "SoundCloud",
    "spinrilla": "Spinrilla",
    "audiomack": "Audiomack",
}


def platform_display_name(platform: str) -> str:
    return PLATFORM_DISPLAY_NAMES.get(platform, platform)


def _ordered_platforms(platform_links: dict[str, PlatformLink]) -> list[str]:
    ordered = [platform for platform in PLATFORM_DISPLAY_ORDER if platform in platform_links]
    rest = sorted(platform for platform in platform_links if platform not in PLATFORM_DISPLAY_ORDER)
    return ordered + rest


def _artist_hashtags(artists: list[ArtistInfo], warnings: list[str]) -> list[str]:
    hashtags: list[str] = []
    for artist in artists:
        hashtag = artist.hashtag or default_hashtag(artist.name)
        if hashtag is None:
            warnings.append(f"хештег для артиста «{artist.name}» не згенеровано (лише цифри)")
            continue
        hashtags.append(f"#{hashtag}")
    return hashtags


def _genre_hashtags(genres: list[GenreInfo], warnings: list[str]) -> list[str]:
    hashtags: list[str] = []
    for genre in genres:
        hashtag = genre.hashtag or default_hashtag(genre.name)
        if hashtag is None:
            warnings.append(f"хештег для жанру «{genre.name}» не згенеровано (лише цифри)")
            continue
        hashtags.append(f"#{hashtag}")
    return hashtags


def _build_document(
    *,
    artists: list[ArtistInfo],
    title: str,
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
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

    document.append_text(" ".join(_artist_hashtags(artists, warnings)))
    if year is not None:
        document.append_text(f" · {year}")
    if release_type is not None:
        release_type_name = RELEASE_TYPE_NAMES.get(release_type, release_type)
        document.append_text(f" · {release_type_name}")
        if include_release_title and release_title and release_type == "album":
            document.append_text(" «")
            if album_id_spotify and is_valid_spotify_id(album_id_spotify):
                document.append_link(
                    release_title, f"https://open.spotify.com/album/{album_id_spotify}"
                )
            else:
                if album_id_spotify:
                    # A stray space in the id is a Telegram 400 on the whole
                    # post; plain text loses a link, not the post (R2-16).
                    warnings.append(f"album_id_spotify «{album_id_spotify}» не base62, лінк знято")
                document.append_text(release_title)
            document.append_text("»")

    genre_hashtags = _genre_hashtags(genres, warnings)
    if genre_hashtags:
        document.append_line_break()
        document.append_text(" ".join(genre_hashtags))

    ordered_platforms = _ordered_platforms(platform_links)
    if ordered_platforms:
        document.append_line_break()
        document.append_line_break()
        for index, platform in enumerate(ordered_platforms):
            if index > 0:
                document.append_text(" · ")
            document.append_link(platform_display_name(platform), platform_links[platform].url)

    document.append_line_break()
    document.append_line_break()
    document.append_link(CHANNEL_LINK_TEXT, CHANNEL_LINK_URL)

    return document


def build_caption(
    *,
    artists: list[ArtistInfo],
    title: str,
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
    genres: list[GenreInfo],
    platform_links: dict[str, PlatformLink],
    limit: int = 4096,
) -> tuple[CaptionDocument, list[str]]:
    remaining_genres = list(genres)
    remaining_platforms = dict(platform_links)
    include_release_title = True

    def attempt() -> tuple[CaptionDocument, list[str]]:
        warnings: list[str] = []
        document = _build_document(
            artists=artists,
            title=title,
            year=year,
            release_type=release_type,
            release_title=release_title,
            album_id_spotify=album_id_spotify,
            genres=remaining_genres,
            platform_links=remaining_platforms,
            include_release_title=include_release_title,
            warnings=warnings,
        )
        return document, warnings

    document, warnings = attempt()

    while remaining_genres and visible_length(document) > limit:
        remaining_genres.pop()
        document, warnings = attempt()

    while remaining_platforms and visible_length(document) > limit:
        ordered = _ordered_platforms(remaining_platforms)
        droppable = [
            platform
            for platform in reversed(ordered)
            if platform not in PLATFORM_PROTECTED_UNTIL_LAST
        ]
        platform_to_drop = droppable[0] if droppable else ordered[-1]
        del remaining_platforms[platform_to_drop]
        document, warnings = attempt()

    if include_release_title and visible_length(document) > limit:
        include_release_title = False
        document, warnings = attempt()

    if visible_length(document) > limit:
        warnings.append("caption перевищує ліміт навіть після деградації")

    return document, warnings
