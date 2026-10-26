"""Saving and previewing an edit with the rows and the locks faked: the order of the checks,
what is written, and what is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.webapp import editing
from pisniar.webapp.editing import (
    assign_if_changed,
    count_media_posts,
    preview_fields,
    refuse_stale,
    refuse_unless_editable,
    save_fields,
    stored_fields_of,
)
from pisniar.webapp.errors import ApiProblem, FieldsInvalidError
from pisniar.webapp.fields import plan_fields
from pisniar.webapp.schemas import ArtistInput, FieldsInput, FieldsUpdate, PostDetail

STAMP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"
MUSIC_URL = "https://music.youtube.com/watch?v=n4bPzdIs_nc"
SPOTIFY_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"


def make_rows(status: PostStatus = PostStatus.RESOLVED) -> tuple[Post, Track]:
    track = Track(
        id=3,
        title="Тема",
        artists=[{"name": "Тол", "hashtag": None, "spotify_id": "a1"}],
        languages=["українська"],
        year=2019,
        release_type="album",
        release_title="Альбом",
        album_id_spotify=ALBUM_ID,
        platform_links={
            "spotify": {"url": SPOTIFY_URL, "source": "spotify", "match": "exact_isrc"},
            "youtubeMusic": {"url": MUSIC_URL, "source": "ytmusic", "match": "confirmed"},
        },
        manual_fields=[],
        removed_platforms=[],
    )
    track.updated_at = STAMP
    post = Post(id=7, track_id=3, status=status, genres=[], caption=None, failed_from_status=None)
    post.updated_at = STAMP
    return post, track


def unchanged_update(track: Track, **overrides: Any) -> FieldsUpdate:
    values: dict[str, Any] = {
        "title": track.title,
        "artists": [ArtistInput(name="Тол")],
        "languages": ["українська"],
        "genres": [],
        "year": track.year,
        "release_type": track.release_type,
        "release_title": track.release_title,
        "album_link": f"https://open.spotify.com/album/{ALBUM_ID}",
        "description": None,
        "platform_links": {"spotify": SPOTIFY_URL, "youtubeMusic": MUSIC_URL},
        "expected_post_updated_at": STAMP,
        "expected_track_updated_at": STAMP,
    }
    values.update(overrides)
    return FieldsUpdate(**values)


@pytest.fixture
def session() -> MagicMock:
    fake = MagicMock()
    fake.flush = AsyncMock()
    fake.refresh = AsyncMock()
    return fake


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    post, track = make_rows()
    mocks = {
        "post": AsyncMock(return_value=post),
        "track": AsyncMock(return_value=track),
        "media": AsyncMock(return_value=0),
        "detail": AsyncMock(return_value=MagicMock(spec=PostDetail)),
    }
    monkeypatch.setattr(editing, "lock_post_without_waiting", mocks["post"])
    monkeypatch.setattr(editing, "lock_track_for_write", mocks["track"])
    monkeypatch.setattr(editing, "count_media_posts", mocks["media"])
    monkeypatch.setattr(editing, "detail_of", mocks["detail"])
    return mocks


def test_a_value_equal_to_the_stored_one_is_not_assigned() -> None:
    _, track = make_rows()

    assert assign_if_changed(track, "title", "Тема") is False
    assert assign_if_changed(track, "title", "Інша") is True
    assert track.title == "Інша"


def test_a_save_without_changes_assigns_nothing() -> None:
    post, track = make_rows()
    artists, links = track.artists, track.platform_links
    planned = plan_fields(unchanged_update(track), stored_fields_of(post, track), 2026)

    editing.apply_plan(post, track, planned)

    assert planned.changed_fields == ()
    assert track.artists is artists
    assert track.platform_links is links


def test_stored_fields_are_read_from_both_rows() -> None:
    post, track = make_rows()
    post.genres = [{"name": "рок", "hashtag": None, "source": "deezer"}]

    stored = stored_fields_of(post, track)

    assert stored.title == "Тема"
    assert stored.genres == post.genres
    assert stored.platform_links is track.platform_links


def test_apply_plan_writes_every_planned_value_to_its_row() -> None:
    post, track = make_rows()
    update = unchanged_update(track, title="Нова", year=2000)
    planned = plan_fields(update, stored_fields_of(post, track), 2026)

    editing.apply_plan(post, track, planned)

    assert (track.title, track.year) == ("Нова", 2000)
    assert track.manual_fields == ["title", "year"]


@pytest.mark.parametrize("status", [PostStatus.RESOLVED, PostStatus.RENDERED])
def test_an_editable_post_passes_the_gate(status: PostStatus) -> None:
    post, _ = make_rows(status)

    refuse_unless_editable(post, 1, "event")


@pytest.mark.parametrize("status", [PostStatus.RESOLVING, PostStatus.PUBLISHING, PostStatus.ARCHIVED])
def test_a_post_that_cannot_be_edited_gets_a_409_with_a_sentence_without_the_state(
    status: PostStatus,
) -> None:
    post, _ = make_rows(status)

    with pytest.raises(ApiProblem) as raised:
        refuse_unless_editable(post, 1, "event")

    assert (raised.value.status_code, raised.value.code) == (409, "not_editable")
    assert status.value not in raised.value.message


def test_a_stale_refusal_is_a_409_stale_data() -> None:
    with pytest.raises(ApiProblem) as raised:
        refuse_stale(1, 7)

    assert (raised.value.status_code, raised.value.code) == (409, "stale_data")


async def test_counting_media_posts_returns_zero_for_none(session: MagicMock) -> None:
    session.scalar = AsyncMock(return_value=None)

    assert await count_media_posts(session, 3) == 0

    session.scalar = AsyncMock(return_value=2)
    assert await count_media_posts(session, 3) == 2


async def test_a_save_writes_flushes_and_returns_the_detail(session: MagicMock, locks: dict[str, AsyncMock]) -> None:
    track = locks["track"].return_value
    update = unchanged_update(track, title="Нова")

    result = await save_fields(session, 7, update, 2026, 111)

    assert result is locks["detail"].return_value
    assert track.title == "Нова"
    assert track.manual_fields == ["title"]
    session.flush.assert_awaited_once()
    assert session.refresh.await_count == 2


async def test_a_save_writes_the_description_to_the_post(session: MagicMock, locks: dict[str, AsyncMock]) -> None:
    track = locks["track"].return_value
    post = locks["post"].return_value

    await save_fields(session, 7, unchanged_update(track, description="Текст\r\n[Кліп](https://youtu.be/x)"), 2026, 111)

    assert post.description == "Текст\n[Кліп](https://youtu.be/x)"


async def test_a_save_without_a_description_change_leaves_the_post_alone(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    track = locks["track"].return_value
    post = locks["post"].return_value
    post.description = "Той самий"

    await save_fields(session, 7, unchanged_update(track, description="Той самий"), 2026, 111)

    assert post.description == "Той самий"
    assert (
        "description"
        not in editing.plan_fields(
            unchanged_update(track, description="Той самий"), stored_fields_of(post, track), 2026
        ).changed_fields
    )


async def test_a_save_whose_description_keeps_the_caption_over_the_limit_is_a_422_on_description(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    track = locks["track"].return_value
    post = locks["post"].return_value
    track.title = "Т" * 300
    track.release_title = "Р" * 300
    track.artists = [{"name": "А" * 200, "hashtag": None, "spotify_id": None}]

    with pytest.raises(FieldsInvalidError) as raised:
        await save_fields(
            session,
            7,
            unchanged_update(track, title=track.title, artists=[ArtistInput(name="А" * 200)], description="в" * 500),
            2026,
            111,
        )

    [error] = raised.value.errors
    assert error.field == "description"
    assert "Скоротіть опис щонайменше на" in error.message
    assert post.description is None
    session.flush.assert_not_awaited()


async def test_a_caption_over_the_limit_without_any_description_is_not_blamed_on_it(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    track = locks["track"].return_value
    title = "Т" * 300
    artists = [ArtistInput(name="А" * 200) for _ in range(3)]

    await save_fields(session, 7, unchanged_update(track, title=title, artists=artists, description="Текст"), 2026, 111)

    session.flush.assert_awaited_once()


async def test_a_preview_shows_the_description_in_the_caption() -> None:
    post, track = make_rows()

    preview = await preview_fields(
        preview_session(post, track), 7, form(description="Опис [Кліп](https://youtu.be/x)"), 2026, 111
    )

    assert preview is not None
    kinds = [(segment.kind, segment.text) for segment in preview.caption.segments]
    assert ("text", "Опис ") in kinds
    assert ("link", "Кліп") in kinds


async def test_a_preview_names_the_destination_hosts_of_the_links() -> None:
    post, track = make_rows()
    text = "[Квиток](https://tickets.example/a) [spotify.com](https://evil.example/x)"

    preview = await preview_fields(preview_session(post, track), 7, form(description=text), 2026, 111)

    assert preview is not None
    assert [(link.label, link.host) for link in preview.description_links] == [
        ("Квиток", "tickets.example"),
        ("spotify.com", "evil.example"),
    ]
    assert preview.description_links[1].warnings == ["текст посилання показує інший сайт"]
    assert preview.description_over_limit is False


async def test_a_preview_blames_the_description_only_when_the_caption_is_long_because_of_it() -> None:
    post, track = make_rows()
    long_fields: dict[str, Any] = {
        "title": "Т" * 300,
        "artists": [ArtistInput(name="А" * 200)],
        "release_title": "Р" * 300,
    }

    blamed = await preview_fields(
        preview_session(post, track), 7, form(description="в" * 500, **long_fields), 2026, 111
    )
    already_long = await preview_fields(
        preview_session(post, track),
        7,
        form(description="в", title="Т" * 300, artists=[ArtistInput(name="А" * 200) for _ in range(3)]),
        2026,
        111,
    )

    assert blamed is not None
    assert (blamed.caption.over_limit, blamed.description_over_limit) == (True, True)
    assert already_long is not None
    assert (already_long.caption.over_limit, already_long.description_over_limit) == (True, False)


async def test_a_missing_post_is_none_and_the_track_is_never_locked(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    locks["post"].return_value = None
    _, track = make_rows()

    assert await save_fields(session, 7, unchanged_update(track), 2026, 111) is None
    locks["track"].assert_not_awaited()


async def test_a_missing_track_is_none(session: MagicMock, locks: dict[str, AsyncMock]) -> None:
    locks["track"].return_value = None
    _, track = make_rows()

    assert await save_fields(session, 7, unchanged_update(track), 2026, 111) is None
    session.flush.assert_not_awaited()


async def test_a_non_editable_post_is_refused_before_the_track_is_locked(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    locks["post"].return_value.status = PostStatus.PUBLISHING
    _, track = make_rows()

    with pytest.raises(ApiProblem) as raised:
        await save_fields(session, 7, unchanged_update(track), 2026, 111)

    assert raised.value.code == "not_editable"
    locks["track"].assert_not_awaited()


async def test_a_stale_post_stamp_is_refused_before_the_track_is_locked(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    _, track = make_rows()
    update = unchanged_update(track, expected_post_updated_at=STAMP - timedelta(seconds=1))

    with pytest.raises(ApiProblem) as raised:
        await save_fields(session, 7, update, 2026, 111)

    assert raised.value.code == "stale_data"
    locks["track"].assert_not_awaited()
    session.flush.assert_not_awaited()


async def test_a_stale_track_stamp_writes_nothing(session: MagicMock, locks: dict[str, AsyncMock]) -> None:
    track = locks["track"].return_value
    update = unchanged_update(track, title="Нова", expected_track_updated_at=STAMP + timedelta(1))

    with pytest.raises(ApiProblem) as raised:
        await save_fields(session, 7, update, 2026, 111)

    assert raised.value.code == "stale_data"
    assert track.title == "Тема"
    session.flush.assert_not_awaited()


async def test_invalid_fields_write_nothing(session: MagicMock, locks: dict[str, AsyncMock]) -> None:
    track = locks["track"].return_value

    with pytest.raises(FieldsInvalidError):
        await save_fields(session, 7, unchanged_update(track, title="  ", year=1000), 2026, 111)

    assert track.title == "Тема"
    session.flush.assert_not_awaited()


async def test_changing_the_youtube_music_link_is_refused_while_a_media_post_exists(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    track = locks["track"].return_value
    locks["media"].return_value = 1
    update = unchanged_update(
        track,
        platform_links={
            "spotify": SPOTIFY_URL,
            "youtubeMusic": "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        },
    )

    with pytest.raises(ApiProblem) as raised:
        await save_fields(session, 7, update, 2026, 111)

    assert (raised.value.status_code, raised.value.code) == (409, "media_in_progress")
    assert track.platform_links["youtubeMusic"]["url"] == MUSIC_URL
    session.flush.assert_not_awaited()


async def test_other_links_and_fields_are_not_held_by_a_media_post(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    track = locks["track"].return_value
    locks["media"].return_value = 1
    update = unchanged_update(track, title="Нова", platform_links={"youtubeMusic": MUSIC_URL})

    await save_fields(session, 7, update, 2026, 111)

    assert track.title == "Нова"
    assert "spotify" not in track.platform_links
    locks["media"].assert_not_awaited()


async def test_the_youtube_music_guard_is_not_asked_when_the_link_is_unchanged(
    session: MagicMock, locks: dict[str, AsyncMock]
) -> None:
    track = locks["track"].return_value

    await save_fields(session, 7, unchanged_update(track), 2026, 111)

    locks["media"].assert_not_awaited()


def form(track: Track | None = None, **overrides: Any) -> FieldsInput:
    values: dict[str, Any] = {
        "title": "Тема",
        "artists": [ArtistInput(name="Тол")],
        "languages": ["українська"],
        "genres": [],
        "year": 2019,
        "release_type": "album",
        "release_title": "Альбом",
        "album_link": f"https://open.spotify.com/album/{ALBUM_ID}",
        "description": None,
        "platform_links": {"spotify": SPOTIFY_URL, "youtubeMusic": MUSIC_URL},
    }
    values.update(overrides)
    return FieldsInput(**values)


def preview_session(post: Post, track: Track) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = (post, track)
    session.execute = AsyncMock(return_value=result)
    return session


async def test_a_preview_of_a_missing_post_is_none() -> None:
    session = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    assert await preview_fields(session, 7, form(), 2026, 111) is None


async def test_a_preview_builds_the_caption_and_compares_it_with_the_stored_one() -> None:
    post, track = make_rows()
    session = preview_session(post, track)

    preview = await preview_fields(session, 7, form(title="Нова назва"), 2026, 111)

    assert preview is not None
    assert "Нова назва" in "".join(segment.text or "" for segment in preview.caption.segments)
    assert preview.stored_caption_sha256 is None
    assert preview.differs_from_stored is True
    session.add.assert_not_called()
    session.flush.assert_not_called()


async def test_a_preview_equal_to_the_stored_caption_does_not_differ() -> None:
    post, track = make_rows()
    view, html = editing.caption_view_of(
        title="Тема",
        artists=track.artists,
        year=2019,
        release_type="album",
        release_title="Альбом",
        album_id_spotify=ALBUM_ID,
        languages=["українська"],
        genres=[],
        platform_links=track.platform_links,
        description=None,
    )
    post.caption = html

    preview = await preview_fields(preview_session(post, track), 7, form(), 2026, 111)

    assert preview is not None
    assert preview.differs_from_stored is False
    assert preview.stored_caption_sha256 == view.sha256 == preview.caption.sha256


async def test_a_preview_of_a_post_that_cannot_be_edited_is_refused() -> None:
    post, track = make_rows(PostStatus.PUBLISHING)

    with pytest.raises(ApiProblem) as raised:
        await preview_fields(preview_session(post, track), 7, form(), 2026, 111)

    assert raised.value.code == "not_editable"


async def test_a_preview_of_invalid_fields_raises_the_field_errors() -> None:
    post, track = make_rows()

    with pytest.raises(FieldsInvalidError):
        await preview_fields(preview_session(post, track), 7, form(title=" "), 2026, 111)
