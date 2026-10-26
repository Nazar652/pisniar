"""Validating an edit of a post's fields and planning what it changes, as pure functions.

Nothing here touches the database or Telegram: the stored values come in as plain data, and the
result is the exact set of values to write, so a preview and a save cannot disagree.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pisniar.db.models import MANUAL_FIELD_NAMES, MAX_DESCRIPTION_LENGTH
from pisniar.post.caption import RELEASE_TYPE_NAMES, is_shown_in_caption
from pisniar.post.caption_input import genres_from_stored
from pisniar.post.description import normalize_description, parse_description
from pisniar.resolve.json_utils import as_dict, as_nonempty_str
from pisniar.resolve.link_policy import ALLOWED_HOSTS, check_manual_link, parse_spotify_album_link
from pisniar.resolve.merge import artists_from_stored
from pisniar.webapp.errors import FieldsInvalidError
from pisniar.webapp.schemas import FieldError, FieldsInput

RELEASE_TYPES = tuple(RELEASE_TYPE_NAMES)
MIN_YEAR = 1900
HASHTAG_PATTERN = re.compile(r"^\w{1,64}$")
MANUAL_SOURCE = "manual"
MANUAL_MATCH = "manual"
YOUTUBE_MUSIC = "youtubeMusic"


@dataclass(frozen=True)
class StoredFields:
    """the editable values as they sit in the rows, still raw JSON where the column is JSON."""

    title: str | None
    artists: list[object]
    languages: list[object]
    genres: list[object]
    year: int | None
    release_type: str | None
    release_title: str | None
    album_id_spotify: str | None
    description: str | None
    platform_links: dict[str, object]
    manual_fields: list[object]
    removed_platforms: list[object]


@dataclass(frozen=True)
class PlannedFields:
    """what to write, plus which of the fields a human actually changed."""

    title: str
    artists: list[dict[str, object]]
    languages: list[str]
    genres: list[dict[str, object]]
    year: int | None
    release_type: str | None
    release_title: str | None
    album_id_spotify: str | None
    description: str | None
    platform_links: dict[str, object]
    manual_fields: list[str]
    removed_platforms: list[str]
    changed_fields: tuple[str, ...]
    youtube_music_changed: bool


def collect(
    errors: list[FieldError],
    field: str,
    message: str,
) -> None:
    errors.append(FieldError(field=field, message=message))


def valid_hashtag(value: str) -> bool:
    return HASHTAG_PATTERN.match(value) is not None and not value.isdigit()


def plan_hashtag(
    value: str | None,
    field: str,
    errors: list[FieldError],
) -> str | None:
    """`None` stays unset (the default is computed at caption time); a given one must fit."""
    if value is None or not value.strip():
        return None

    hashtag = value.strip()
    if not valid_hashtag(hashtag):
        collect(
            errors,
            field,
            "Хештег: літери, цифри й підкреслення без #, до 64 символів, не самі цифри.",
        )
        return None
    return hashtag


def plan_title(value: str, errors: list[FieldError]) -> str:
    title = value.strip()
    if not title:
        collect(errors, "title", "Назва не може бути порожньою.")
    return title


def plan_artists(
    inputs: Iterable[tuple[str, str | None]],
    stored: Sequence[object],
    errors: list[FieldError],
) -> tuple[list[dict[str, object]], bool]:
    """a known name keeps its Spotify id, a new one has none; the flag says the names changed
    (a hashtag alone never freezes the names against a re-resolve)."""
    spotify_ids = {artist.name: artist.spotify_id for artist in artists_from_stored(stored)}

    problems_before = len(errors)
    planned: list[dict[str, object]] = []
    for index, (raw_name, raw_hashtag) in enumerate(inputs):
        name = raw_name.strip()
        if not name:
            collect(errors, f"artists[{index}].name", "Ім'я артиста не може бути порожнім.")
            continue
        hashtag = plan_hashtag(raw_hashtag, f"artists[{index}].hashtag", errors)
        planned.append({"name": name, "hashtag": hashtag, "spotify_id": spotify_ids.get(name)})

    if not planned and len(errors) == problems_before:
        collect(errors, "artists", "Потрібен принаймні один артист.")

    stored_names = [artist.name for artist in artists_from_stored(stored)]
    return planned, [entry["name"] for entry in planned] != stored_names


def plan_languages(values: Iterable[str], errors: list[FieldError]) -> list[str]:
    problems_before = len(errors)
    languages: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        language = raw.strip()
        if not language:
            collect(errors, f"languages[{index}]", "Мова не може бути порожньою.")
        elif language.casefold() in seen:
            collect(errors, f"languages[{index}]", f"Мову «{language}» вказано двічі.")
        else:
            seen.add(language.casefold())
            languages.append(language)

    if not languages and len(errors) == problems_before:
        collect(errors, "languages", "Потрібна принаймні одна мова.")
    return languages


def plan_genres(
    inputs: Iterable[tuple[str, str | None]],
    stored: Sequence[object],
    errors: list[FieldError],
) -> list[dict[str, object]]:
    """a known genre keeps its source, a new one is marked as entered by hand."""
    sources = {genre.name: genre.source for genre in genres_from_stored(stored)}

    genres: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, (raw_name, raw_hashtag) in enumerate(inputs):
        name = raw_name.strip()
        if not name:
            collect(errors, f"genres[{index}].name", "Назва жанру не може бути порожньою.")
            continue
        if name.casefold() in seen:
            collect(errors, f"genres[{index}].name", f"Жанр «{name}» вказано двічі.")
            continue
        seen.add(name.casefold())
        hashtag = plan_hashtag(raw_hashtag, f"genres[{index}].hashtag", errors)
        genres.append({"name": name, "hashtag": hashtag, "source": sources.get(name, MANUAL_SOURCE)})
    return genres


def plan_year(
    value: int | None,
    current_year: int,
    errors: list[FieldError],
) -> int | None:
    if value is None:
        return None
    if not MIN_YEAR <= value <= current_year + 1:
        collect(errors, "year", f"Рік має бути від {MIN_YEAR} до {current_year + 1}.")
    return value


def plan_release_type(
    value: str | None,
    stored: str | None,
    errors: list[FieldError],
) -> str | None:
    """a type a source reported outside the known ones is accepted only as it already is."""
    release_type = (value or "").strip() or None
    if release_type is None or release_type in RELEASE_TYPES or release_type == stored:
        return release_type

    collect(
        errors,
        "release_type",
        "Тип релізу: " + ", ".join(RELEASE_TYPE_NAMES.values()) + " або порожньо.",
    )
    return release_type


def plan_release_title(value: str | None) -> str | None:
    return (value or "").strip() or None


def plan_album_id(link: str | None, errors: list[FieldError]) -> str | None:
    if link is None or not link.strip():
        return None

    parsed = parse_spotify_album_link(link)
    if not parsed.is_accepted:
        collect(errors, "album_link", parsed.problem or "Некоректне посилання на альбом.")
    return parsed.album_id


def plan_description(value: str | None, errors: list[FieldError]) -> str | None:
    """the normalized text, or `None` for none; the length is counted after normalizing, and every problem of a link is
    its own sentence."""
    description = normalize_description(value)
    if description is None:
        return None

    if len(description) > MAX_DESCRIPTION_LENGTH:
        collect(errors, "description", f"Опис задовгий: {len(description)} з {MAX_DESCRIPTION_LENGTH} символів.")
    for problem in parse_description(description).problems:
        collect(errors, "description", problem)
    return description


def plan_platform_links(
    inputs: Mapping[str, str],
    stored_links: Mapping[str, object],
    stored_removed: Iterable[object],
    errors: list[FieldError],
) -> tuple[dict[str, object], list[str], bool]:
    """an unchanged link stays as stored, a new or edited one becomes a validated manual link
    and lifts the platform's removal; a stored link missing from the input is removed, except one the caption
    never shows: the editor does not list it, so its absence means «unchanged»."""
    links: dict[str, object] = {}
    for platform, raw_url in inputs.items():
        stored_entry = stored_links.get(platform)
        stored_url = as_nonempty_str(as_dict(stored_entry).get("url"))
        url = raw_url.strip()
        if stored_url is not None and url == stored_url:
            links[platform] = stored_entry
            continue

        field = f"platform_links.{platform}"
        if platform not in ALLOWED_HOSTS:
            collect(errors, field, "Невідома платформа.")
            continue
        checked = check_manual_link(platform, url)
        if checked.url is None:
            collect(errors, field, checked.problem or "Посилання не підходить.")
            continue
        links[platform] = {"url": checked.url, "source": MANUAL_SOURCE, "match": MANUAL_MATCH}

    for platform, stored_entry in stored_links.items():
        if platform not in inputs and not is_shown_in_caption(platform):
            links[platform] = stored_entry

    removed = [name for name in stored_removed if isinstance(name, str) and name not in links]
    for platform in stored_links:
        if platform not in links and platform not in removed:
            removed.append(platform)

    music_before = as_nonempty_str(as_dict(stored_links.get(YOUTUBE_MUSIC)).get("url"))
    music_after = as_nonempty_str(as_dict(links.get(YOUTUBE_MUSIC)).get("url"))
    return links, removed, music_before != music_after


def plan_manual_fields(
    stored: Iterable[object],
    changed: Iterable[str],
    reverted: Iterable[str],
    errors: list[FieldError],
) -> list[str]:
    """a field joins the list only because its value changed; «back to automatic» leaves it."""
    changed_names = set(changed)
    reverted_names = set(reverted)

    unknown = reverted_names - set(MANUAL_FIELD_NAMES)
    if unknown:
        collect(errors, "revert_to_automatic", "Невідоме поле: " + ", ".join(sorted(unknown)) + ".")
    clash = reverted_names & changed_names
    if clash:
        collect(
            errors,
            "revert_to_automatic",
            "Поле не можна змінити й повернути до автоматичного разом: " + ", ".join(sorted(clash)) + ".",
        )

    keep = {name for name in stored if isinstance(name, str)} | changed_names
    return [name for name in MANUAL_FIELD_NAMES if name in keep and name not in reverted_names]


def plan_fields(
    update: FieldsInput,
    stored: StoredFields,
    current_year: int,
) -> PlannedFields:
    """raises `FieldsInvalidError` with every problem at once, never half a plan."""
    errors: list[FieldError] = []

    title = plan_title(update.title, errors)
    artists, artist_names_changed = plan_artists(
        [(artist.name, artist.hashtag) for artist in update.artists], stored.artists, errors
    )
    languages = plan_languages(update.languages, errors)
    genres = plan_genres([(genre.name, genre.hashtag) for genre in update.genres], stored.genres, errors)
    year = plan_year(update.year, current_year, errors)
    release_type = plan_release_type(update.release_type, stored.release_type, errors)
    release_title = plan_release_title(update.release_title)
    album_id = plan_album_id(update.album_link, errors)
    description = plan_description(update.description, errors)
    links, removed, youtube_music_changed = plan_platform_links(
        update.platform_links, stored.platform_links, stored.removed_platforms, errors
    )

    changed_manual = [
        name
        for name, differs in (
            ("title", title != stored.title),
            ("artists", artist_names_changed),
            ("year", year != stored.year),
            ("release_type", release_type != stored.release_type),
            ("release_title", release_title != stored.release_title),
            ("album_id_spotify", album_id != stored.album_id_spotify),
        )
        if differs
    ]
    manual_fields = plan_manual_fields(stored.manual_fields, changed_manual, update.revert_to_automatic, errors)

    if errors:
        raise FieldsInvalidError(errors)

    changed = list(changed_manual)
    if languages != [language for language in stored.languages if isinstance(language, str)]:
        changed.append("languages")
    if genres != [entry for entry in stored.genres if isinstance(entry, dict)]:
        changed.append("genres")
    if links != stored.platform_links:
        changed.append("platform_links")
    if description != stored.description:
        changed.append("description")

    return PlannedFields(
        title=title,
        artists=artists,
        languages=languages,
        genres=genres,
        year=year,
        release_type=release_type,
        release_title=release_title,
        album_id_spotify=album_id,
        description=description,
        platform_links=links,
        manual_fields=manual_fields,
        removed_platforms=removed,
        changed_fields=tuple(changed),
        youtube_music_changed=youtube_music_changed,
    )
