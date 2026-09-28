"""The publish trigger against a real Postgres: the inline button, the double-tap
guard, the dispatch-failure bookkeeping, and `/retry`'s split between re-resolving and
re-publishing. Celery and Telegram are mocked: this file is about which row moves
where, and what gets dispatched.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.bot import handlers
from pisniar.bot.handlers import (
    PUBLISH_DISPATCH_FAILED_ERROR,
    UNKNOWN_CALLBACK_ANSWER,
    handle_callback,
    handle_retry,
)
from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.worker.celery_app import (
    DOWNLOAD_POST_AUDIO_TASK_NAME,
    PUBLISH_POST_TASK_NAME,
    RENDER_POST_VIDEO_TASK_NAME,
    RESOLVE_POST_TASK_NAME,
)

pytestmark = pytest.mark.integration

CHANNEL_ID = -1009876543210
SUBMITTER_ID = 111
PUBLISHER_ID = 222

# `uq_post_draft_message`/`uq_post_channel_message` are real and the database is
# shared for the whole session, so every reference derives from the post id.
DRAFT_CHAT_ID = 111
# `uq_post_preview_message` is real too, so the preview reference has to be
# distinct from the draft's within the same chat.
PREVIEW_MESSAGE_OFFSET = 1_000_000


def status_ladder(status: PostStatus, *, with_draft: bool) -> list[PostStatus]:
    """There is no `RESOLVED -> FAILED` edge, so a `failed` post with a draft got
    there through publishing, and one without a draft failed during the resolve."""
    if status is PostStatus.RESOLVING:
        return [PostStatus.RESOLVING]
    if status is PostStatus.PUBLISHING:
        return [PostStatus.RESOLVING, PostStatus.RESOLVED, PostStatus.PUBLISHING]
    if status is PostStatus.FAILED and not with_draft:
        return [PostStatus.RESOLVING, PostStatus.FAILED]

    ladder = [PostStatus.RESOLVING, PostStatus.RESOLVED]
    if status is PostStatus.RESOLVED:
        return ladder
    if status in MEDIA_LADDER:
        return [*ladder, *MEDIA_LADDER[status]]
    return [*ladder, PostStatus.PUBLISHING, status]


# The real path through the media chain, so the `failed_from_status` a test
# reads is the one `set_post_status` actually wrote on the way there.
MEDIA_LADDER = {
    PostStatus.DOWNLOADING: [PostStatus.DOWNLOADING],
    PostStatus.DOWNLOADED: [PostStatus.DOWNLOADING, PostStatus.DOWNLOADED],
    PostStatus.RENDERING: [
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.RENDERING,
    ],
    PostStatus.RENDERED: [
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.RENDERING,
        PostStatus.RENDERED,
    ],
}


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
    with_preview: bool = False,
    failed_from: PostStatus | None = None,
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

        if with_preview:
            post.preview_chat_id = DRAFT_CHAT_ID
            post.preview_message_id = post.id + PREVIEW_MESSAGE_OFFSET

        ladder = status_ladder(status, with_draft=with_draft)
        if failed_from is not None:
            ladder = [*MEDIA_LADDER.get(failed_from, [failed_from]), PostStatus.FAILED]
            ladder = [PostStatus.RESOLVING, PostStatus.RESOLVED, *ladder]

        for step in ladder:
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
    `capture_logs()` is defeated by an already-cached bound logger."""
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

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.PUBLISHING

    # Whoever tapped the button, not whoever submitted the link.
    celery_client.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME,
        kwargs={"post_id": post_id, "published_by_user_id": PUBLISHER_ID},
    )


