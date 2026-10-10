"""The audit trail of the Mini App: every edit and archive action leaves one line with the user,
the post, what was touched and the result, and no line carries a credential.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError
from support import ADMIN_ID, BOT_TOKEN, make_init_data
from test_webapp_apply_routes import BODY, make_client
from test_webapp_applying import CHANNEL, arrange, arrange_archive, caption_of, make_post
from test_webapp_edit_routes import FORM, SAVE_BODY, database_error
from test_webapp_editing import STAMP, form, make_rows, unchanged_update
from test_webapp_posts_routes import bearer

from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.telegram.caption_edit import MessageProbe
from pisniar.webapp import api, applying, auth, editing
from pisniar.webapp import posts as posts_module
from pisniar.webapp.applying import apply_caption, archive_post
from pisniar.webapp.editing import preview_fields, save_fields
from pisniar.webapp.errors import ApiProblem, FieldsInvalidError
from pisniar.webapp.posts import LOGGED_AT_THE_ROUTE, refusals_logged
from pisniar.webapp.schemas import FieldError

USER_ID = 111
EDIT_ROUTES = [
    ("put", "/api/posts/7/fields", "save_fields", SAVE_BODY, "fields_save_refused"),
    ("post", "/api/posts/7/preview", "preview_fields", FORM, "fields_preview_refused"),
    ("post", "/api/posts/7/apply", "apply_caption", BODY, "caption_apply_refused"),
    ("post", "/api/posts/7/archive", "archive_post", None, "post_archive_refused"),
]
YOUTUBE_MUSIC_OTHER = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"


class RecordingLogger:
    """stands in for a module logger: `capture_logs()` is defeated by a logger that an earlier
    test in the same process already cached."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def record(event: str, **fields: Any) -> None:
            self.entries.append({"event": event, "log_level": level, **fields})

        return record


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorder = RecordingLogger()
    for module in (api, applying, auth, editing, posts_module):
        monkeypatch.setattr(module, "logger", recorder)
    return recorder.entries


def only(entries: list[dict[str, Any]], event: str) -> dict[str, Any]:
    matching = [entry for entry in entries if entry["event"] == event]
    assert len(matching) == 1, entries
    return matching[0]


def assert_no_credentials(entries: list[dict[str, Any]]) -> None:
    text = repr(entries)
    session_token = bearer()["Authorization"].removeprefix("Bearer ")
    for secret in (BOT_TOKEN, session_token, "init_data", "initData", "TS_AUTHKEY"):
        assert secret not in text


def assert_actor(
    entry: dict[str, Any],
    reason: str | None,
    user_id: int = USER_ID,
) -> None:
    assert (entry["user_id"], entry["post_id"]) == (user_id, 7)
    if reason is not None:
        assert entry["reason"] == reason


def fake_session() -> MagicMock:
    fake = MagicMock()
    fake.flush = AsyncMock()
    fake.refresh = AsyncMock()
    fake.get = AsyncMock()
    return fake


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> tuple[Post, Track]:
    post, track = make_rows()
    monkeypatch.setattr(editing, "lock_post_without_waiting", AsyncMock(return_value=post))
    monkeypatch.setattr(editing, "lock_track_for_write", AsyncMock(return_value=track))
    monkeypatch.setattr(editing, "count_media_posts", AsyncMock(return_value=0))
    monkeypatch.setattr(editing, "detail_of", AsyncMock(return_value=MagicMock()))
    return post, track


def test_the_recording_logger_keeps_the_event_the_level_and_the_fields() -> None:
    recorder = RecordingLogger()

    recorder.warning("something", user_id=1)

    assert recorder.entries == [{"event": "something", "log_level": "warning", "user_id": 1}]


async def test_a_save_logs_the_user_the_post_the_changed_fields_and_the_manual_ones(
    rows: tuple[Post, Track], captured: list[dict[str, Any]]
) -> None:
    _, track = rows

    await save_fields(fake_session(), 7, unchanged_update(track, title="Нова"), 2026, USER_ID)

    saved = only(captured, "fields_saved")
    assert_actor(saved, None)
    assert saved["changed_fields"] == ["title"]
    assert saved["manual_fields"] == ["title"]
    assert_no_credentials(captured)


