"""`worker.tasks._publish_post()` against a real Postgres: the three
idempotency barriers, the D-38 send/write/commit order, `published_by_user_id`
(D-62), and the failure path. Telegram is mocked whole — `preflight_channel`
and `publish_to_channel` have their own unit suite."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendPhoto
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus, set_post_status
from pisniar.telegram.publish import CANNOT_POST_ERROR, PublishPreflightError
from pisniar.worker import tasks as worker_tasks

pytestmark = pytest.mark.integration

CHANNEL_ID = -1009876543210
PUBLISHER_ID = 222222222
CAPTION = '<a href="https://open.spotify.com/track/x">Spotify</a> &amp; Co'

# `uq_post_draft_message`/`uq_post_channel_message` are real, and the test
# database is shared across the whole session — every reference must differ.
DRAFT_CHAT_ID = 111111111
CHANNEL_MESSAGE_ID_OFFSET = 400_000


@pytest.fixture(autouse=True)
def point_task_at_test_database(
    monkeypatch: pytest.MonkeyPatch, migrated_test_database_url: str
) -> AsyncMock:
    monkeypatch.setattr(worker_tasks.settings, "database_url", migrated_test_database_url)
    monkeypatch.setattr(worker_tasks.settings, "channel_id", CHANNEL_ID)
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=AsyncMock()))
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_tasks, "send_alert", send_alert_mock)
    return send_alert_mock


@pytest.fixture
def preflight_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    chat = MagicMock()
    chat.id = CHANNEL_ID
    chat.username = "test_channel"
    mock = AsyncMock(return_value=chat)
    monkeypatch.setattr(worker_tasks, "preflight_channel", mock)
    return mock


@pytest.fixture
def publish_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stands in for the real send and writes the reference the same way, so
    the task's own transaction order is what is under test here."""

    async def fake_publish(
        bot: object,
        post: Post,
        chat_id: int,
        cover_url: str | None,
        video: object | None = None,
    ) -> None:
        if post.channel_message_id is not None:
            return
        post.channel_chat_id = chat_id
        post.channel_message_id = CHANNEL_MESSAGE_ID_OFFSET + post.id

    mock = AsyncMock(side_effect=fake_publish)
    monkeypatch.setattr(worker_tasks, "publish_to_channel", mock)
    return mock


async def make_publishing_post(
    db_session: AsyncSession,
    *,
    status: PostStatus = PostStatus.PUBLISHING,
    already_published: bool = False,
    cover_url: str | None = "https://cdn.example/cover.jpg",
) -> int:
    marker = uuid.uuid4().hex
    track_url = f"https://open.spotify.com/track/{marker}"
    track = Track(source_url=track_url, source_url_normalized=track_url, cover_url=cover_url)
    db_session.add(track)
    await db_session.flush()

    post = Post(track_id=track.id, created_by_user_id=DRAFT_CHAT_ID, caption=CAPTION)
    db_session.add(post)
    await db_session.flush()

    post.draft_chat_id = DRAFT_CHAT_ID
    post.draft_message_id = post.id
    set_post_status(post, PostStatus.RESOLVING)
    set_post_status(post, PostStatus.RESOLVED)
    if status is not PostStatus.RESOLVED:
        set_post_status(post, status)
    if already_published:
        post.channel_chat_id = CHANNEL_ID
        post.channel_message_id = post.id

    await db_session.commit()
    return post.id


async def test_publishing_writes_the_channel_reference_and_the_publisher(
    db_session: AsyncSession, preflight_mock: AsyncMock, publish_mock: AsyncMock
) -> None:
    post_id = await make_publishing_post(db_session)

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.status is PostStatus.PUBLISHED
    assert post.channel_chat_id == CHANNEL_ID
    assert post.channel_message_id == CHANNEL_MESSAGE_ID_OFFSET + post_id
    assert post.published_at is not None
    # D-62: who approved it, not who submitted the link.
    assert post.published_by_user_id == PUBLISHER_ID
    assert post.created_by_user_id == DRAFT_CHAT_ID


async def test_the_cover_of_the_tracks_row_is_what_gets_published(
    db_session: AsyncSession, preflight_mock: AsyncMock, publish_mock: AsyncMock
) -> None:
    post_id = await make_publishing_post(db_session, cover_url="https://cdn.example/other.jpg")

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    publish_call = publish_mock.await_args
    assert publish_call is not None
    assert publish_call.args[2] == CHANNEL_ID
    assert publish_call.args[3] == "https://cdn.example/other.jpg"


