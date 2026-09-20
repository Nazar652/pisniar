"""The publish trigger against a real Postgres: the inline button (D-46),
the double-tap guard, the dispatch-failure bookkeeping, and `/retry`'s split
between re-resolving and re-publishing (D-47). Celery and Telegram are mocked
— this file is about which row moves where, and what gets dispatched."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.bot import handlers
from pisniar.bot.handlers import (
    PUBLISH_DISPATCH_FAILED_ERROR,
    UNKNOWN_CALLBACK_ANSWER,
    handle_publish_callback,
    handle_retry,
)
from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.worker.celery_app import PUBLISH_POST_TASK_NAME, RESOLVE_POST_TASK_NAME

pytestmark = pytest.mark.integration

CHANNEL_ID = -1009876543210
SUBMITTER_ID = 111
PUBLISHER_ID = 222

# `uq_post_draft_message`/`uq_post_channel_message` are real indexes and the
# test database is shared for the whole session, so every reference derives
# from the post id rather than from a constant.
DRAFT_CHAT_ID = 111


def status_ladder(status: PostStatus, *, with_draft: bool) -> list[PostStatus]:
    """The real path to each state — there is no `RESOLVED -> FAILED` edge on
    purpose, so a `failed` post that has a draft got there through publishing,
    and one without a draft failed during the resolve itself."""
    if status is PostStatus.RESOLVING:
        return [PostStatus.RESOLVING]
    if status is PostStatus.PUBLISHING:
        return [PostStatus.RESOLVING, PostStatus.RESOLVED, PostStatus.PUBLISHING]
    if status is PostStatus.FAILED and not with_draft:
        return [PostStatus.RESOLVING, PostStatus.FAILED]

    ladder = [PostStatus.RESOLVING, PostStatus.RESOLVED]
    if status is PostStatus.RESOLVED:
        return ladder
    return [*ladder, PostStatus.PUBLISHING, status]


@pytest.fixture
async def session_factory(migrated_test_database_url: str):
    engine = build_engine(migrated_test_database_url)
    factory = build_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


async def make_post_with_draft(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: PostStatus = PostStatus.RESOLVED,
    with_draft: bool = True,
    already_published: bool = False,
) -> int:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.flush()

        post = Post(track_id=track.id, created_by_user_id=SUBMITTER_ID, caption="caption")
        session.add(post)
        await session.flush()

        if with_draft:
            post.draft_chat_id = DRAFT_CHAT_ID
            post.draft_message_id = post.id
        if already_published:
            post.channel_chat_id = CHANNEL_ID
            post.channel_message_id = post.id

        # The real path to each state: `RESOLVED -> FAILED` has no edge on
        # purpose, so a `failed` post with a draft got there through publishing.
        for step in status_ladder(status, with_draft=with_draft):
            set_post_status(post, step, error="boom" if step is PostStatus.FAILED else None)

        post_id = post.id
        await session.commit()
    return post_id


def make_callback(data: str | None, user_id: int = PUBLISHER_ID) -> AsyncMock:
    callback = AsyncMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=user_id)
    return callback


def make_message(text: str, user_id: int = PUBLISHER_ID) -> AsyncMock:
    message = AsyncMock()
    message.text = text
    message.from_user = SimpleNamespace(id=user_id)
    return message


def logged_fields(level: MagicMock, event: str) -> list[Mapping[str, Any]]:
    """The module logger is replaced rather than captured through structlog:
    `capture_logs()` is defeated by an already-cached bound logger (V12)."""
    return [call.kwargs for call in level.call_args_list if call.args[:1] == (event,)]


async def load_post(session_factory: async_sessionmaker[AsyncSession], post_id: int) -> Post:
    async with session_factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        return post


async def test_the_button_moves_the_post_to_publishing_and_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(session_factory)
    callback = make_callback(f"publish:{post_id}")
    celery_client = MagicMock()

    await handle_publish_callback(callback, AsyncMock(), session_factory, celery_client)

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.PUBLISHING

    # D-62: whoever tapped the button, not whoever submitted the link.
    celery_client.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME,
        kwargs={"post_id": post_id, "published_by_user_id": PUBLISHER_ID},
    )


async def test_the_button_is_removed_before_the_task_is_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """S3-R8: the keyboard goes first so a second tap has nothing left to
    press; the status transition above is what actually refuses one."""
    post_id = await make_post_with_draft(session_factory)
    callback = make_callback(f"publish:{post_id}")
    order: list[str] = []

    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = lambda **kwargs: order.append("keyboard")
    celery_client = MagicMock()
    celery_client.send_task.side_effect = lambda *args, **kwargs: order.append("task")

    await handle_publish_callback(callback, bot, session_factory, celery_client)

    assert order == ["keyboard", "task"]
    removal = bot.edit_message_reply_markup.await_args
    assert removal is not None
    assert removal.kwargs == {
        "chat_id": DRAFT_CHAT_ID,
        "message_id": post_id,
        "reply_markup": None,
    }


async def test_a_tap_on_an_already_published_post_dispatches_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """S3-A3: the most expensive failure of this step is a second copy in a
    public channel, and the cheapest place to stop it is here."""
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.PUBLISHED, already_published=True
    )
    callback = make_callback(f"publish:{post_id}")
    celery_client = MagicMock()

    await handle_publish_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "уже опубліковано" in callback.answer.await_args.args[0]

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.PUBLISHED
    assert post.channel_message_id == post_id


async def test_a_tap_on_a_post_that_cannot_publish_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(session_factory, status=PostStatus.RESOLVING)
    celery_client = MagicMock()

    await handle_publish_callback(
        make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
    )

    celery_client.send_task.assert_not_called()

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.RESOLVING


async def test_a_tap_for_an_unknown_post_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    callback = make_callback("publish:10000000")
    celery_client = MagicMock()

    await handle_publish_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "не знайдено" in callback.answer.await_args.args[0]


async def test_malformed_callback_data_never_reaches_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    callback = make_callback("publish:not-an-id")
    bot = AsyncMock()
    celery_client = MagicMock()

    await handle_publish_callback(callback, bot, session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    bot.edit_message_reply_markup.assert_not_awaited()
    assert callback.answer.await_args.args[0] == UNKNOWN_CALLBACK_ANSWER


async def test_a_failed_publish_dispatch_leaves_the_post_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """D-53 on the publish side: the post is already committed as
    `publishing`, and no task exists to move it on."""
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_publish_callback(
        make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
    )

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.FAILED
    assert post.last_error == PUBLISH_DISPATCH_FAILED_ERROR


async def test_a_tap_on_a_post_already_publishing_dispatches_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `publishing` self-edge is for Celery redelivery, not for a second
    tap — `set_post_status` alone accepts it and would dispatch twice."""
    post_id = await make_post_with_draft(session_factory, status=PostStatus.PUBLISHING)
    callback = make_callback(f"publish:{post_id}")
    celery_client = MagicMock()

    await handle_publish_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "уже публікується" in callback.answer.await_args.args[0]


