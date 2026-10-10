"""Preparing the audio before the first fragment choice: the locked database part with its refusals, the steps after the
commit, and the route that orders them (database, then the queue, then the draft's keyboard).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from support import ADMIN_ID, NOW
from test_webapp_edit_routes import database_error
from test_webapp_fragment_choice import RecordingLogger
from test_webapp_posts_routes import FakeSession, bearer

from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.dispatch import DOWNLOAD_DISPATCH_FAILED_ERROR
from pisniar.webapp import audio_preparation
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.audio_preparation import (
    AUDIO_READY_MESSAGE,
    AudioPreparation,
    enqueue_audio_preparation,
    prepare_audio,
    release_draft_keyboard,
)
from pisniar.webapp.database import POST_BUSY_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.worker.celery_app import PREPARE_POST_AUDIO_TASK_NAME

POST_ID = 7
DRAFT = (555, 41)
PATH = f"/api/posts/{POST_ID}/fragment/audio"
PREPARATION = AudioPreparation(POST_ID, DRAFT)
MUSIC_LINKS = {"youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}}


def make_post(
    status: PostStatus = PostStatus.RESOLVED,
    *,
    failed_from: PostStatus | None = None,
    draft: tuple[int, int] | None = DRAFT,
) -> Post:
    return Post(
        id=POST_ID,
        track_id=3,
        status=status,
        failed_from_status=failed_from,
        last_error="boom" if failed_from is not None else None,
        draft_chat_id=None if draft is None else draft[0],
        draft_message_id=None if draft is None else draft[1],
    )


@dataclass
class Locks:
    post: Post | None = field(default_factory=make_post)
    track: Track | None = field(default_factory=lambda: Track(id=3, platform_links=MUSIC_LINKS))
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


async def refusal_of() -> ApiProblem:
    with pytest.raises(ApiProblem) as refusal:
        await prepare_audio(MagicMock(), POST_ID, ADMIN_ID)
    return refusal.value


async def test_a_draft_goes_to_downloading_and_remembers_its_keyboard(locks: Locks, logger: RecordingLogger) -> None:
    preparation = await prepare_audio(MagicMock(), POST_ID, ADMIN_ID)

    assert preparation == PREPARATION
    assert locks.post is not None
    assert locks.post.status is PostStatus.DOWNLOADING
    assert locks.order == ["post", "track"]
    assert logger.entries == []


async def test_a_failed_preparation_starts_again_and_clears_the_failure(locks: Locks) -> None:
    locks.post = make_post(PostStatus.FAILED, failed_from=PostStatus.DOWNLOADING)

    preparation = await prepare_audio(MagicMock(), POST_ID, ADMIN_ID)

    assert preparation == PREPARATION
    assert locks.post.status is PostStatus.DOWNLOADING
    assert (locks.post.failed_from_status, locks.post.last_error) == (None, None)


async def test_a_post_without_a_draft_has_no_keyboard_to_release(locks: Locks) -> None:
    locks.post = make_post(draft=None)

    preparation = await prepare_audio(MagicMock(), POST_ID, ADMIN_ID)

    assert preparation == AudioPreparation(POST_ID, None)


@pytest.mark.parametrize("missing", ["post", "track"])
async def test_a_missing_row_is_none_and_nothing_is_written(locks: Locks, missing: str) -> None:
    setattr(locks, missing, None)

    assert await prepare_audio(MagicMock(), POST_ID, ADMIN_ID) is None
    if locks.post is not None:
        assert locks.post.status is PostStatus.RESOLVED


@pytest.mark.parametrize(
    ("status", "failed_from", "message"),
    [
        (PostStatus.EDITING, None, AUDIO_READY_MESSAGE),
        (PostStatus.RENDERED, None, AUDIO_READY_MESSAGE),
        (PostStatus.FAILED, PostStatus.RENDERING, AUDIO_READY_MESSAGE),
        (PostStatus.DOWNLOADING, None, None),
        (PostStatus.DOWNLOADED, None, None),
        (PostStatus.RENDERING, None, None),
        (PostStatus.PUBLISHED, None, None),
        (PostStatus.FAILED, PostStatus.PUBLISHING, None),
    ],
)
async def test_a_post_that_cannot_be_prepared_is_409_with_a_sentence_and_no_state_name(
    locks: Locks,
    logger: RecordingLogger,
    status: PostStatus,
    failed_from: PostStatus | None,
    message: str | None,
) -> None:
    locks.post = make_post(status, failed_from=failed_from)

    problem = await refusal_of()

    assert (problem.status_code, problem.code) == (409, "not_preparable")
    assert message is None or problem.message == message
    assert status.value not in problem.message.lower()
    assert locks.order == ["post"]
    assert locks.post.status is status
    assert logger.entries == [
        {
            "event": "audio_preparation_refused",
            "log_level": "warning",
            "reason": "not_preparable",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
        }
    ]


@pytest.mark.parametrize(
    "platform_links",
    [{}, {"youtubeMusic": {"url": "https://music.youtube.com/watch?v=bad"}}],
)
async def test_a_track_without_a_usable_youtube_music_link_is_409_and_unchanged(
    locks: Locks,
    logger: RecordingLogger,
    platform_links: dict[str, Any],
) -> None:
    locks.track = Track(id=3, platform_links=platform_links)

    problem = await refusal_of()

    assert (problem.status_code, problem.code) == (409, "youtube_music_missing")
    assert locks.post is not None
    assert locks.post.status is PostStatus.RESOLVED
    assert logger.entries[0]["reason"] == "youtube_music_missing"


async def test_the_prepare_task_is_enqueued_by_name_and_logged(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(audio_preparation, "dispatch_task", dispatch)
    celery_client = MagicMock()

    await enqueue_audio_preparation(celery_client, MagicMock(), PREPARATION, ADMIN_ID)

    assert dispatch.await_args_list[0].args[:3] == (
        celery_client,
        PREPARE_POST_AUDIO_TASK_NAME,
        {"post_id": POST_ID},
    )
    assert logger.entries == [
        {
            "event": "audio_preparation_dispatched",
            "log_level": "info",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
        }
    ]


async def test_a_refused_task_fails_the_post_at_downloading_and_asks_for_another_try(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    monkeypatch.setattr(audio_preparation, "dispatch_task", AsyncMock(return_value=False))
    mark = AsyncMock()
    monkeypatch.setattr(audio_preparation, "mark_dispatch_failure", mark)
    session_factory = MagicMock()

    with pytest.raises(ApiProblem) as refusal:
        await enqueue_audio_preparation(MagicMock(), session_factory, PREPARATION, ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (503, "dispatch_failed")
    assert PostStatus.DOWNLOADING.value not in refusal.value.message
    mark.assert_awaited_once_with(
        session_factory,
        POST_ID,
        expected_status=PostStatus.DOWNLOADING,
        error=DOWNLOAD_DISPATCH_FAILED_ERROR,
    )
    assert [entry["event"] for entry in logger.entries] == ["audio_preparation_refused"]


async def test_the_draft_loses_its_keyboard_with_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remove = AsyncMock()
    monkeypatch.setattr(audio_preparation, "remove_draft_keyboard", remove)
    bot = MagicMock()

    await release_draft_keyboard(bot, DRAFT, POST_ID, ADMIN_ID)

    remove.assert_awaited_once_with(bot, *DRAFT, POST_ID, request_timeout=15)


async def test_without_a_draft_nothing_is_edited(monkeypatch: pytest.MonkeyPatch) -> None:
    remove = AsyncMock()
    monkeypatch.setattr(audio_preparation, "remove_draft_keyboard", remove)

    await release_draft_keyboard(MagicMock(), None, POST_ID, ADMIN_ID)

    remove.assert_not_awaited()


async def test_a_failed_keyboard_removal_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    monkeypatch.setattr(audio_preparation, "remove_draft_keyboard", AsyncMock(side_effect=TimeoutError("slow")))

    await release_draft_keyboard(MagicMock(), DRAFT, POST_ID, ADMIN_ID)

    assert [(entry["event"], entry["post_id"]) for entry in logger.entries] == [
        ("draft_keyboard_removal_failed", POST_ID)
    ]


class Journal:
    """one ordered record of the commit, the queue and the keyboard, plus what Telegram and the broker were asked while
    the transaction was open."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.asked_inside_transaction: list[object] = []
        self.bot = MagicMock()
        self.celery_client = MagicMock()
        self.preparation: AudioPreparation | None = PREPARATION
        self.prepare_error: Exception | None = None

    async def prepare(
        self,
        session: object,
        post_id: int,
        user_id: int,
    ) -> Any:
        self.order.append("prepare")
        self.asked_inside_transaction += [*self.bot.mock_calls, *self.celery_client.mock_calls]
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.preparation

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
    monkeypatch.setattr(posts_module, "prepare_audio", recorded.prepare)
    monkeypatch.setattr(posts_module, "release_draft_keyboard", recorded.release)
    monkeypatch.setattr(posts_module, "enqueue_audio_preparation", recorded.enqueue)
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


