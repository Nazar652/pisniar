"""Publishing the cover without a video: the locked database part with its refusals in order, the steps after the
commit, and the route that orders them (database, then the queue, then the draft's keyboard).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from support import ADMIN_ID, NOW
from test_webapp_edit_routes import database_error
from test_webapp_editing import make_rows
from test_webapp_fragment_choice import RecordingLogger
from test_webapp_posts_routes import FakeSession, bearer

from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.dispatch import PUBLISH_DISPATCH_FAILED_ERROR
from pisniar.webapp import cover_publication
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.cover_publication import (
    CAPTION_NOT_APPLIED_MESSAGE,
    StartedCoverPublication,
    enqueue_cover_publication,
    start_cover_publication,
)
from pisniar.webapp.database import POST_BUSY_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import CoverPublicationRequest
from pisniar.webapp.service import rebuild_caption
from pisniar.worker.celery_app import PUBLISH_POST_TASK_NAME

STAMP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
POST_ID = 7
DRAFT = (555, 41)
PATH = f"/api/posts/{POST_ID}/publish-cover"
BODY = {"expected_post_updated_at": STAMP.isoformat()}
STARTED = StartedCoverPublication(POST_ID, DRAFT)


def make_request(**overrides: Any) -> CoverPublicationRequest:
    values: dict[str, Any] = {"expected_post_updated_at": STAMP} | overrides
    return CoverPublicationRequest(**values)


def make_post_and_track(status: PostStatus = PostStatus.RESOLVED) -> tuple[Post, Track]:
    """a post whose stored caption is exactly the rebuilt one, with a draft."""
    post, track = make_rows(status)
    _, html = rebuild_caption(post, track)
    post.caption = html
    post.draft_chat_id, post.draft_message_id = DRAFT
    return post, track


class Held:
    """what the lock and the plain read hand back, and the order they were asked in."""

    def __init__(self) -> None:
        self.post: Post | None
        self.track: Track | None
        self.post, self.track = make_post_and_track()
        self.order: list[str] = []


@pytest.fixture
def logger(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(cover_publication, "logger", recorder)
    return recorder


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Held:
    rows = Held()

    async def lock_post(session: object, post_id: int) -> Post | None:
        rows.order.append("post")
        return rows.post

    monkeypatch.setattr(cover_publication, "lock_post_without_waiting", lock_post)
    return rows


def session_for(held: Held) -> MagicMock:
    session = MagicMock()

    async def get(
        model: type,
        identifier: int,
        **options: Any,
    ) -> Track | None:
        assert options == {}
        held.order.append("track")
        return held.track

    session.get = get
    return session


async def refusal_of(held: Held, request: CoverPublicationRequest | None = None) -> ApiProblem:
    with pytest.raises(ApiProblem) as refusal:
        await start_cover_publication(session_for(held), POST_ID, request or make_request(), ADMIN_ID)
    return refusal.value


async def test_a_draft_goes_to_publishing_and_remembers_its_keyboard(held: Held, logger: RecordingLogger) -> None:
    started = await start_cover_publication(session_for(held), POST_ID, make_request(), ADMIN_ID)

    assert started == STARTED
    assert held.post is not None
    assert held.post.status is PostStatus.PUBLISHING
    assert held.order == ["post", "track"]
    assert logger.entries == []


@pytest.mark.parametrize("stage", [PostStatus.DOWNLOADING, PostStatus.PUBLISHING])
async def test_a_failed_post_without_a_preview_is_published_again_as_a_cover(held: Held, stage: PostStatus) -> None:
    held.post, held.track = make_post_and_track(PostStatus.FAILED)
    held.post.failed_from_status = stage

    started = await start_cover_publication(session_for(held), POST_ID, make_request(), ADMIN_ID)

    assert started == STARTED
    assert held.post.status is PostStatus.PUBLISHING


async def test_a_post_without_a_draft_has_no_keyboard_to_remove(held: Held) -> None:
    held.post, held.track = make_post_and_track()
    held.post.draft_chat_id = held.post.draft_message_id = None

    started = await start_cover_publication(session_for(held), POST_ID, make_request(), ADMIN_ID)

    assert started == StartedCoverPublication(POST_ID, None)


async def test_a_missing_post_or_track_is_none(held: Held) -> None:
    held.track = None
    assert await start_cover_publication(session_for(held), POST_ID, make_request(), 1) is None

    held.post = None
    held.order.clear()
    assert await start_cover_publication(session_for(held), POST_ID, make_request(), 1) is None
    assert held.order == ["post"]


@pytest.mark.parametrize(
    "status",
    [
        PostStatus.EDITING,
        PostStatus.RENDERED,
        PostStatus.RENDERING,
        PostStatus.PUBLISHING,
        PostStatus.PUBLISHED,
        PostStatus.ARCHIVED,
    ],
)
async def test_a_post_that_cannot_be_published_as_a_cover_is_409_with_a_sentence_and_no_state_name(
    held: Held,
    logger: RecordingLogger,
    status: PostStatus,
) -> None:
    held.post, held.track = make_post_and_track(status)

    problem = await refusal_of(held)

    assert (problem.status_code, problem.code) == (409, "not_publishable")
    assert status.value not in problem.message.lower()
    assert held.order == ["post"]
    assert held.post.status is status
    assert [entry["reason"] for entry in logger.entries] == ["not_publishable"]


async def test_a_post_that_has_a_preview_is_not_published_as_a_cover(held: Held) -> None:
    assert held.post is not None
    held.post.preview_message_id = 99

    problem = await refusal_of(held)

    assert problem.code == "not_publishable"
    assert held.post.status is PostStatus.RESOLVED


async def test_a_post_changed_since_the_editor_opened_is_stale_before_the_track_is_read(
    held: Held, logger: RecordingLogger
) -> None:
    problem = await refusal_of(held, make_request(expected_post_updated_at=datetime(2026, 1, 1, tzinfo=UTC)))

    assert (problem.status_code, problem.code) == (409, "stale_data")
    assert held.order == ["post"]
    assert logger.entries[0]["reason"] == "stale_data"


async def test_a_caption_not_yet_applied_to_the_draft_is_refused_with_its_sentence(
    held: Held, logger: RecordingLogger
) -> None:
    assert held.post is not None
    held.post.caption = "<b>old</b>"

    problem = await refusal_of(held)

    assert (problem.status_code, problem.code) == (409, "caption_not_applied")
    assert problem.message == CAPTION_NOT_APPLIED_MESSAGE
    assert held.post.status is PostStatus.RESOLVED


async def test_a_post_with_no_stored_caption_counts_as_not_applied(held: Held) -> None:
    assert held.post is not None
    held.post.caption = None

    assert (await refusal_of(held)).code == "caption_not_applied"


async def test_a_track_without_a_title_has_no_caption_to_publish(held: Held) -> None:
    assert held.track is not None
    held.track.title = None

    problem = await refusal_of(held)

    assert (problem.status_code, problem.code) == (409, "caption_unavailable")


async def test_the_publish_task_is_enqueued_by_name_with_the_admin_and_logged(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(cover_publication, "dispatch_task", dispatch)
    celery_client = MagicMock()

    await enqueue_cover_publication(celery_client, MagicMock(), STARTED, ADMIN_ID)

    assert dispatch.await_args_list[0].args[:3] == (
        celery_client,
        PUBLISH_POST_TASK_NAME,
        {"post_id": POST_ID, "published_by_user_id": ADMIN_ID},
    )
    assert [entry["event"] for entry in logger.entries] == ["cover_publication_dispatched"]


async def test_a_refused_task_fails_the_post_at_publishing_and_names_the_retry(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    monkeypatch.setattr(cover_publication, "dispatch_task", AsyncMock(return_value=False))
    mark = AsyncMock()
    monkeypatch.setattr(cover_publication, "mark_dispatch_failure", mark)
    session_factory = MagicMock()

    with pytest.raises(ApiProblem) as refusal:
        await enqueue_cover_publication(MagicMock(), session_factory, STARTED, ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (503, "dispatch_failed")
    assert f"/retry {POST_ID}" in refusal.value.message
    mark.assert_awaited_once_with(
        session_factory,
        POST_ID,
        expected_status=PostStatus.PUBLISHING,
        error=PUBLISH_DISPATCH_FAILED_ERROR,
    )


def test_the_request_refuses_an_extra_field_and_a_missing_stamp() -> None:
    with pytest.raises(ValueError):
        make_request(extra=1)
    with pytest.raises(ValueError):
        CoverPublicationRequest.model_validate({})


class Journal:
    """one ordered record of the commit, the queue and the keyboard, plus what Telegram and the
    broker were asked while the transaction was open."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.asked_inside_transaction: list[object] = []
        self.bot = MagicMock()
        self.celery_client = MagicMock()
        self.started: StartedCoverPublication | None = STARTED
        self.error: Exception | None = None

    async def start(
        self,
        session: object,
        post_id: int,
        request: object,
        user_id: int,
    ) -> Any:
        self.order.append("start")
        self.asked_inside_transaction += [*self.bot.mock_calls, *self.celery_client.mock_calls]
        if self.error is not None:
            raise self.error
        return self.started

    async def release(
        self,
        bot: object,
        draft: object,
        post_id: int,
        user_id: int,
    ) -> None:
        self.order.append("keyboard")

    async def enqueue(self, *arguments: object) -> None:
        self.order.append("enqueue")


