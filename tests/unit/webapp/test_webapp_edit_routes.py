"""The save and preview routes with the database faked: authorization before the database,
the error shapes, and the mapping of lock and database failures.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from sqlalchemy.exc import DBAPIError, OperationalError
from support import ADMIN_ID
from test_webapp_posts_routes import STAMP, bearer, make_client, make_detail

from pisniar.config import Settings
from pisniar.webapp import posts as posts_module
from pisniar.webapp.database import (
    DATABASE_UNAVAILABLE_MESSAGE,
    POST_BUSY_MESSAGE,
    is_lock_failure,
    write_to_database,
)
from pisniar.webapp.errors import ApiProblem, FieldsInvalidError
from pisniar.webapp.schemas import POST_ID_MAX, CaptionView, FieldError, PreviewResponse

STAMP_TEXT = STAMP.isoformat()
FORM: dict[str, Any] = {
    "title": "Тема",
    "artists": [{"name": "Тол", "hashtag": None}],
    "languages": ["українська"],
    "genres": [],
    "year": 2019,
    "release_type": "album",
    "release_title": "Альбом",
    "album_link": None,
    "platform_links": {},
}
SAVE_BODY = {
    **FORM,
    "expected_post_updated_at": STAMP_TEXT,
    "expected_track_updated_at": STAMP_TEXT,
}


class SqlState(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("driver error")
        self.sqlstate = sqlstate


def database_error(sqlstate: str | None) -> DBAPIError:
    return DBAPIError("UPDATE", {}, SqlState(sqlstate))


def make_preview() -> PreviewResponse:
    return PreviewResponse(
        caption=CaptionView(
            segments=[],
            visible_length=0,
            limit=1024,
            dropped_genres=[],
            dropped_platforms=[],
            release_title_dropped=False,
            over_limit=False,
            warnings=[],
            sha256="0" * 64,
        ),
        stored_caption_sha256=None,
        differs_from_stored=True,
    )


@pytest.mark.parametrize(
    ("method", "path"), [("put", "/api/posts/7/fields"), ("post", "/api/posts/7/preview")]
)
def test_an_anonymous_or_foreign_caller_is_refused_before_the_database(
    settings: Settings, method: str, path: str
) -> None:
    client, session_factory = make_client(settings)

    anonymous = client.request(method, path, json=SAVE_BODY)
    stranger = client.request(method, path, json=SAVE_BODY, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()


def test_a_save_returns_the_fresh_detail(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = AsyncMock(return_value=make_detail())
    monkeypatch.setattr(posts_module, "save_fields", save)
    client, _ = make_client(settings)

    response = client.put("/api/posts/7/fields", json=SAVE_BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json()["id"] == 7
    args = save.await_args_list[0].args
    assert (args[1], args[3], args[4]) == (7, 2027, ADMIN_ID)


def test_a_preview_returns_the_caption_and_the_difference(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = AsyncMock(return_value=make_preview())
    monkeypatch.setattr(posts_module, "preview_fields", preview)
    client, _ = make_client(settings)

    response = client.post("/api/posts/7/preview", json=FORM, headers=bearer())

    assert response.status_code == 200
    assert response.json()["differs_from_stored"] is True
    assert response.json()["caption"]["sha256"] == "0" * 64


@pytest.mark.parametrize(
    ("method", "path", "name", "body"),
    [
        ("put", "/api/posts/7/fields", "save_fields", SAVE_BODY),
        ("post", "/api/posts/7/preview", "preview_fields", FORM),
    ],
)
def test_a_missing_post_is_a_404(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    name: str,
    body: dict[str, Any],
) -> None:
    monkeypatch.setattr(posts_module, name, AsyncMock(return_value=None))
    client, _ = make_client(settings)

    response = client.request(method, path, json=body, headers=bearer())

    assert response.status_code == 404
    assert response.json()["code"] == "post_not_found"


@pytest.mark.parametrize(
    ("method", "path", "name", "body"),
    [
        ("put", "/api/posts/7/fields", "save_fields", SAVE_BODY),
        ("post", "/api/posts/7/preview", "preview_fields", FORM),
    ],
)
@pytest.mark.parametrize("code", ["not_editable", "stale_data", "media_in_progress"])
def test_a_refusal_is_a_409_with_its_own_code_and_sentence(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    name: str,
    body: dict[str, Any],
    code: str,
) -> None:
    refusal = ApiProblem(409, code, "Речення для адміна.")
    monkeypatch.setattr(posts_module, name, AsyncMock(side_effect=refusal))
    client, _ = make_client(settings)

    response = client.request(method, path, json=body, headers=bearer())

    assert response.status_code == 409
    assert response.json() == {"code": code, "message": "Речення для адміна."}


def test_invalid_fields_are_a_422_listing_every_field(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = [
        FieldError(field="year", message="Рік поза межами."),
        FieldError(field="title", message="Порожньо."),
    ]
    monkeypatch.setattr(
        posts_module, "save_fields", AsyncMock(side_effect=FieldsInvalidError(errors))
    )
    client, _ = make_client(settings)

    response = client.put("/api/posts/7/fields", json=SAVE_BODY, headers=bearer())

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_fields"
    assert body["errors"] == [
        {"field": "year", "message": "Рік поза межами."},
        {"field": "title", "message": "Порожньо."},
    ]


@pytest.mark.parametrize("post_id", ["0", "-1", "abc", str(POST_ID_MAX + 1), "9" * 30])
def test_a_bad_post_id_is_a_422_before_any_work(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, post_id: str
) -> None:
    save = AsyncMock()
    monkeypatch.setattr(posts_module, "save_fields", save)
    client, _ = make_client(settings)

    response = client.put(f"/api/posts/{post_id}/fields", json=SAVE_BODY, headers=bearer())

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    save.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": 1},
        {"title": 5},
        {"year": "2019"},
        {"year": 2019.5},
        {"artists": "Тол"},
        {"title": "т" * 301},
        {"languages": ["у"] * 11},
        {"platform_links": {"spotify": 1}},
        {"expected_post_updated_at": "not a date"},
        {"expected_track_updated_at": None},
    ],
)
def test_a_malformed_save_body_is_a_422_and_never_a_500(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, mutation: dict[str, Any]
) -> None:
    save = AsyncMock()
    monkeypatch.setattr(posts_module, "save_fields", save)
    client, _ = make_client(settings)

    response = client.put("/api/posts/7/fields", json={**SAVE_BODY, **mutation}, headers=bearer())

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "Некоректний запит."}
    save.assert_not_awaited()


@pytest.mark.parametrize("missing", ["expected_post_updated_at", "expected_track_updated_at"])
def test_a_save_without_its_versions_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    save = AsyncMock()
    monkeypatch.setattr(posts_module, "save_fields", save)
    client, _ = make_client(settings)
    body = {key: value for key, value in SAVE_BODY.items() if key != missing}

    response = client.put("/api/posts/7/fields", json=body, headers=bearer())

    assert response.status_code == 422
    save.assert_not_awaited()


def test_a_preview_does_not_need_the_versions(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posts_module, "preview_fields", AsyncMock(return_value=make_preview()))
    client, _ = make_client(settings)

    assert client.post("/api/posts/7/preview", json=FORM, headers=bearer()).status_code == 200


def test_a_lock_failure_in_a_save_is_a_409_and_another_failure_a_503(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = make_client(settings)

    monkeypatch.setattr(posts_module, "save_fields", AsyncMock(side_effect=database_error("55P03")))
    busy = client.put("/api/posts/7/fields", json=SAVE_BODY, headers=bearer())
    monkeypatch.setattr(
        posts_module,
        "save_fields",
        AsyncMock(side_effect=OperationalError("UPDATE", {}, Exception("secret-dsn"))),
    )
    down = client.put("/api/posts/7/fields", json=SAVE_BODY, headers=bearer())

    assert (busy.status_code, busy.json()["code"]) == (409, "post_busy")
    assert busy.json()["message"] == POST_BUSY_MESSAGE
    assert (down.status_code, down.json()["message"]) == (503, DATABASE_UNAVAILABLE_MESSAGE)
    assert "secret-dsn" not in down.text


def test_a_preview_database_failure_is_a_503(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posts_module, "preview_fields", AsyncMock(side_effect=OSError("refused")))
    client, _ = make_client(settings)

    response = client.post("/api/posts/7/preview", json=FORM, headers=bearer())

    assert response.status_code == 503
    assert "refused" not in response.text


def test_without_a_session_factory_both_routes_are_a_503(settings: Settings) -> None:
    client, _ = make_client(settings, with_database=False)

    assert client.put("/api/posts/7/fields", json=SAVE_BODY, headers=bearer()).status_code == 503
    assert client.post("/api/posts/7/preview", json=FORM, headers=bearer()).status_code == 503


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [("55P03", True), ("57014", True), ("23505", False), ("40001", False), (None, False)],
)
def test_only_the_two_lock_sqlstates_count_as_a_lock_failure(
    sqlstate: str | None, expected: bool
) -> None:
    assert is_lock_failure(database_error(sqlstate)) is expected


def test_an_error_without_a_driver_exception_is_not_a_lock_failure() -> None:
    assert is_lock_failure(DBAPIError("SELECT", {}, ValueError("plain"))) is False


class RecordingSession:
    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self) -> RecordingSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def make_request(session: RecordingSession | None) -> Request:
    request = MagicMock()
    request.app.state.session_factory = None if session is None else MagicMock(return_value=session)
    return request


async def test_a_write_is_committed_after_it_returns() -> None:
    session = RecordingSession()

    async def write(opened: Any) -> str:
        assert session.committed is False
        return "done"

    assert await write_to_database(make_request(session), write) == "done"
    assert session.committed is True


async def test_a_write_that_raises_is_never_committed() -> None:
    session = RecordingSession()

    async def write(opened: Any) -> None:
        raise ApiProblem(409, "stale_data", "x")

    with pytest.raises(ApiProblem):
        await write_to_database(make_request(session), write)

    assert session.committed is False


async def test_a_lock_failure_while_writing_is_a_409() -> None:
    async def write(opened: Any) -> None:
        raise database_error("57014")

    with pytest.raises(ApiProblem) as raised:
        await write_to_database(make_request(RecordingSession()), write)

    assert (raised.value.status_code, raised.value.code) == (409, "post_busy")


@pytest.mark.parametrize(
    "error", [database_error("23505"), OperationalError("x", {}, Exception()), OSError("down")]
)
async def test_any_other_failure_while_writing_is_a_503(error: Exception) -> None:
    async def write(opened: Any) -> None:
        raise error

    with pytest.raises(ApiProblem) as raised:
        await write_to_database(make_request(RecordingSession()), write)

    assert (raised.value.status_code, raised.value.code) == (503, "database_unavailable")


async def test_a_write_without_a_session_factory_is_a_503() -> None:
    async def write(opened: Any) -> None:
        raise AssertionError("must not run")

    with pytest.raises(ApiProblem) as raised:
        await write_to_database(make_request(None), write)

    assert raised.value.status_code == 503
