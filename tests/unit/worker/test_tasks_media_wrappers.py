"""The wrappers both media tasks share: the soft time limit is caught around `run_async()` in
the sync wrapper and, separately, around one stage in the async body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from pisniar.worker import tasks as worker_tasks

POST_ID = 7
SESSION_FACTORY = MagicMock()


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    logger = MagicMock()
    monkeypatch.setattr(worker_tasks, "logger", logger)
    return logger


@pytest.fixture
def fail_media_post(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(worker_tasks, "fail_media_post", mock)
    return mock


def test_run_media_task_runs_the_body_and_does_nothing_else_on_success(
    monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
) -> None:
    run_async = MagicMock()
    monkeypatch.setattr(worker_tasks, "run_async", run_async)
    perform = MagicMock(return_value="body")

    worker_tasks.run_media_task(POST_ID, perform, "some_soft_limit_event")

    perform.assert_called_once_with(POST_ID)
    run_async.assert_called_once_with("body")
    fake_logger.error.assert_not_called()


def test_run_media_task_fails_the_post_by_id_when_the_limit_fires_outside_the_loop(
    monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
) -> None:
    run_async = MagicMock(side_effect=[SoftTimeLimitExceeded(), None])
    fail_by_id = MagicMock(return_value="failing")
    monkeypatch.setattr(worker_tasks, "run_async", run_async)
    monkeypatch.setattr(worker_tasks, "fail_media_post_by_id", fail_by_id)

    worker_tasks.run_media_task(POST_ID, MagicMock(return_value="body"), "some_soft_limit_event")

    fake_logger.error.assert_called_once_with("some_soft_limit_event", post_id=POST_ID)
    fail_by_id.assert_called_once_with(POST_ID, worker_tasks.MEDIA_TASK_TIMEOUT_ERROR)
    assert run_async.call_args_list[1].args == ("failing",)


def test_run_media_task_lets_any_other_error_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_tasks, "run_async", MagicMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        worker_tasks.run_media_task(POST_ID, MagicMock(), "some_soft_limit_event")


async def guard(stage: AsyncMock) -> object | None:
    return await worker_tasks.guard_media_stage(
        SESSION_FACTORY,
        POST_ID,
        stage,
        timeout_event="stage_soft_limit",
        failure_event="stage_failed",
    )


async def test_guard_media_stage_returns_the_stage_result(fake_logger: MagicMock, fail_media_post: AsyncMock) -> None:
    assert await guard(AsyncMock(return_value="outcome")) == "outcome"

    fail_media_post.assert_not_awaited()


async def test_guard_media_stage_reports_a_timeout_as_a_timeout(
    fake_logger: MagicMock, fail_media_post: AsyncMock
) -> None:
    assert await guard(AsyncMock(side_effect=SoftTimeLimitExceeded())) is None

    fake_logger.error.assert_called_once_with("stage_soft_limit", post_id=POST_ID)
    fail_media_post.assert_awaited_once_with(SESSION_FACTORY, POST_ID, worker_tasks.MEDIA_TASK_TIMEOUT_ERROR)


async def test_guard_media_stage_names_the_error_class_for_any_other_failure(
    fake_logger: MagicMock, fail_media_post: AsyncMock
) -> None:
    assert await guard(AsyncMock(side_effect=ValueError("stub"))) is None

    fake_logger.exception.assert_called_once_with("stage_failed", post_id=POST_ID)
    fail_media_post.assert_awaited_once_with(SESSION_FACTORY, POST_ID, "ValueError: stub")
