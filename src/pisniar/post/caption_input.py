"""Stored rows to caption input: the one conversion the worker and the Mini App share,
so a caption rebuilt from the database is the caption the worker built.
"""

from __future__ import annotations

from collections.abc import Mapping

from pisniar.db.models import DEFAULT_LANGUAGES
from pisniar.post.caption import MEDIA_CAPTION_LIMIT, CaptionResult, build_caption_with_report
from pisniar.resolve.merge import artists_from_stored, platform_link_from_stored
from pisniar.resolve.types import GenreInfo, PlatformLink


def genres_from_stored(stored: list[object]) -> list[GenreInfo]:
    """reads `post.genres` (JSONB) back as domain objects, so a caption
    rebuilt after an empty genre fetch says what the column says."""
    genres: list[GenreInfo] = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue

        hashtag = entry.get("hashtag")
        source = entry.get("source")
        genres.append(
            GenreInfo(
                name=name,
                hashtag=hashtag if isinstance(hashtag, str) and hashtag else None,
                source=source if isinstance(source, str) and source else "stored",
            )
        )
    return genres


def languages_from_stored(stored: list[object]) -> list[str]:
    """falls back to the schema default, never to an empty line: the language
    is always shown."""
    languages = [entry for entry in stored if isinstance(entry, str) and entry]
    return languages or list(DEFAULT_LANGUAGES)


def platform_links_from_stored(stored: Mapping[str, object]) -> dict[str, PlatformLink]:
    links: dict[str, PlatformLink] = {}
    for platform, raw_link in stored.items():
        link = platform_link_from_stored(raw_link)
        if link is not None:
            links[platform] = link
    return links


def build_caption_from_rows(
    *,
    title: str,
    artists: list[object],
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
    languages: list[object],
    genres: list[object],
    platform_links: Mapping[str, object],
    limit: int = MEDIA_CAPTION_LIMIT,
) -> CaptionResult:
    return build_caption_with_report(
        artists=artists_from_stored(artists),
        title=title,
        year=year,
        release_type=release_type,
        release_title=release_title,
        album_id_spotify=album_id_spotify,
        languages=languages_from_stored(languages),
        genres=genres_from_stored(genres),
        platform_links=platform_links_from_stored(platform_links),
        limit=limit,
    )