class JournalSession(FakeSession):
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    async def commit(self) -> None:
        self.journal.order.append("commit")


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Journal:
    recorded = Journal()
    monkeypatch.setattr(posts_module, "start_cover_publication", recorded.start)
    monkeypatch.setattr(posts_module, "release_draft_keyboard", recorded.release)
    monkeypatch.setattr(posts_module, "enqueue_cover_publication", recorded.enqueue)
    return recorded


def make_client(
    settings: Settings,
    recorded: Journal,
    *,
    with_bot: bool = True,
    with_queue: bool = True,
) -> tuple[TestClient, MagicMock]:
    session_factory = MagicMock(side_effect=lambda: JournalSession(recorded))
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        bot=recorded.bot if with_bot else None,
        celery_client=recorded.celery_client if with_queue else None,
    )
    return TestClient(app), session_factory


def test_an_anonymous_or_foreign_caller_is_refused_before_everything(settings: Settings, journal: Journal) -> None:
    client, session_factory = make_client(settings, journal)

    anonymous = client.post(PATH, json=BODY)
    stranger = client.post(PATH, json=BODY, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()
    assert journal.order == []


def test_the_database_commits_then_the_task_is_enqueued_then_the_draft_keyboard_goes(
    settings: Settings, journal: Journal
) -> None:
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json() == {"post_id": POST_ID, "status_label": "Публікується"}
    assert journal.order == ["start", "commit", "enqueue", "keyboard"]
    assert journal.asked_inside_transaction == []


def test_a_missing_post_is_404_and_nothing_follows_the_transaction(settings: Settings, journal: Journal) -> None:
    journal.started = None
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")
    assert journal.order == ["start", "commit"]


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (database_error("55P03"), 409, "post_busy"),
        (database_error("57014"), 409, "post_busy"),
        (database_error("08006"), 503, "database_unavailable"),
        (ApiProblem(409, "not_publishable", "x"), 409, "not_publishable"),
        (ApiProblem(409, "caption_not_applied", "x"), 409, "caption_not_applied"),
    ],
)
def test_a_refused_lock_a_database_failure_and_a_refusal_stop_before_the_queue(
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
    assert journal.order == ["start"]


def test_a_busy_post_answers_with_the_busy_sentence(settings: Settings, journal: Journal) -> None:
    journal.error = database_error("55P03")
    client, _ = make_client(settings, journal)

    assert client.post(PATH, json=BODY, headers=bearer()).json()["message"] == POST_BUSY_MESSAGE


@pytest.mark.parametrize(
    ("with_bot", "with_queue", "code"),
    [(False, True, "telegram_unavailable"), (True, False, "queue_unavailable")],
)
def test_a_missing_bot_or_queue_is_503_before_the_database_is_touched(
    settings: Settings,
    journal: Journal,
    with_bot: bool,
    with_queue: bool,
    code: str,
) -> None:
    client, session_factory = make_client(settings, journal, with_bot=with_bot, with_queue=with_queue)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, code)
    session_factory.assert_not_called()


@pytest.mark.parametrize("body", [{}, {**BODY, "extra": 1}, {"expected_post_updated_at": "x"}])
def test_a_malformed_body_is_422_before_the_database(
    settings: Settings,
    journal: Journal,
    body: dict[str, Any],
) -> None:
    client, session_factory = make_client(settings, journal)

    response = client.post(PATH, json=body, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    session_factory.assert_not_called()


def test_a_refused_queue_fails_the_post_through_the_route(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    monkeypatch.setattr(posts_module, "enqueue_cover_publication", cover_publication.enqueue_cover_publication)
    monkeypatch.setattr(cover_publication, "dispatch_task", AsyncMock(return_value=False))
    mark = AsyncMock()
    monkeypatch.setattr(cover_publication, "mark_dispatch_failure", mark)
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, "dispatch_failed")
    assert mark.await_args_list[0].kwargs["expected_status"] is PostStatus.PUBLISHING
    assert journal.order == ["start", "commit"]


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
        ("cover_publication_requested", None),
        ("cover_publication_refused", "post_busy"),
    ]
