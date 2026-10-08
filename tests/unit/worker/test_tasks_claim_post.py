"""`lock_post_for_claim`, the three checks both media claims share: the lock, a missing row,
the entry statuses, each with the log event of its own task.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from pisniar.db.models import Post
from pisniar.db.states import PostStatus, set_post_status
from pisniar.worker import tasks as worker_tasks

POST_ID = 7

EVENTS = worker_tasks.ClaimEvents(
    post_locked="locked_event",
    post_not_found="not_found_event",
    settled="settled_event",
)
ENTRY_STATUSES = frozenset({PostStatus.RESOLVED})


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    logger = MagicMock()
    monkeypatch.setattr(worker_tasks, "logger", logger)
    return logger


def make_post(status: PostStatus, preview_message_id: int | None = None) -> Post:
    post = Post(track_id=1, created_by_user_id=111)
    set_post_status(post, PostStatus.RESOLVING)
    set_post_status(post, PostStatus.RESOLVED)
    if status is not PostStatus.RESOLVED:
        set_post_status(post, status)
    post.preview_message_id = preview_message_id
    return post


async def test_an_entry_status_without_a_preview_returns_the_locked_post(
    fake_logger: MagicMock,
) -> None:
    post = make_post(PostStatus.RESOLVED)
    session = AsyncMock()
    session.get.return_value = post

    claimed = await worker_tasks.lock_post_for_claim(session, POST_ID, ENTRY_STATUSES, EVENTS)

    assert claimed is post
    session.get.assert_awaited_once_with(Post, POST_ID, with_for_update=True)
    fake_logger.error.assert_not_called()
    fake_logger.info.assert_not_called()


async def test_a_lock_that_cannot_be_taken_rolls_back_and_logs_the_error(
    fake_logger: MagicMock,
) -> None:
    session = AsyncMock()
    session.get.side_effect = DBAPIError("SELECT ... FOR UPDATE", {}, Exception("lock timeout"))

    claimed = await worker_tasks.lock_post_for_claim(session, POST_ID, ENTRY_STATUSES, EVENTS)

    assert claimed is None
    session.rollback.assert_awaited_once()
    event = fake_logger.error.call_args
    assert event.args == ("locked_event",)
    assert event.kwargs["post_id"] == POST_ID
    assert "lock timeout" in event.kwargs["error"]


async def test_a_missing_post_logs_the_not_found_event(fake_logger: MagicMock) -> None:
    session = AsyncMock()
    session.get.return_value = None

    claimed = await worker_tasks.lock_post_for_claim(session, POST_ID, ENTRY_STATUSES, EVENTS)

    assert claimed is None
    fake_logger.error.assert_called_once_with("not_found_event", post_id=POST_ID)


async def test_a_status_outside_the_entries_logs_the_settled_event(
    fake_logger: MagicMock,
) -> None:
    session = AsyncMock()
    session.get.return_value = make_post(PostStatus.DOWNLOADING)

    claimed = await worker_tasks.lock_post_for_claim(session, POST_ID, ENTRY_STATUSES, EVENTS)

    assert claimed is None
    fake_logger.info.assert_called_once_with("settled_event", post_id=POST_ID, status="downloading")


async def test_a_previewed_post_in_an_entry_status_is_still_locked_and_returned(
    fake_logger: MagicMock,
) -> None:
    """whether a preview refuses the claim is each task's own rule, not the shared lock's."""
    post = make_post(PostStatus.RESOLVED, preview_message_id=99)
    session = AsyncMock()
    session.get.return_value = post

    claimed = await worker_tasks.lock_post_for_claim(session, POST_ID, ENTRY_STATUSES, EVENTS)

    assert claimed is post
    fake_logger.info.assert_not_called()


def test_each_media_task_has_its_own_event_names() -> None:
    download = worker_tasks.DOWNLOAD_CLAIM_EVENTS
    render = worker_tasks.RENDER_CLAIM_EVENTS

    assert download.post_locked == "download_post_audio_post_locked"
    assert len({*vars(download).values(), *vars(render).values()}) == 6