async def test_the_button_is_removed_before_the_task_is_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The keyboard goes first so a second tap has nothing left to press; the
    status transition above is what actually refuses one."""
    post_id = await make_post_with_draft(session_factory)
    callback = make_callback(f"publish:{post_id}")
    order: list[str] = []

    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = lambda **kwargs: order.append("keyboard")
    celery_client = MagicMock()
    celery_client.send_task.side_effect = lambda *args, **kwargs: order.append("task")

    await handle_callback(callback, bot, session_factory, celery_client)

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
    """A second copy in a public channel is the most expensive failure there is,
    and this is the cheapest place to stop it."""
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.PUBLISHED, already_published=True
    )
    callback = make_callback(f"publish:{post_id}")
    celery_client = MagicMock()

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

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

    await handle_callback(
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

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "не знайдено" in callback.answer.await_args.args[0]


async def test_malformed_callback_data_never_reaches_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    callback = make_callback("publish:not-an-id")
    bot = AsyncMock()
    celery_client = MagicMock()

    await handle_callback(callback, bot, session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    bot.edit_message_reply_markup.assert_not_awaited()
    assert callback.answer.await_args.args[0] == UNKNOWN_CALLBACK_ANSWER


async def test_a_failed_publish_dispatch_leaves_the_post_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The post is already committed as `publishing`, and no task exists to move
    it on."""
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_callback(
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

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "уже публікується" in callback.answer.await_args.args[0]


async def test_two_simultaneous_taps_dispatch_only_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()

    await asyncio.gather(
        handle_callback(
            make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
        ),
        handle_callback(
            make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
        ),
    )

    assert celery_client.send_task.call_count == 1


async def test_retry_after_a_failed_publish_republishes_instead_of_resolving(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a recorded stage, a delivered draft on a `failed` post means the
    failure came later, on publication."""
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
    """Without these lines the logs cannot say who tapped, when, or whether a task
    was ever queued."""
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    post_id = await make_post_with_draft(session_factory)

    await handle_callback(
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

    await handle_callback(
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

    await handle_callback(
        make_callback(f"publish:{post_id}"), AsyncMock(), session_factory, celery_client
    )

    assert logged_fields(fake_logger.warning, "publish_refused") == [
        {"reason": "dispatch_failed", "user_id": PUBLISHER_ID, "post_id": post_id}
    ]


async def test_retry_logs_which_of_the_two_branches_it_took(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "reclaimed_stale_stage": None,
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


async def test_the_render_button_moves_the_post_to_downloading_and_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The only trigger for the media chain: nothing else starts it."""
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()

    await handle_callback(
        make_callback(f"render:{post_id}"), AsyncMock(), session_factory, celery_client
    )

    celery_client.send_task.assert_called_once_with(
        DOWNLOAD_POST_AUDIO_TASK_NAME, kwargs={"post_id": post_id}
    )

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.DOWNLOADING


async def test_the_render_keyboard_goes_before_the_task_is_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Database first, keyboard second, dispatch last: removing the keyboard any
    earlier would strand a post the database then declined to move."""
    post_id = await make_post_with_draft(session_factory)
    order: list[str] = []

    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = lambda **kwargs: order.append("keyboard")
    celery_client = MagicMock()
    celery_client.send_task.side_effect = lambda *args, **kwargs: order.append("task")

    await handle_callback(make_callback(f"render:{post_id}"), bot, session_factory, celery_client)

    assert order == ["keyboard", "task"]
    assert bot.edit_message_reply_markup.await_args.kwargs == {
        "chat_id": DRAFT_CHAT_ID,
        "message_id": post_id,
        "reply_markup": None,
    }


async def test_two_simultaneous_render_taps_dispatch_only_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second download is minutes of CPU and a second copy on the volume."""
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()

    await asyncio.gather(
        handle_callback(
            make_callback(f"render:{post_id}"), AsyncMock(), session_factory, celery_client
        ),
        handle_callback(
            make_callback(f"render:{post_id}"), AsyncMock(), session_factory, celery_client
        ),
    )

    assert celery_client.send_task.call_count == 1


async def test_a_render_tap_on_an_already_published_post_dispatches_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.PUBLISHED, already_published=True
    )
    callback = make_callback(f"render:{post_id}")
    celery_client = MagicMock()

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "опубліковано" in callback.answer.await_args.args[0]


async def test_a_render_tap_while_rendering_dispatches_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(session_factory, status=PostStatus.RENDERING)
    callback = make_callback(f"render:{post_id}")
    celery_client = MagicMock()

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "відео" in callback.answer.await_args.args[0]


async def test_a_failed_render_dispatch_leaves_the_post_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`downloading` is already committed, and no task exists to move it on."""
    post_id = await make_post_with_draft(session_factory)
    celery_client = MagicMock()
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_callback(
        make_callback(f"render:{post_id}"), AsyncMock(), session_factory, celery_client
    )

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.FAILED
    assert post.last_error == handlers.DOWNLOAD_DISPATCH_FAILED_ERROR
    assert post.failed_from_status is PostStatus.DOWNLOADING


async def test_a_publish_tap_during_the_render_says_what_is_actually_happening(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`RESOLVED -> PUBLISHING` stays legal, so the draft's publish button is live
    during the render; a generic refusal would name a state and read like a breakage."""
    post_id = await make_post_with_draft(session_factory, status=PostStatus.DOWNLOADING)
    callback = make_callback(f"publish:{post_id}")
    celery_client = MagicMock()

    await handle_callback(callback, AsyncMock(), session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    answer = callback.answer.await_args.args[0]
    assert "відео" in answer
    assert "downloading" not in answer

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.DOWNLOADING


async def test_retry_after_a_failed_download_downloads_instead_of_publishing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The post has a draft, so routing by the draft alone would publish the cover
    photo of a post whose video never rendered; the stage comes from the column."""
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.FAILED, failed_from=PostStatus.DOWNLOADING
    )
    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    celery_client.send_task.assert_called_once_with(
        DOWNLOAD_POST_AUDIO_TASK_NAME, kwargs={"post_id": post_id}
    )
    assert "завантаження" in message.answer.await_args.args[0]

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.DOWNLOADING


async def test_retry_after_a_failed_render_renders_instead_of_publishing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The admin must read about a repeated render, not a publication."""
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.FAILED, failed_from=PostStatus.RENDERING
    )
    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    celery_client.send_task.assert_called_once_with(
        RENDER_POST_VIDEO_TASK_NAME, kwargs={"post_id": post_id}
    )
    assert "рендер" in message.answer.await_args.args[0]

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.RENDERING


async def test_retry_after_a_failed_publication_still_republishes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.FAILED, failed_from=PostStatus.PUBLISHING
    )
    celery_client = MagicMock()

    await handle_retry(make_message(f"/retry {post_id}"), session_factory, celery_client)

    celery_client.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME,
        kwargs={"post_id": post_id, "published_by_user_id": PUBLISHER_ID},
    )