async def test_a_save_of_a_post_that_cannot_be_edited_logs_not_editable(
    rows: tuple[Post, Track], captured: list[dict[str, Any]]
) -> None:
    post, track = rows
    post.status = PostStatus.PUBLISHING

    with pytest.raises(ApiProblem):
        await save_fields(fake_session(), 7, unchanged_update(track), 2026, USER_ID)

    assert_actor(only(captured, "fields_save_refused"), "not_editable")
    assert_no_credentials(captured)


async def test_a_stale_save_logs_stale_data(rows: tuple[Post, Track], captured: list[dict[str, Any]]) -> None:
    _, track = rows
    update = unchanged_update(track, expected_post_updated_at=STAMP.replace(year=2000))

    with pytest.raises(ApiProblem):
        await save_fields(fake_session(), 7, update, 2026, USER_ID)

    assert_actor(only(captured, "fields_save_refused"), "stale_data")


async def test_a_save_held_by_a_media_post_logs_media_in_progress(
    rows: tuple[Post, Track],
    captured: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, track = rows
    monkeypatch.setattr(editing, "count_media_posts", AsyncMock(return_value=1))
    update = unchanged_update(track, platform_links={"youtubeMusic": YOUTUBE_MUSIC_OTHER})

    with pytest.raises(ApiProblem):
        await save_fields(fake_session(), 7, update, 2026, USER_ID)

    assert_actor(only(captured, "fields_save_refused"), "media_in_progress")


def preview_session(post: Post, track: Track) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = (post, track)
    session.execute = AsyncMock(return_value=result)
    return session


async def test_a_preview_logs_the_changed_fields_and_the_length(
    captured: list[dict[str, Any]],
) -> None:
    post, track = make_rows()

    await preview_fields(preview_session(post, track), 7, form(title="Нова"), 2026, USER_ID)

    previewed = only(captured, "fields_previewed")
    assert_actor(previewed, None)
    assert previewed["changed_fields"] == ["title"]
    assert previewed["visible_length"] > 0


async def test_a_preview_of_a_post_that_cannot_be_edited_logs_not_editable(
    captured: list[dict[str, Any]],
) -> None:
    post, track = make_rows(PostStatus.PUBLISHING)

    with pytest.raises(ApiProblem):
        await preview_fields(preview_session(post, track), 7, form(), 2026, USER_ID)

    assert_actor(only(captured, "fields_preview_refused"), "not_editable")


async def test_an_apply_logs_the_targets_and_the_result(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, _ = caption_of(post, track)
    session = fake_session()
    arrange(monkeypatch, session, post, track)

    await apply_caption(session, MagicMock(), 7, sha, USER_ID, None)

    applied = only(captured, "caption_applied")
    assert_actor(applied, None)
    assert (applied["targets"], applied["result"]) == (["draft"], "edited")
    assert_no_credentials(captured)


async def test_a_stale_apply_logs_stale(captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    session = fake_session()
    arrange(monkeypatch, session, post, track)

    with pytest.raises(ApiProblem):
        await apply_caption(session, MagicMock(), 7, "0" * 64, USER_ID, None)

    assert_actor(only(captured, "caption_apply_refused"), "stale")


async def test_an_archive_logs_the_user_and_the_post(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED, channel=CHANNEL, kind="video")
    arrange_archive(monkeypatch, post, MessageProbe.MISSING)

    await archive_post(fake_session(), MagicMock(), 7, USER_ID)

    assert_actor(only(captured, "post_archived_channel_message_missing"), None)
    assert_no_credentials(captured)


async def test_a_refused_archive_logs_its_reason(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED, channel=CHANNEL, kind="video")
    arrange_archive(monkeypatch, post, MessageProbe.EXISTS)

    with pytest.raises(ApiProblem):
        await archive_post(fake_session(), MagicMock(), 7, USER_ID)

    assert_actor(only(captured, "post_archive_refused"), "message_exists")


@pytest.mark.parametrize(("method", "path", "name", "body", "event"), EDIT_ROUTES)
def test_a_missing_post_is_logged_by_the_route(
    settings: Settings,
    captured: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    name: str,
    body: dict[str, Any] | None,
    event: str,
) -> None:
    monkeypatch.setattr(posts_module, name, AsyncMock(return_value=None))
    client, _, _ = make_client(settings)

    client.request(method, path, json=body, headers=bearer())

    assert_actor(only(captured, event), "post_not_found", ADMIN_ID)
    assert_no_credentials(captured)


@pytest.mark.parametrize(("method", "path", "name", "body", "event"), EDIT_ROUTES)
def test_a_held_lock_and_a_database_failure_are_logged_by_the_route(
    settings: Settings,
    captured: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    name: str,
    body: dict[str, Any] | None,
    event: str,
) -> None:
    client, _, _ = make_client(settings)

    monkeypatch.setattr(posts_module, name, AsyncMock(side_effect=database_error("55P03")))
    client.request(method, path, json=body, headers=bearer())
    busy = captured.copy()
    captured.clear()
    monkeypatch.setattr(posts_module, name, AsyncMock(side_effect=DBAPIError("x", {}, Exception("secret-dsn"))))
    client.request(method, path, json=body, headers=bearer())

    # the preview only reads, so no lock is ever refused there
    busy_reason = "database_unavailable" if name == "preview_fields" else "post_busy"
    assert_actor(only(busy, event), busy_reason, ADMIN_ID)
    assert_actor(only(captured, event), "database_unavailable", ADMIN_ID)
    assert "secret-dsn" not in repr(captured)


@pytest.mark.parametrize(("method", "path", "name", "body", "event"), EDIT_ROUTES[2:])
def test_a_missing_telegram_client_is_logged_by_the_route(
    settings: Settings,
    captured: list[dict[str, Any]],
    method: str,
    path: str,
    name: str,
    body: dict[str, Any] | None,
    event: str,
) -> None:
    client, _, _ = make_client(settings, with_bot=False)

    client.request(method, path, json=body, headers=bearer())

    assert_actor(only(captured, event), "telegram_unavailable", ADMIN_ID)


@pytest.mark.parametrize(("method", "path", "name", "body", "event"), EDIT_ROUTES[:2])
def test_invalid_fields_are_logged_with_the_field_names_and_not_the_sentences(
    settings: Settings,
    captured: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    name: str,
    body: dict[str, Any] | None,
    event: str,
) -> None:
    errors = [FieldError(field="year", message="Рік поза межами.")]
    monkeypatch.setattr(posts_module, name, AsyncMock(side_effect=FieldsInvalidError(errors)))
    client, _, _ = make_client(settings)

    client.request(method, path, json=body, headers=bearer())

    refused = only(captured, event)
    assert_actor(refused, "invalid_fields", ADMIN_ID)
    assert refused["fields"] == ["year"]
    assert "Рік поза межами." not in repr(captured)


async def test_a_refusal_logged_deeper_is_not_logged_a_second_time(
    captured: list[dict[str, Any]],
) -> None:
    assert "not_editable" not in LOGGED_AT_THE_ROUTE

    with pytest.raises(ApiProblem):
        async with refusals_logged("fields_save_refused", USER_ID, 7):
            raise ApiProblem(409, "not_editable", "Речення.")

    assert captured == []


def test_a_session_logs_the_user_and_never_the_token_or_the_init_data(
    settings: Settings, captured: list[dict[str, Any]]
) -> None:
    client, _, _ = make_client(settings)
    init_data = make_init_data()

    response = client.post("/api/session", json={"init_data": init_data})

    assert only(captured, "webapp_session_issued")["user_id"] == ADMIN_ID
    assert response.json()["token"] not in repr(captured)
    assert init_data not in repr(captured)


def test_a_rejected_bearer_logs_the_reason_and_the_user(settings: Settings, captured: list[dict[str, Any]]) -> None:
    client, _, _ = make_client(settings)

    client.post("/api/posts/7/archive")
    client.post("/api/posts/7/archive", headers=bearer(999))

    reasons = [(entry["reason"], entry.get("user_id")) for entry in captured]
    assert reasons == [("no_bearer_token", None), ("not_admin", 999)]
    assert_no_credentials(captured)