async def test_a_post_that_already_has_a_channel_message_is_never_sent_again(
    db_session: AsyncSession, preflight_mock: AsyncMock, publish_mock: AsyncMock
) -> None:
    """V18 at task level: the guard has to fire before anything reaches
    Telegram, including the preflight."""
    post_id = await make_publishing_post(db_session, already_published=True)

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    publish_mock.assert_not_awaited()
    preflight_mock.assert_not_awaited()

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.channel_message_id == post_id


async def test_a_stale_redelivery_to_a_settled_post_sends_nothing(
    db_session: AsyncSession, preflight_mock: AsyncMock, publish_mock: AsyncMock
) -> None:
    """V19: `task_acks_late=True` guarantees redelivery, and a post no longer
    in `publishing` has already been decided one way or the other."""
    post_id = await make_publishing_post(db_session, status=PostStatus.RESOLVED)

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    publish_mock.assert_not_awaited()
    preflight_mock.assert_not_awaited()

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.status is PostStatus.RESOLVED
    assert post.channel_message_id is None


async def test_a_telegram_failure_fails_the_post_and_alerts(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    preflight_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """V21: `failed` + `last_error`, `channel_message_id` still NULL, and the
    admin told — a silent failure here loses the post with no trace."""
    monkeypatch.setattr(
        worker_tasks,
        "publish_to_channel",
        AsyncMock(
            side_effect=TelegramForbiddenError(
                method=SendPhoto(chat_id=CHANNEL_ID, photo="x"), message="bot was kicked"
            )
        ),
    )
    post_id = await make_publishing_post(db_session)

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.status is PostStatus.FAILED
    assert post.last_error is not None
    assert "403" in post.last_error
    assert post.channel_chat_id is None
    assert post.channel_message_id is None
    assert post.published_at is None

    alert_call = point_task_at_test_database.await_args
    assert alert_call is not None
    assert f"/retry {post_id}" in alert_call.args[2]


async def test_a_preflight_refusal_reaches_the_admin_in_ukrainian(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    publish_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """S3-A6: the admin gets the sentence, never the raw 400 or a traceback."""
    monkeypatch.setattr(
        worker_tasks,
        "preflight_channel",
        AsyncMock(side_effect=PublishPreflightError(CANNOT_POST_ERROR)),
    )
    post_id = await make_publishing_post(db_session)

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    publish_mock.assert_not_awaited()

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.status is PostStatus.FAILED
    assert post.last_error is not None
    assert CANNOT_POST_ERROR in post.last_error
    assert "Traceback" not in post.last_error


async def test_two_concurrent_tasks_publish_exactly_once(
    db_session: AsyncSession, preflight_mock: AsyncMock, publish_mock: AsyncMock
) -> None:
    """S3-A5: the row lock plus the guards under it, which is the only thing
    that deduplicates — two sends would produce two different message ids and
    `uq_post_channel_message` would accept both."""
    post_id = await make_publishing_post(db_session)

    await asyncio.gather(
        worker_tasks._publish_post(post_id, PUBLISHER_ID),
        worker_tasks._publish_post(post_id, PUBLISHER_ID),
    )

    assert publish_mock.await_count == 1

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.status is PostStatus.PUBLISHED


async def test_a_missing_post_is_logged_and_nothing_is_sent(
    preflight_mock: AsyncMock, publish_mock: AsyncMock
) -> None:
    await worker_tasks._publish_post(10_000_000, PUBLISHER_ID)

    publish_mock.assert_not_awaited()
    preflight_mock.assert_not_awaited()


async def test_publication_removes_the_button_and_answers_with_a_link(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    preflight_mock: AsyncMock,
    publish_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """D-66: only the keyboard goes — the channel reference stays, because
    step 5 edits the published caption through it."""
    bot = AsyncMock()
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=bot))
    post_id = await make_publishing_post(db_session)

    await worker_tasks._publish_post(post_id, PUBLISHER_ID)

    bot.edit_message_reply_markup.assert_awaited_once()
    removal = bot.edit_message_reply_markup.await_args
    assert removal is not None
    assert removal.kwargs["chat_id"] == DRAFT_CHAT_ID
    assert removal.kwargs["message_id"] == post_id
    assert removal.kwargs["reply_markup"] is None

    confirmation = point_task_at_test_database.await_args
    assert confirmation is not None
    expected_message_id = CHANNEL_MESSAGE_ID_OFFSET + post_id
    assert f"https://t.me/test_channel/{expected_message_id}" in confirmation.args[2]

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.channel_chat_id == CHANNEL_ID
    assert post.channel_message_id == CHANNEL_MESSAGE_ID_OFFSET + post_id