async def test_a_live_render_refuses_retry_with_its_own_sentence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_post_with_draft(session_factory, status=PostStatus.RENDERING)
    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    celery_client.send_task.assert_not_called()
    assert "рендериться" in message.answer.await_args.args[0]


async def test_a_stuck_render_older_than_its_limit_can_be_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Otherwise a `rendering` post whose worker died could leave that state only
    through a manual UPDATE in psql."""
    post_id = await make_post_with_draft(session_factory, status=PostStatus.RENDERING)
    stale_age = handlers.settings.render_task_time_limit_seconds + 60
    async with session_factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        post.updated_at = datetime.now(UTC) - timedelta(seconds=stale_age)
        await session.commit()

    celery_client = MagicMock()
    await handle_retry(make_message(f"/retry {post_id}"), session_factory, celery_client)

    celery_client.send_task.assert_called_once_with(
        RENDER_POST_VIDEO_TASK_NAME, kwargs={"post_id": post_id}
    )

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.RENDERING
    # The self-edge changes no column, so the ORM `onupdate` never fires and
    # every later /retry would pass the staleness guard too.
    assert (datetime.now(UTC) - post.updated_at).total_seconds() < stale_age


async def test_a_publish_tap_under_a_preview_clears_the_preview_keyboard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Once a preview exists it is the message carrying the button — the text
    draft lost its keyboard when the preview arrived."""
    post_id = await make_post_with_draft(
        session_factory, status=PostStatus.RENDERED, with_preview=True
    )
    bot = AsyncMock()

    await handle_callback(make_callback(f"publish:{post_id}"), bot, session_factory, MagicMock())

    assert bot.edit_message_reply_markup.await_args.kwargs == {
        "chat_id": DRAFT_CHAT_ID,
        "message_id": post_id + PREVIEW_MESSAGE_OFFSET,
        "reply_markup": None,
    }

    post = await load_post(session_factory, post_id)
    assert post.status is PostStatus.PUBLISHING
