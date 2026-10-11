"""Choosing a fragment: the locked database part with its refusals, the steps after the commit,
and the route that orders them (database, then the queue, then the old keyboard).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from support import ADMIN_ID, NOW
from test_webapp_edit_routes import database_error
from test_webapp_posts_routes import FakeSession, bearer

from pisniar.config import Settings
from pisniar.db.models import AudioAsset, Post, Track
from pisniar.db.states import PostStatus
from pisniar.dispatch import RENDER_DISPATCH_FAILED_ERROR
from pisniar.webapp import fragment_choice
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.database import POST_BUSY_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_choice import (
    FragmentChoice,
    choose_fragment,
    enqueue_render,
    release_superseded_preview,
)
from pisniar.webapp.schemas import BIGINT_MAX, FragmentRequest
from pisniar.worker.celery_app import RENDER_POST_VIDEO_TASK_NAME, build_celery_app
from pisniar.worker.fragment import FragmentBounds

STAMP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
POST_ID = 7
DURATION_MS = 180_000
PREVIEW = (555, 41)
PATH = f"/api/posts/{POST_ID}/fragment"
BODY = {"start_ms": 60_000, "end_ms": 105_000, "expected_post_updated_at": STAMP.isoformat()}
CHOICE = FragmentChoice(POST_ID, FragmentBounds(60_000, 105_000), PREVIEW)


class RecordingLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def record(event: str, **fields: Any) -> None:
            self.entries.append({"event": event, "log_level": level, **fields})

        return record


def make_request(**overrides: Any) -> FragmentRequest:
    values: dict[str, Any] = {**BODY, "expected_post_updated_at": STAMP} | overrides
    return FragmentRequest(**values)


def make_post(
    status: PostStatus = PostStatus.RENDERED,
    *,
    failed_from: PostStatus | None = None,
    preview: tuple[int, int] | None = PREVIEW,
) -> Post:
    post = Post(
        id=POST_ID,
        track_id=3,
        status=status,
        failed_from_status=failed_from,
        last_error="boom" if failed_from is not None else None,
        fragment_start_ms=0,
        fragment_end_ms=45_000,
        preview_chat_id=None if preview is None else preview[0],
        preview_message_id=None if preview is None else preview[1],
    )
    post.updated_at = STAMP
    return post


@dataclass
class Locks:
    """what the locking helpers hand back, and the order they were asked in."""

    post: Post | None = field(default_factory=make_post)
    track: Track | None = field(default_factory=lambda: Track(id=3))
    asset: AudioAsset | None = field(default_factory=lambda: AudioAsset(duration_ms=DURATION_MS))
    order: list[str] = field(default_factory=list)


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


async def refusal_of(request: FragmentRequest) -> ApiProblem:
    with pytest.raises(ApiProblem) as refusal:
        await choose_fragment(MagicMock(), POST_ID, request, ADMIN_ID)
    return refusal.value


async def test_a_rendered_post_gets_the_new_bounds_and_renders_again(locks: Locks, logger: RecordingLogger) -> None:
    choice = await choose_fragment(MagicMock(), POST_ID, make_request(), ADMIN_ID)

    assert choice == CHOICE
    assert locks.post is not None
    assert (locks.post.fragment_start_ms, locks.post.fragment_end_ms) == (60_000, 105_000)
    assert locks.post.status is PostStatus.RENDERING
    assert logger.entries == []


async def test_the_post_is_locked_before_the_track_and_the_audio_is_read_under_both(
    locks: Locks,
) -> None:
    await choose_fragment(MagicMock(), POST_ID, make_request(), ADMIN_ID)

    assert locks.order == ["post", "track", "audio"]


async def test_a_failed_render_is_started_again_even_with_the_same_bounds(locks: Locks) -> None:
    locks.post = make_post(PostStatus.FAILED, failed_from=PostStatus.RENDERING, preview=None)

    choice = await choose_fragment(MagicMock(), POST_ID, make_request(start_ms=0, end_ms=45_000), ADMIN_ID)

    assert choice == FragmentChoice(POST_ID, FragmentBounds(0, 45_000), None)
    assert locks.post.status is PostStatus.RENDERING
    assert (locks.post.failed_from_status, locks.post.last_error) == (None, None)


async def test_a_post_with_ready_audio_renders_its_first_preview_from_the_chosen_bounds(
    locks: Locks,
) -> None:
    locks.post = make_post(PostStatus.EDITING, preview=None)

    choice = await choose_fragment(MagicMock(), POST_ID, make_request(start_ms=0, end_ms=45_000), ADMIN_ID)

    assert choice == FragmentChoice(POST_ID, FragmentBounds(0, 45_000), None)
    assert locks.post.status is PostStatus.RENDERING


async def test_the_choice_remembers_the_preview_it_replaces(locks: Locks) -> None:
    locks.post = make_post(PostStatus.FAILED, failed_from=PostStatus.RENDERING, preview=PREVIEW)

    choice = await choose_fragment(MagicMock(), POST_ID, make_request(), ADMIN_ID)

    assert choice is not None
    assert choice.superseded_preview == PREVIEW


@pytest.mark.parametrize("missing", ["post", "track"])
async def test_a_missing_row_is_none_and_the_later_locks_are_not_taken(locks: Locks, missing: str) -> None:
    setattr(locks, missing, None)

    assert await choose_fragment(MagicMock(), POST_ID, make_request(), ADMIN_ID) is None
    assert "audio" not in locks.order


@pytest.mark.parametrize(
    ("status", "failed_from"),
    [
        (PostStatus.PUBLISHED, None),
        (PostStatus.RENDERING, None),
        (PostStatus.RESOLVED, None),
        (PostStatus.FAILED, PostStatus.PUBLISHING),
    ],
)
async def test_a_post_that_cannot_be_chosen_is_409_with_a_sentence_and_no_state_name(
    locks: Locks,
    logger: RecordingLogger,
    status: PostStatus,
    failed_from: PostStatus | None,
) -> None:
    locks.post = make_post(status, failed_from=failed_from)

    problem = await refusal_of(make_request())

    assert (problem.status_code, problem.code) == (409, "not_choosable")
    assert status.value not in problem.message.lower()
    assert locks.order == ["post"]
    assert locks.post.status is status
    assert logger.entries == [
        {
            "event": "fragment_render_refused",
            "log_level": "warning",
            "reason": "not_choosable",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
        }
    ]


async def test_a_post_changed_since_the_editor_opened_is_stale_before_the_track_is_locked(
    locks: Locks, logger: RecordingLogger
) -> None:
    stamp = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)

    problem = await refusal_of(make_request(expected_post_updated_at=stamp))

    assert (problem.status_code, problem.code) == (409, "stale_data")
    assert locks.order == ["post"]
    assert logger.entries[0]["reason"] == "stale_data"


async def test_no_audio_of_the_current_recording_is_audio_missing(locks: Locks) -> None:
    locks.asset = None

    problem = await refusal_of(make_request())

    assert (problem.status_code, problem.code) == (409, "audio_missing")
    assert locks.post is not None
    assert locks.post.status is PostStatus.RENDERED


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "numbers"),
    [
        (-1, 5_000, ["-1"]),
        (5_000, 5_000, ["5000"]),
        (60_000, DURATION_MS + 1, [str(DURATION_MS + 1), str(DURATION_MS)]),
        (0, 120_001, ["120000", "120001"]),
    ],
)
async def test_bounds_outside_the_rule_are_422_with_the_sentence_and_the_numbers(
    locks: Locks,
    start_ms: int,
    end_ms: int,
    numbers: list[str],
) -> None:
    problem = await refusal_of(make_request(start_ms=start_ms, end_ms=end_ms))

    assert (problem.status_code, problem.code) == (422, "invalid_fragment")
    assert problem.message.startswith("Фрагмент не підходить: ")
    assert problem.message.endswith(".")
    assert all(number in problem.message for number in numbers)
    assert locks.post is not None
    assert locks.post.status is PostStatus.RENDERED
    assert (locks.post.fragment_start_ms, locks.post.fragment_end_ms) == (0, 45_000)


async def test_a_two_minute_fragment_and_a_whole_short_track_pass_the_rule(locks: Locks) -> None:
    long_choice = await choose_fragment(MagicMock(), POST_ID, make_request(start_ms=10_000, end_ms=130_000), ADMIN_ID)
    locks.post = make_post()
    locks.asset = AudioAsset(duration_ms=7_000)
    short_choice = await choose_fragment(MagicMock(), POST_ID, make_request(start_ms=0, end_ms=7_000), ADMIN_ID)

    assert long_choice is not None and short_choice is not None
    assert short_choice.bounds == FragmentBounds(0, 7_000)


async def test_the_bounds_the_preview_already_shows_render_again(locks: Locks, logger: RecordingLogger) -> None:
    choice = await choose_fragment(MagicMock(), POST_ID, make_request(start_ms=0, end_ms=45_000), ADMIN_ID)

    assert choice == FragmentChoice(POST_ID, FragmentBounds(0, 45_000), PREVIEW)
    assert locks.post is not None
    assert locks.post.status is PostStatus.RENDERING
    assert (locks.post.fragment_start_ms, locks.post.fragment_end_ms) == (0, 45_000)
    assert [entry for entry in logger.entries if entry["event"] == "fragment_render_refused"] == []


def test_the_request_refuses_a_bool_a_float_a_string_an_extra_field_and_a_value_beyond_bigint() -> None:
    for override in (
        {"start_ms": True},
        {"end_ms": 1.5},
        {"start_ms": "0"},
        {"end_ms": BIGINT_MAX + 1},
        {"start_ms": -(BIGINT_MAX + 2)},
        {"extra": 1},
    ):
        with pytest.raises(ValueError):
            make_request(**override)


def test_the_request_accepts_the_bigint_range_so_a_negative_start_gets_its_sentence() -> None:
    request = make_request(start_ms=-5, end_ms=BIGINT_MAX)

    assert (request.start_ms, request.end_ms) == (-5, BIGINT_MAX)


async def test_a_superseded_preview_loses_its_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    remove = AsyncMock()
    monkeypatch.setattr(fragment_choice, "remove_preview_keyboard", remove)
    bot = MagicMock()

    await release_superseded_preview(bot, CHOICE, ADMIN_ID)

    remove.assert_awaited_once_with(bot, *PREVIEW, POST_ID, request_timeout=15)


async def test_without_a_superseded_preview_nothing_is_edited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remove = AsyncMock()
    monkeypatch.setattr(fragment_choice, "remove_preview_keyboard", remove)

    await release_superseded_preview(MagicMock(), FragmentChoice(POST_ID, CHOICE.bounds, None), 1)

    remove.assert_not_awaited()


async def test_a_failed_keyboard_removal_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    monkeypatch.setattr(fragment_choice, "remove_preview_keyboard", AsyncMock(side_effect=TimeoutError("slow")))

    await release_superseded_preview(MagicMock(), CHOICE, ADMIN_ID)

    assert [(entry["event"], entry["post_id"]) for entry in logger.entries] == [
        ("fragment_preview_keyboard_removal_failed", POST_ID)
    ]


async def test_the_render_task_is_enqueued_by_name_and_logged(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(fragment_choice, "dispatch_task", dispatch)
    celery_client = MagicMock()

    await enqueue_render(celery_client, MagicMock(), CHOICE, ADMIN_ID)

    assert dispatch.await_args_list[0].args[:3] == (
        celery_client,
        RENDER_POST_VIDEO_TASK_NAME,
        {"post_id": POST_ID},
    )
    assert logger.entries == [
        {
            "event": "fragment_render_dispatched",
            "log_level": "info",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
            "start_ms": 60_000,
            "end_ms": 105_000,
        }
    ]


async def test_a_refused_task_fails_the_post_at_rendering_and_says_which_retry_to_run(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    monkeypatch.setattr(fragment_choice, "dispatch_task", AsyncMock(return_value=False))
    mark = AsyncMock()
    monkeypatch.setattr(fragment_choice, "mark_dispatch_failure", mark)
    session_factory = MagicMock()

    with pytest.raises(ApiProblem) as refusal:
        await enqueue_render(MagicMock(), session_factory, CHOICE, ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (503, "dispatch_failed")
    assert f"/retry {POST_ID}" in refusal.value.message
    assert PostStatus.RENDERING.value not in refusal.value.message
    mark.assert_awaited_once_with(
        session_factory,
        POST_ID,
        expected_status=PostStatus.RENDERING,
        error=RENDER_DISPATCH_FAILED_ERROR,
    )
    assert [entry["event"] for entry in logger.entries] == ["fragment_render_refused"]


def test_the_producer_is_built_without_touching_the_broker() -> None:
    """so the webapp starts, and serves everything else, while Redis is down."""
    producer = build_celery_app("redis://:secret@127.0.0.1:1/0")

    assert producer.conf.broker_url == "redis://:secret@127.0.0.1:1/0"
    producer.close()


class Journal:
    """one ordered record of the commit, the keyboard and the queue, plus what Telegram and the
    broker were asked while the transaction was open."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.asked_inside_transaction: list[object] = []
        self.bot = MagicMock()
        self.celery_client = MagicMock()
        self.choice: FragmentChoice | None = CHOICE
        self.choose_error: Exception | None = None

    async def choose(
        self,
        session: object,
        post_id: int,
        request: object,
        user_id: int,
    ) -> Any:
        self.order.append("choose")
        self.asked_inside_transaction += [*self.bot.mock_calls, *self.celery_client.mock_calls]
        if self.choose_error is not None:
            raise self.choose_error
        return self.choice

    async def release(
        self,
        bot: object,
        choice: object,
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
    monkeypatch.setattr(posts_module, "choose_fragment", recorded.choose)
    monkeypatch.setattr(posts_module, "release_superseded_preview", recorded.release)
    monkeypatch.setattr(posts_module, "enqueue_render", recorded.enqueue)
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

    anonymous = client.post(PATH, json=BODY)
    stranger = client.post(PATH, json=BODY, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()
    assert journal.order == []
    assert journal.bot.mock_calls == [] and journal.celery_client.mock_calls == []


def test_the_database_commits_then_the_task_is_enqueued_then_the_old_keyboard_goes(
    settings: Settings, journal: Journal
) -> None:
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json() == {
        "post_id": POST_ID,
        "status_label": "Рендериться відео",
        "start_ms": 60_000,
        "end_ms": 105_000,
    }
    assert journal.order == ["choose", "commit", "enqueue", "keyboard"]
    assert journal.asked_inside_transaction == []


def test_a_missing_post_is_404_and_nothing_follows_the_transaction(settings: Settings, journal: Journal) -> None:
    journal.choice = None
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")
    assert journal.order == ["choose", "commit"]


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (database_error("55P03"), 409, "post_busy"),
        (database_error("57014"), 409, "post_busy"),
        (database_error("08006"), 503, "database_unavailable"),
        (ApiProblem(409, "stale_data", "x"), 409, "stale_data"),
        (ApiProblem(422, "invalid_fragment", "x"), 422, "invalid_fragment"),
    ],
)
def test_a_refused_lock_a_database_failure_and_a_refusal_stop_before_the_keyboard_and_the_queue(
    settings: Settings,
    journal: Journal,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    journal.choose_error = error
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (status_code, code)
    assert journal.order == ["choose"]


def test_a_busy_post_answers_with_the_busy_sentence(settings: Settings, journal: Journal) -> None:
    journal.choose_error = database_error("55P03")
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

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

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, code)
    session_factory.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "start_ms": True},
        {**BODY, "end_ms": "105000"},
        {**BODY, "end_ms": BIGINT_MAX + 1},
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

    response = client.post(PATH, json=body, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    session_factory.assert_not_called()


def test_a_refused_queue_fails_the_post_through_the_route(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    monkeypatch.setattr(posts_module, "enqueue_render", fragment_choice.enqueue_render)
    monkeypatch.setattr(fragment_choice, "dispatch_task", AsyncMock(return_value=False))
    mark = AsyncMock()
    monkeypatch.setattr(fragment_choice, "mark_dispatch_failure", mark)
    client, _ = make_client(settings, journal)

    response = client.post(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (503, "dispatch_failed")
    assert f"/retry {POST_ID}" in response.json()["message"]
    assert mark.await_args_list[0].kwargs["expected_status"] is PostStatus.RENDERING
    assert journal.order == ["choose", "commit"]


def test_the_route_logs_the_request_with_the_user_the_post_and_the_bounds(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(posts_module, "logger", recorder)
    client, _ = make_client(settings, journal)

    client.post(PATH, json=BODY, headers=bearer())

    assert recorder.entries == [
        {
            "event": "fragment_render_requested",
            "log_level": "info",
            "user_id": ADMIN_ID,
            "post_id": POST_ID,
            "start_ms": 60_000,
            "end_ms": 105_000,
        }
    ]