async def test_two_simultaneous_taps_dispatch_only_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()

    await asyncio.gather(
        handle_publish_callback(
            make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
        ),
        handle_publish_callback(
            make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
        ),
    )

    assert celery_client.send_task.call_count == 1


async def test_retry_after_a_failed_publish_republishes_instead_of_resolving(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The defect D-47 closed without a migration: `draft_message_id IS NOT
    NULL` on a `failed` post means the draft was delivered, so the failure
    came later — on publication. `/retry` used to silently re-resolve."""
    post_id = await make_post_with_draft(session_factory, status=PostStatus.FAILED)
    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    celery_client.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME,
        kwargs={"post_id": post_id, "published_by_user_id": PUBLISHER_ID},
    )
    assert "публікацію" in message.answer.await_args.args[0]

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.PUBLISHING


async def test_the_tap_and_its_dispatch_are_both_logged_with_the_user(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-70: found by manual acceptance — the logs said nothing about who
    tapped, when, or whether a task was ever queued."""
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    post_id = await make_post_with_draft(session_factory)

    await handle_publish_callback(
        make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, MagicMock()
    )

    assert logged_fields(fake_logger.info, "publish_button_tapped") == [
        {"user_id": PUBLISHER_ID, "post_id": post_id}
    ]
    assert logged_fields(fake_logger.info, "publish_task_dispatched") == [
        {"user_id": PUBLISHER_ID, "post_id": post_id}
    ]
    assert logged_fields(fake_logger.warning, "publish_refused") == []


async def test_a_refused_tap_is_logged_with_its_own_reason(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.PUBLISHED, already_published=True
    )

    await handle_publish_callback(
        make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, MagicMock()
    )

    assert logged_fields(fake_logger.info, "publish_button_tapped") != []
    assert logged_fields(fake_logger.warning, "publish_refused") == [
        {"reason": "already_published", "user_id": PUBLISHER_ID, "post_id": post_id}
    ]
    assert logged_fields(fake_logger.info, "publish_task_dispatched") == []


async def test_a_failed_publish_dispatch_is_logged_as_a_refusal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_publish_callback(
        make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
    )

    assert logged_fields(fake_logger.warning, "publish_refused") == [
        {"reason": "dispatch_failed", "user_id": PUBLISHER_ID, "post_id": post_id}
    ]


async def test_retry_logs_which_of_the_two_branches_it_took(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction D-47 introduced — re-resolve or re-publish — is exactly
    what was impossible to read back out of the logs."""
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    published_post_id = await make_post_with_draft(session_factory, status=PostStatus.FAILED)
    resolved_post_id = await make_post_with_draft(
        session_factory, status=PostStatus.FAILED, with_draft=False
    )
    celery_client = MagicMock()

    await handle_retry(make_message(f"/retry {published_post_id}"), session_factory, celery_client)
    await handle_retry(make_message(f"/retry {resolved_post_id}"), session_factory, celery_client)

    branches = [fields["branch"] for fields in logged_fields(fake_logger.info, "retry_dispatched")]
    assert branches == ["republish", "resolve"]

    selections = logged_fields(fake_logger.info, "retry_branch_selected")
    assert selections[0] == {
        "branch": "republish",
        "user_id": PUBLISHER_ID,
        "post_id": published_post_id,
        "reclaimed_stale_resolving": False,
    }


async def test_a_refused_retry_names_the_state_that_refused_it(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    post_id = await make_post_with_draft(session_factory, status=PostStatus.RESOLVED)

    await handle_retry(make_message(f"/retry {post_id}"), session_factory, MagicMock())

    assert logged_fields(fake_logger.info, "retry_requested") == [
        {"user_id": PUBLISHER_ID, "post_id": post_id}
    ]
    assert logged_fields(fake_logger.warning, "retry_refused") == [
        {
            "reason": "illegal_status",
            "status": "resolved",
            "user_id": PUBLISHER_ID,
            "post_id": post_id,
        }
    ]
    assert logged_fields(fake_logger.info, "retry_branch_selected") == []


async def test_retry_before_any_draft_still_resolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other side of that branch: a resolve that failed before delivery
    has no draft, and must not be turned into a publication."""
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.FAILED, with_draft=False
    )
    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    celery_client.send_task.assert_called_once_with(
        RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id}
    )

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.RESOLVING
