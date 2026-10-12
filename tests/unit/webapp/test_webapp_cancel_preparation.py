"""Giving up a prepared audio: the locked database part with its refusals, the draft's keyboard
that comes back after the commit, and the route that orders them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from support import ADMIN_ID, NOW
from test_webapp_edit_routes import database_error
from test_webapp_fragment_choice import RecordingLogger
from test_webapp_posts_routes import FakeSession, bearer

from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.webapp import audio_preparation
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.audio_preparation import (
    CancelledPreparation,
    cancel_preparation,
    draft_of,
    restore_draft,
)
from pisniar.webapp.editing import STALE_DATA_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import CancelPreparationRequest
from pisniar.webapp.status import NOT_CANCELLABLE_REASON, RENDER_STARTED_REASON

POST_ID = 7
DRAFT = (555, 41)
URL = "https://pisniar.example.ts.net"
PATH = f"/api/posts/{POST_ID}/fragment/cancel"
CANCELLED = CancelledPreparation(POST_ID, DRAFT)
STAMP = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
REQUEST = CancelPreparationRequest(expected_post_updated_at=STAMP)
BODY = {"expected_post_updated_at": STAMP.isoformat()}


def make_post(
    status: PostStatus = PostStatus.EDITING,
    *,
    draft: tuple[int, int] | None = DRAFT,
    preview_message_id: int | None = None,
) -> Post:
    return Post(
        id=POST_ID,
        track_id=3,
        status=status,
        updated_at=STAMP,
        draft_chat_id=None if draft is None else draft[0],
        draft_message_id=None if draft is None else draft[1],
        preview_message_id=preview_message_id,
    )


@dataclass
class Locks:
    post: Post | None = field(default_factory=make_post)
    track: Track | None = field(default_factory=lambda: Track(id=3))
    order: list[str] = field(default_factory=list)


@pytest.fixture
def logger(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(audio_preparation, "logger", recorder)
    return recorder


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> Locks:
    held = Locks()

    async def lock_post(session: object, post_id: int) -> Post | None:
        held.order.append("post")
        return held.post

    async def lock_track(session: object, track_id: int) -> Track | None:
        held.order.append("track")
        return held.track

    monkeypatch.setattr(audio_preparation, "lock_post_without_waiting", lock_post)
    monkeypatch.setattr(audio_preparation, "lock_track_for_write", lock_track)
    return held


def test_the_draft_reference_is_both_ids_or_nothing() -> None:
    assert draft_of(make_post()) == DRAFT
    assert draft_of(make_post(draft=None)) is None
    assert draft_of(Post(id=1, draft_chat_id=5)) is None


async def test_prepared_audio_goes_back_to_a_draft_under_both_locks(locks: Locks, logger: RecordingLogger) -> None:
    cancelled = await cancel_preparation(MagicMock(), POST_ID, REQUEST, ADMIN_ID)

    assert cancelled == CANCELLED
    assert locks.post is not None
    assert locks.post.status is PostStatus.RESOLVED
    assert locks.order == ["post", "track"]
    assert logger.entries == []


async def test_an_older_stamp_is_stale_and_changes_nothing(locks: Locks, logger: RecordingLogger) -> None:
    stale = CancelPreparationRequest(expected_post_updated_at=STAMP - timedelta(seconds=1))

    with pytest.raises(ApiProblem) as refusal:
        await cancel_preparation(MagicMock(), POST_ID, stale, ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "stale_data")
    assert refusal.value.message == STALE_DATA_MESSAGE
    assert locks.order == ["post"]
    assert locks.post is not None
    assert locks.post.status is PostStatus.EDITING
    assert [(entry["event"], entry["reason"]) for entry in logger.entries] == [
        ("audio_preparation_cancel_refused", "stale_data")
    ]


def test_the_request_refuses_extra_fields_and_a_missing_stamp() -> None:
    with pytest.raises(ValidationError):
        CancelPreparationRequest.model_validate({"expected_post_updated_at": STAMP, "x": 1})
    with pytest.raises(ValidationError):
        CancelPreparationRequest.model_validate({})


@pytest.mark.parametrize("missing", ["post", "track"])
async def test_a_missing_row_is_none_and_the_status_stays(locks: Locks, missing: str) -> None:
    setattr(locks, missing, None)

    assert await cancel_preparation(MagicMock(), POST_ID, REQUEST, ADMIN_ID) is None
    if locks.post is not None:
        assert locks.post.status is PostStatus.EDITING


@pytest.mark.parametrize(
    ("status", "preview", "message"),
    [
        (PostStatus.RESOLVED, None, NOT_CANCELLABLE_REASON),
        (PostStatus.DOWNLOADING, None, NOT_CANCELLABLE_REASON),
        (PostStatus.PUBLISHED, None, NOT_CANCELLABLE_REASON),
        (PostStatus.DOWNLOADED, None, RENDER_STARTED_REASON),
        (PostStatus.RENDERING, None, RENDER_STARTED_REASON),
        (PostStatus.RENDERED, 99, RENDER_STARTED_REASON),
    ],
)
async def test_anything_but_prepared_audio_is_409_with_a_sentence_and_nothing_changes(
    locks: Locks,
    logger: RecordingLogger,
    status: PostStatus,
    preview: int | None,
    message: str,
) -> None:
    locks.post = make_post(status, preview_message_id=preview)

    with pytest.raises(ApiProblem) as refusal:
        await cancel_preparation(MagicMock(), POST_ID, REQUEST, ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "not_cancellable")
    assert refusal.value.message == message
    assert status.value not in message.lower()
    assert locks.order == ["post"]
    assert locks.post.status is status
    assert logger.entries == [
        {
            "event": "audio_preparation_cancel_refused",
            "log_level": "warning",
            "reason": "not_cancellable",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
        }
    ]


async def test_the_draft_gets_the_buttons_of_a_draft_with_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = AsyncMock()
    monkeypatch.setattr(audio_preparation, "restore_draft_keyboard", restore)
    bot = MagicMock()

    await restore_draft(bot, CANCELLED, URL, ADMIN_ID)

    restore.assert_awaited_once()
    arguments = restore.await_args
    assert arguments is not None
    assert arguments.args[:4] == (bot, *DRAFT, POST_ID)
    assert arguments.kwargs == {"request_timeout": 15}
    texts = [button.text for row in arguments.args[4].inline_keyboard for button in row]
    assert texts == ["Зробити відео", "Редагувати"]


async def test_without_a_draft_nothing_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    restore = AsyncMock()
    monkeypatch.setattr(audio_preparation, "restore_draft_keyboard", restore)

    await restore_draft(MagicMock(), CancelledPreparation(POST_ID, None), URL, ADMIN_ID)

    restore.assert_not_awaited()


async def test_a_failed_restore_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    monkeypatch.setattr(audio_preparation, "restore_draft_keyboard", AsyncMock(side_effect=TimeoutError("slow")))

    await restore_draft(MagicMock(), CANCELLED, URL, ADMIN_ID)

    assert [(entry["event"], entry["post_id"]) for entry in logger.entries] == [
        ("draft_keyboard_restore_failed", POST_ID)
    ]


class Journal:
    """one ordered record of the commit and the keyboard, plus what Telegram and the broker were
    asked while the transaction was open."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.asked_inside_transaction: list[object] = []
        self.bot = MagicMock()
        self.celery_client = MagicMock()
        self.cancelled: CancelledPreparation | None = CANCELLED
        self.error: Exception | None = None

    async def cancel(
        self,
        session: object,
        post_id: int,
        request: object,
        user_id: int,
    ) -> Any:
        self.order.append("cancel")
        self.asked_inside_transaction += [*self.bot.mock_calls, *self.celery_client.mock_calls]
        if self.error is not None:
            raise self.error
        return self.cancelled

    async def restore(
        self,
        bot: object,
        cancelled: object,
        url: object,
        user_id: int,
    ) -> None:
        self.order.append("keyboard")


