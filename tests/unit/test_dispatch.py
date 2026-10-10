"""`dispatch_task` and `mark_dispatch_failure`, shared by the bot and the Mini App: the event
and the fields are the caller's, and a failed write never raises past the caller's alert.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar import dispatch
from pisniar.db.states import PostStatus

POST_ID = 7


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    logger = MagicMock()
    monkeypatch.setattr(dispatch, "logger", logger)
    return logger


class FakeSession:
    def __init__(self, post: Any) -> None:
        self.post = post
        self.get = AsyncMock(return_value=post)
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None


def factory_of(session: FakeSession) -> Any:
    return lambda: session


async def test_dispatch_task_sends_the_task_by_name_with_the_given_kwargs(
    fake_logger: MagicMock,
) -> None:
    celery_client = MagicMock()

    sent = await dispatch.dispatch_task(
        celery_client, "some.task", {"post_id": POST_ID}, "some_dispatch_failed", post_id=POST_ID
    )

    assert sent is True
    celery_client.send_task.assert_called_once_with("some.task", kwargs={"post_id": POST_ID})
    fake_logger.exception.assert_not_called()


async def test_dispatch_task_logs_the_callers_event_and_fields_on_a_broker_failure(
    fake_logger: MagicMock,
) -> None:
    celery_client = MagicMock()
    celery_client.send_task.side_effect = ConnectionError("redis is down")

    sent = await dispatch.dispatch_task(
        celery_client,
        "some.task",
        {"post_id": POST_ID},
        "some_dispatch_failed",
        task_name="some.task",
        post_id=POST_ID,
    )

    assert sent is False
    fake_logger.exception.assert_called_once_with("some_dispatch_failed", task_name="some.task", post_id=POST_ID)


async def test_mark_dispatch_failure_fails_a_post_still_in_the_expected_status() -> None:
    post = MagicMock(status=PostStatus.RENDERING)
    session = FakeSession(post)

    await dispatch.mark_dispatch_failure(
        factory_of(session), POST_ID, expected_status=PostStatus.RENDERING, error="no queue"
    )

    assert post.status is PostStatus.FAILED
    assert post.last_error == "no queue"
    assert post.failed_from_status is PostStatus.RENDERING
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("post_status", [PostStatus.RENDERED, None])
async def test_mark_dispatch_failure_leaves_a_post_that_moved_on_or_vanished(
    post_status: PostStatus | None,
) -> None:
    post = None if post_status is None else MagicMock(status=post_status)
    session = FakeSession(post)

    await dispatch.mark_dispatch_failure(
        factory_of(session), POST_ID, expected_status=PostStatus.RENDERING, error="no queue"
    )

    assert post is None or post.status is post_status


async def test_mark_dispatch_failure_logs_a_failed_write_instead_of_raising(
    fake_logger: MagicMock,
) -> None:
    session = FakeSession(None)
    session.get.side_effect = ConnectionError("postgres is down")

    await dispatch.mark_dispatch_failure(
        factory_of(session), POST_ID, expected_status=PostStatus.RENDERING, error="no queue"
    )

    fake_logger.exception.assert_called_once_with("dispatch_failure_status_write_failed", post_id=POST_ID)
