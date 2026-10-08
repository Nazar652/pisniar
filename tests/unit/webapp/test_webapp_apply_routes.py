"""The apply and archive routes with the database and Telegram faked: authorization first,
the error shapes, and the edits that follow the commit.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from support import ADMIN_ID, NOW
from test_webapp_edit_routes import database_error
from test_webapp_posts_routes import FakeSession, bearer

from pisniar.config import Settings
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.applying import AppliedCaption, EditTarget
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import POST_ID_MAX, ApplyResponse, ArchiveResponse

URL = "https://pisniar.example.ts.net"
SHA = "a" * 64
BODY = {"expected_caption_sha256": SHA}
ROUTES = [
    ("/api/posts/7/apply", "apply_caption", BODY),
    ("/api/posts/7/archive", "archive_post", None),
]


def make_client(
    settings: Settings, with_bot: bool = True
) -> tuple[TestClient, MagicMock, MagicMock]:
    session_factory = MagicMock(return_value=FakeSession())
    bot = MagicMock()
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        bot=bot if with_bot else None,
    )
    return TestClient(app), session_factory, bot


def applied(*remaining: str) -> AppliedCaption:
    response = ApplyResponse(caption_sha256=SHA, result="edited", other_messages_failed=[])
    targets = [EditTarget(role, 5, 100, "text", None) for role in remaining]
    return AppliedCaption(response=response, html="<b>x</b>", remaining=targets)


@pytest.mark.parametrize(("path", "name", "body"), ROUTES)
def test_an_anonymous_or_foreign_caller_is_refused_before_the_database_and_telegram(
    settings: Settings, path: str, name: str, body: dict[str, str] | None
) -> None:
    client, session_factory, bot = make_client(settings)

    anonymous = client.post(path, json=body)
    stranger = client.post(path, json=body, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()
    assert bot.mock_calls == []


def test_an_apply_returns_the_result_and_the_failed_extra_messages(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply = AsyncMock(return_value=applied("draft"))
    monkeypatch.setattr(posts_module, "apply_caption", apply)
    remaining = AsyncMock(
        return_value=ApplyResponse(
            caption_sha256=SHA, result="edited", other_messages_failed=["draft"]
        )
    )
    monkeypatch.setattr(posts_module, "edit_remaining", remaining)
    client, _, bot = make_client(settings)

    response = client.post("/api/posts/7/apply", json=BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json() == {
        "caption_sha256": SHA,
        "result": "edited",
        "other_messages_failed": ["draft"],
    }
    args = apply.await_args_list[0].args
    assert (args[1], args[2], args[3], args[4]) == (bot, 7, SHA, ADMIN_ID)
    assert remaining.await_args_list[0].args[0] is bot


def test_the_extra_messages_are_edited_only_after_the_database_work(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    async def apply(*args: Any) -> AppliedCaption:
        order.append("apply")
        return applied("draft")

    async def remaining(bot: Any, result: AppliedCaption, *args: Any) -> ApplyResponse:
        order.append("remaining")
        return result.response

    monkeypatch.setattr(posts_module, "apply_caption", apply)
    monkeypatch.setattr(posts_module, "edit_remaining", remaining)
    client, _, _ = make_client(settings)

    client.post("/api/posts/7/apply", json=BODY, headers=bearer())

    assert order == ["apply", "remaining"]


def test_a_refused_apply_never_edits_the_extra_messages(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = ApiProblem(409, "caption_changed", "Підпис змінився.")
    monkeypatch.setattr(posts_module, "apply_caption", AsyncMock(side_effect=problem))
    remaining = AsyncMock()
    monkeypatch.setattr(posts_module, "edit_remaining", remaining)
    client, _, _ = make_client(settings)

    response = client.post("/api/posts/7/apply", json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (409, "caption_changed")
    remaining.assert_not_awaited()


def test_an_archive_returns_the_new_label(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ArchiveResponse(post_id=7, status_label="Архівовано")
    archive = AsyncMock(return_value=result)
    monkeypatch.setattr(posts_module, "archive_post", archive)
    client, _, bot = make_client(settings)

    response = client.post("/api/posts/7/archive", headers=bearer())

    assert response.status_code == 200
    assert response.json() == {"post_id": 7, "status_label": "Архівовано"}
    assert archive.await_args_list[0].args[1:] == (bot, 7, ADMIN_ID)


@pytest.mark.parametrize(("path", "name", "body"), ROUTES)
def test_a_missing_post_is_a_404(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    name: str,
    body: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(posts_module, name, AsyncMock(return_value=None))
    client, _, _ = make_client(settings)

    response = client.post(path, json=body, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")


@pytest.mark.parametrize(("path", "name", "body"), ROUTES)
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (409, "not_editable"),
        (409, "channel_message_missing"),
        (422, "caption_too_long"),
        (429, "telegram_rate_limited"),
        (502, "telegram_failed"),
    ],
)
def test_a_refusal_keeps_its_status_code_and_sentence(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    name: str,
    body: dict[str, str] | None,
    status: int,
    code: str,
) -> None:
    monkeypatch.setattr(
        posts_module, name, AsyncMock(side_effect=ApiProblem(status, code, "Речення."))
    )
    client, _, _ = make_client(settings)

    response = client.post(path, json=body, headers=bearer())

    assert response.status_code == status
    assert response.json() == {"code": code, "message": "Речення."}


@pytest.mark.parametrize(("path", "name", "body"), ROUTES)
def test_a_held_lock_is_a_409_and_another_database_failure_a_503(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    name: str,
    body: dict[str, str] | None,
) -> None:
    client, _, _ = make_client(settings)

    monkeypatch.setattr(posts_module, name, AsyncMock(side_effect=database_error("55P03")))
    busy = client.post(path, json=body, headers=bearer())
    monkeypatch.setattr(
        posts_module, name, AsyncMock(side_effect=DBAPIError("x", {}, Exception("secret-dsn")))
    )
    down = client.post(path, json=body, headers=bearer())

    assert (busy.status_code, busy.json()["code"]) == (409, "post_busy")
    assert (down.status_code, down.json()["code"]) == (503, "database_unavailable")
    assert "secret-dsn" not in down.text


@pytest.mark.parametrize(("path", "name", "body"), ROUTES)
def test_without_a_telegram_client_the_routes_are_a_503_before_any_work(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    name: str,
    body: dict[str, str] | None,
) -> None:
    work = AsyncMock()
    monkeypatch.setattr(posts_module, name, work)
    client, session_factory, _ = make_client(settings, with_bot=False)

    response = client.post(path, json=body, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, "telegram_unavailable")
    work.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expected_caption_sha256": "short"},
        {"expected_caption_sha256": "A" * 64},
        {"expected_caption_sha256": "g" * 64},
        {"expected_caption_sha256": SHA, "extra": 1},
        {"expected_caption_sha256": 5},
        {"expected_caption_sha256": None},
    ],
)
def test_a_malformed_apply_body_is_a_422_and_never_a_500(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    apply = AsyncMock()
    monkeypatch.setattr(posts_module, "apply_caption", apply)
    client, _, _ = make_client(settings)

    response = client.post("/api/posts/7/apply", json=body, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    apply.assert_not_awaited()


@pytest.mark.parametrize("post_id", ["0", "-1", "abc", str(POST_ID_MAX + 1), "9" * 30])
@pytest.mark.parametrize("path", ["apply", "archive"])
def test_a_bad_post_id_is_a_422_before_any_work(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, post_id: str, path: str
) -> None:
    work = AsyncMock()
    monkeypatch.setattr(posts_module, "apply_caption", work)
    monkeypatch.setattr(posts_module, "archive_post", work)
    client, _, _ = make_client(settings)

    response = client.post(f"/api/posts/{post_id}/{path}", json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    work.assert_not_awaited()


def test_the_application_keeps_the_telegram_client_and_the_public_url(settings: Settings) -> None:
    bot = MagicMock()
    configured = settings.model_copy(update={"webapp_public_url": URL})

    app = create_webapp(configured, bot=bot)

    assert app.state.bot is bot
    assert app.state.webapp_public_url == URL