class JournalSession(FakeSession):
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    async def commit(self) -> None:
        self.journal.order.append("commit")


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Journal:
    recorded = Journal()
    monkeypatch.setattr(posts_module, "cancel_preparation", recorded.cancel)
    monkeypatch.setattr(posts_module, "restore_draft", recorded.restore)
    return recorded


def make_client(
    settings: Settings,
    recorded: Journal,
    *,
    with_bot: bool = True,
) -> tuple[TestClient, MagicMock]:
    session_factory = MagicMock(side_effect=lambda: JournalSession(recorded))
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        bot=recorded.bot if with_bot else None,
        celery_client=recorded.celery_client,
    )
    return TestClient(app), session_factory


def test_an_anonymous_or_foreign_caller_is_refused_before_the_database(settings: Settings, journal: Journal) -> None:
    client, session_factory = make_client(settings, journal)

    anonymous = client.post(PATH, json=BODY)
    stranger = client.post(PATH, json=BODY, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()
    assert journal.order == []


def test_the_database_commits_then_the_draft_keyboard_comes_back_and_no_task_is_queued(
    settings: Settings, journal: Journal
) -> None:
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json() == {"post_id": POST_ID, "status_label": "Чернетка готова"}
    assert journal.order == ["cancel", "commit", "keyboard"]
    assert journal.asked_inside_transaction == []
    assert journal.celery_client.mock_calls == []


def test_a_body_without_the_stamp_or_with_extra_fields_is_422_before_the_database(
    settings: Settings, journal: Journal
) -> None:
    client, session_factory = make_client(settings, journal)

    missing = client.post(PATH, json={}, headers=bearer())
    extra = client.post(PATH, json={**BODY, "x": 1}, headers=bearer())

    assert (missing.status_code, extra.status_code) == (422, 422)
    session_factory.assert_not_called()


def test_the_cancellation_is_logged_once_and_only_after_the_commit(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    seen_at_commit: list[int] = []

    async def commit(self: JournalSession) -> None:
        seen_at_commit.append(len(recorder.entries))
        self.journal.order.append("commit")

    monkeypatch.setattr(JournalSession, "commit", commit)
    client, _ = make_client(settings, journal)

    client.post(PATH, json=BODY, headers=bearer())

    assert seen_at_commit == [1]
    assert [entry["event"] for entry in recorder.entries] == [
        "audio_preparation_cancel_requested",
        "audio_preparation_cancelled",
    ]


def test_a_refused_cancellation_is_not_logged_as_cancelled(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    journal.error = ApiProblem(409, "stale_data", "x")
    client, _ = make_client(settings, journal)

    client.post(PATH, json=BODY, headers=bearer())

    assert "audio_preparation_cancelled" not in [entry["event"] for entry in recorder.entries]


def test_a_missing_post_is_404_and_no_keyboard_is_touched(settings: Settings, journal: Journal) -> None:
    journal.cancelled = None
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")
    assert journal.order == ["cancel", "commit"]


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (database_error("55P03"), 409, "post_busy"),
        (database_error("57014"), 409, "post_busy"),
        (database_error("08006"), 503, "database_unavailable"),
        (ApiProblem(409, "not_cancellable", "x"), 409, "not_cancellable"),
        (ApiProblem(409, "stale_data", "x"), 409, "stale_data"),
    ],
)
def test_a_busy_post_a_database_failure_and_a_refusal_stop_before_the_keyboard(
    settings: Settings,
    journal: Journal,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    journal.error = error
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (status_code, code)
    assert journal.order == ["cancel"]


def test_a_missing_bot_is_503_before_the_database_is_touched(settings: Settings, journal: Journal) -> None:
    client, session_factory = make_client(settings, journal, with_bot=False)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, "telegram_unavailable")
    session_factory.assert_not_called()


def test_the_route_logs_the_request_and_the_refusals_it_alone_sees(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    journal.error = database_error("55P03")
    client, _ = make_client(settings, journal)

    client.post(PATH, json=BODY, headers=bearer())

    assert [(entry["event"], entry.get("reason")) for entry in recorder.entries] == [
        ("audio_preparation_cancel_requested", None),
        ("audio_preparation_cancel_refused", "post_busy"),
    ]