def test_an_anonymous_or_foreign_caller_is_refused_before_the_database_the_bot_and_the_queue(
    settings: Settings, journal: Journal
) -> None:
    client, session_factory = make_client(settings, journal)

    anonymous = client.post(PATH)
    stranger = client.post(PATH, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()
    assert journal.order == []
    assert journal.bot.mock_calls == [] and journal.celery_client.mock_calls == []


def test_the_database_commits_then_the_task_is_enqueued_then_the_draft_keyboard_goes(
    settings: Settings, journal: Journal
) -> None:
    client, _ = make_client(settings, journal)

    response = client.post(PATH, headers=bearer())

    assert response.status_code == 200
    assert response.json() == {"post_id": POST_ID, "status_label": "Завантажується аудіо"}
    assert journal.order == ["prepare", "commit", "enqueue", "keyboard"]
    assert journal.asked_inside_transaction == []


def test_a_missing_post_is_404_and_nothing_follows_the_transaction(settings: Settings, journal: Journal) -> None:
    journal.preparation = None
    client, _ = make_client(settings, journal)

    response = client.post(PATH, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")
    assert journal.order == ["prepare", "commit"]


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (database_error("55P03"), 409, "post_busy"),
        (database_error("57014"), 409, "post_busy"),
        (database_error("08006"), 503, "database_unavailable"),
        (ApiProblem(409, "not_preparable", "x"), 409, "not_preparable"),
        (ApiProblem(409, "youtube_music_missing", "x"), 409, "youtube_music_missing"),
    ],
)
def test_a_refused_lock_a_database_failure_and_a_refusal_stop_before_the_queue_and_the_keyboard(
    settings: Settings,
    journal: Journal,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    journal.prepare_error = error
    client, _ = make_client(settings, journal)

    response = client.post(PATH, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (status_code, code)
    assert journal.order == ["prepare"]


def test_a_busy_post_answers_with_the_busy_sentence(settings: Settings, journal: Journal) -> None:
    journal.prepare_error = database_error("55P03")
    client, _ = make_client(settings, journal)

    response = client.post(PATH, headers=bearer())

    assert response.json()["message"] == POST_BUSY_MESSAGE


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

    response = client.post(PATH, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, code)
    session_factory.assert_not_called()


def test_a_post_id_beyond_bigint_is_422_before_the_database(settings: Settings, journal: Journal) -> None:
    client, session_factory = make_client(settings, journal)

    response = client.post("/api/posts/9223372036854775808/fragment/audio", headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    session_factory.assert_not_called()


def test_a_refused_queue_fails_the_post_through_the_route(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    monkeypatch.setattr(posts_module, "enqueue_audio_preparation", audio_preparation.enqueue_audio_preparation)
    monkeypatch.setattr(audio_preparation, "dispatch_task", AsyncMock(return_value=False))
    mark = AsyncMock()
    monkeypatch.setattr(audio_preparation, "mark_dispatch_failure", mark)
    client, _ = make_client(settings, journal)

    response = client.post(PATH, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, "dispatch_failed")
    assert mark.await_args_list[0].kwargs["expected_status"] is PostStatus.DOWNLOADING
    assert journal.order == ["prepare", "commit"]


def test_the_route_logs_the_request_with_the_user_and_the_post(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    client, _ = make_client(settings, journal)

    client.post(PATH, headers=bearer())

    assert recorder.entries == [
        {
            "event": "audio_preparation_requested",
            "log_level": "info",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
        }
    ]


def test_a_refusal_the_route_alone_can_see_is_logged_once_with_its_reason(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    journal.prepare_error = database_error("55P03")
    client, _ = make_client(settings, journal)

    client.post(PATH, headers=bearer())

    refusals = [entry for entry in recorder.entries if entry["event"] == "audio_preparation_refused"]
    assert [(entry["reason"], entry["post_id"]) for entry in refusals] == [("post_busy", POST_ID)]
