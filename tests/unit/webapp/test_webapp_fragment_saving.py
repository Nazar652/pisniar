"""Saving a fragment without a render: the same locks and refusals as choosing one, no status change, no
Telegram and no queue, and the route that commits and answers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from support import ADMIN_ID, NOW
from test_webapp_edit_routes import database_error
from test_webapp_fragment_choice import (
    BODY,
    POST_ID,
    Locks,
    RecordingLogger,
    make_post,
    make_request,
)
from test_webapp_posts_routes import FakeSession, bearer

from pisniar.config import Settings
from pisniar.db.models import AudioAsset, Post, Track
from pisniar.db.states import PostStatus
from pisniar.webapp import fragment_choice
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_choice import save_fragment
from pisniar.webapp.schemas import FragmentSaved

PATH = f"/api/posts/{POST_ID}/fragment"
SAVED = FragmentSaved(
    post_id=POST_ID,
    post_updated_at=datetime(2026, 10, 2, 12, 5, tzinfo=UTC),
    start_ms=60_000,
    end_ms=105_000,
    preview_stale=True,
)


@pytest.fixture
def logger(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(fragment_choice, "logger", recorder)
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

    async def current_asset(session: object, track: Track) -> AudioAsset | None:
        held.order.append("audio")
        return held.asset

    monkeypatch.setattr(fragment_choice, "lock_post_without_waiting", lock_post)
    monkeypatch.setattr(fragment_choice, "lock_track_for_write", lock_track)
    monkeypatch.setattr(fragment_choice, "current_audio_asset", current_asset)
    return held


@pytest.fixture
def preview_bounds(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    reader = AsyncMock(return_value=(0, 45_000))
    monkeypatch.setattr(fragment_choice, "preview_fragment_bounds", reader)
    return reader


def make_session() -> Any:
    session = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return session


async def test_the_bounds_are_stored_and_nothing_else_moves(
    locks: Locks, preview_bounds: AsyncMock, logger: RecordingLogger
) -> None:
    session = make_session()

    saved = await save_fragment(session, POST_ID, make_request(), ADMIN_ID)

    assert saved is not None
    assert (saved.start_ms, saved.end_ms, saved.preview_stale) == (60_000, 105_000, True)
    assert locks.post is not None
    assert (locks.post.fragment_start_ms, locks.post.fragment_end_ms) == (60_000, 105_000)
    assert locks.post.status is PostStatus.RENDERED
    assert (locks.post.preview_chat_id, locks.post.preview_message_id) == (555, 41)
    assert locks.order == ["post", "track", "audio"]
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(locks.post)
    assert logger.entries == [
        {
            "event": "fragment_saved",
            "log_level": "info",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
            "start_ms": 60_000,
            "end_ms": 105_000,
            "changed": True,
            "preview_stale": True,
        }
    ]


async def test_equal_bounds_write_nothing_and_do_not_flush(
    locks: Locks, preview_bounds: AsyncMock, logger: RecordingLogger
) -> None:
    session = make_session()

    saved = await save_fragment(session, POST_ID, make_request(start_ms=0, end_ms=45_000), ADMIN_ID)

    assert saved is not None
    assert saved.preview_stale is False
    session.flush.assert_not_awaited()
    session.refresh.assert_not_awaited()
    assert logger.entries[0]["changed"] is False


async def test_a_preview_cut_with_the_saved_bounds_is_not_stale(locks: Locks, preview_bounds: AsyncMock) -> None:
    preview_bounds.return_value = (60_000, 105_000)

    saved = await save_fragment(make_session(), POST_ID, make_request(), ADMIN_ID)

    assert saved is not None
    assert saved.preview_stale is False


@pytest.mark.parametrize(
    ("status", "failed_from"),
    [
        (PostStatus.EDITING, None),
        (PostStatus.RENDERED, None),
        (PostStatus.FAILED, PostStatus.DOWNLOADED),
        (PostStatus.FAILED, PostStatus.RENDERING),
    ],
)
async def test_every_choosable_status_keeps_its_status(
    locks: Locks,
    preview_bounds: AsyncMock,
    status: PostStatus,
    failed_from: PostStatus | None,
) -> None:
    locks.post = make_post(status, failed_from=failed_from)

    await save_fragment(make_session(), POST_ID, make_request(), ADMIN_ID)

    assert (locks.post.status, locks.post.failed_from_status) == (status, failed_from)


async def test_a_missing_post_or_track_is_none(locks: Locks, preview_bounds: AsyncMock) -> None:
    locks.post = None
    assert await save_fragment(make_session(), POST_ID, make_request(), ADMIN_ID) is None

    locks.post = make_post()
    locks.track = None
    assert await save_fragment(make_session(), POST_ID, make_request(), ADMIN_ID) is None
    assert "audio" not in locks.order


@pytest.mark.parametrize(
    ("status", "failed_from"),
    [(PostStatus.PUBLISHED, None), (PostStatus.RENDERING, None), (PostStatus.RESOLVED, None)],
)
async def test_a_post_that_cannot_be_chosen_is_refused_with_the_save_event(
    locks: Locks,
    logger: RecordingLogger,
    status: PostStatus,
    failed_from: PostStatus | None,
) -> None:
    locks.post = make_post(status, failed_from=failed_from)

    with pytest.raises(ApiProblem) as refusal:
        await save_fragment(make_session(), POST_ID, make_request(), ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "not_choosable")
    assert locks.order == ["post"]
    assert [entry["event"] for entry in logger.entries] == ["fragment_save_refused"]
    assert (locks.post.fragment_start_ms, locks.post.fragment_end_ms) == (0, 45_000)


async def test_a_stale_stamp_an_missing_audio_and_invalid_bounds_are_refused_and_write_nothing(
    locks: Locks,
    logger: RecordingLogger,
) -> None:
    with pytest.raises(ApiProblem) as stale:
        await save_fragment(
            make_session(),
            POST_ID,
            make_request(expected_post_updated_at=datetime(2026, 10, 2, 11, 0, tzinfo=UTC)),
            ADMIN_ID,
        )
    locks.asset = None
    with pytest.raises(ApiProblem) as missing:
        await save_fragment(make_session(), POST_ID, make_request(), ADMIN_ID)
    locks.asset = AudioAsset(duration_ms=180_000)
    with pytest.raises(ApiProblem) as invalid:
        await save_fragment(make_session(), POST_ID, make_request(start_ms=5_000, end_ms=5_000), ADMIN_ID)

    assert [problem.value.code for problem in (stale, missing, invalid)] == [
        "stale_data",
        "audio_missing",
        "invalid_fragment",
    ]
    assert [entry["event"] for entry in logger.entries] == ["fragment_save_refused"] * 3
    assert locks.post is not None
    assert (locks.post.fragment_start_ms, locks.post.fragment_end_ms) == (0, 45_000)


class Journal:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.bot = MagicMock()
        self.celery_client = MagicMock()
        self.saved: FragmentSaved | None = SAVED
        self.error: Exception | None = None

    async def save(
        self,
        session: object,
        post_id: int,
        request: object,
        user_id: int,
    ) -> FragmentSaved | None:
        self.order.append("save")
        if self.error is not None:
            raise self.error
        return self.saved


class JournalSession(FakeSession):
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    async def commit(self) -> None:
        self.journal.order.append("commit")


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Journal:
    recorded = Journal()
    monkeypatch.setattr(posts_module, "save_fragment", recorded.save)
    return recorded


def make_client(settings: Settings, recorded: Journal) -> tuple[TestClient, MagicMock]:
    session_factory = MagicMock(side_effect=lambda: JournalSession(recorded))
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        bot=None,
        celery_client=None,
    )
    return TestClient(app), session_factory


def test_an_anonymous_or_foreign_caller_is_refused_before_the_database(settings: Settings, journal: Journal) -> None:
    client, session_factory = make_client(settings, journal)

    anonymous = client.put(PATH, json=BODY)
    stranger = client.put(PATH, json=BODY, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()
    assert journal.order == []


def test_the_save_commits_and_answers_without_a_bot_or_a_queue(settings: Settings, journal: Journal) -> None:
    client, _ = make_client(settings, journal)

    response = client.put(PATH, json=BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json() == {
        "post_id": POST_ID,
        "post_updated_at": "2026-10-02T12:05:00Z",
        "start_ms": 60_000,
        "end_ms": 105_000,
        "preview_stale": True,
    }
    assert journal.order == ["save", "commit"]


def test_a_missing_post_is_404(settings: Settings, journal: Journal) -> None:
    journal.saved = None
    client, _ = make_client(settings, journal)

    response = client.put(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (database_error("55P03"), 409, "post_busy"),
        (database_error("08006"), 503, "database_unavailable"),
        (ApiProblem(409, "stale_data", "x"), 409, "stale_data"),
        (ApiProblem(422, "invalid_fragment", "x"), 422, "invalid_fragment"),
    ],
)
def test_a_busy_post_a_database_failure_and_a_refusal_are_answered_without_a_commit(
    settings: Settings,
    journal: Journal,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    journal.error = error
    client, _ = make_client(settings, journal)

    response = client.put(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (status_code, code)
    assert journal.order == ["save"]


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "start_ms": True},
        {**BODY, "end_ms": "105000"},
        {**BODY, "extra": 1},
        {"start_ms": 0, "end_ms": 1},
    ],
)
def test_a_malformed_body_is_422_before_the_database(
    settings: Settings,
    journal: Journal,
    body: dict[str, Any],
) -> None:
    client, session_factory = make_client(settings, journal)

    response = client.put(PATH, json=body, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    session_factory.assert_not_called()


def test_the_route_logs_the_request_with_the_user_the_post_and_the_bounds(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    client, _ = make_client(settings, journal)

    client.put(PATH, json=BODY, headers=bearer())

    assert recorder.entries == [
        {
            "event": "fragment_save_requested",
            "log_level": "info",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
            "start_ms": 60_000,
            "end_ms": 105_000,
        }
    ]
