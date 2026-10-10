"""Publishing the cover without a video and giving up a prepared audio through the Mini App,
against a real Postgres: the locks, the order of the database, the queue and the keyboard, a
refused queue followed by `/retry`, and the cover going out through the worker's own task.
Celery and Telegram are stand-ins; the rows are real.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pisniar.bot.handlers import handle_callback, handle_retry
from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.dispatch import PUBLISH_DISPATCH_FAILED_ERROR
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import issue_session_token
from pisniar.webapp.service import rebuild_caption
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.celery_app import PREPARE_POST_AUDIO_TASK_NAME, PUBLISH_POST_TASK_NAME

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 111
NOW = 1_800_000_000
LOCK_TIMEOUT_MS = 400
CHANNEL_ID = -1009876543210
WEBAPP_URL = "https://pisniar.example.ts.net"
DRAFT_CHAT_ID = ADMIN_ID
PREVIEW_MESSAGE_OFFSET = 6_000_000

PATH_STEPS = {
    "resolved": [PostStatus.RESOLVING, PostStatus.RESOLVED],
    "editing": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.EDITING,
    ],
    "rendering": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.RENDERING,
    ],
    "failed_downloading": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.FAILED,
    ],
    "failed_rendering": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.RENDERING,
        PostStatus.FAILED,
    ],
    "failed_publishing": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.PUBLISHING,
        PostStatus.FAILED,
    ],
}


@dataclass
class FakeBot:
    """records the keyboard edits; `error` makes every one of them fail."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    async def edit_message_reply_markup(self, **arguments: Any) -> bool:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return True


@dataclass
class Environment:
    http: httpx.AsyncClient
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    bot: FakeBot
    queue: MagicMock

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)}"}

    async def load(self, post_id: int) -> Post:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            assert post is not None
            return post

    async def publish_cover(self, post_id: int, stamp: str | None = None) -> httpx.Response:
        if stamp is None:
            detail = await self.http.get(f"/api/posts/{post_id}", headers=self.headers())
            stamp = detail.json()["expected_post_updated_at"]
        return await self.http.post(
            f"/api/posts/{post_id}/publish-cover",
            json={"expected_post_updated_at": stamp},
            headers=self.headers(),
        )

    async def cancel(self, post_id: int, stamp: str | None = None) -> httpx.Response:
        if stamp is None:
            detail = await self.http.get(f"/api/posts/{post_id}", headers=self.headers())
            stamp = detail.json()["expected_post_updated_at"]
        return await self.http.post(
            f"/api/posts/{post_id}/fragment/cancel",
            json={"expected_post_updated_at": stamp},
            headers=self.headers(),
        )

    async def prepare(self, post_id: int) -> httpx.Response:
        return await self.http.post(f"/api/posts/{post_id}/fragment/audio", headers=self.headers())


@pytest_asyncio.fixture
async def environment(migrated_test_database_url: str) -> AsyncIterator[Environment]:
    settings = Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        admin_ids=str(ADMIN_ID),
        alert_chat_id=1,
        channel_id=CHANNEL_ID,
        database_url=migrated_test_database_url,
        spotify_client_id="id",
        spotify_client_secret="secret",
        webapp_public_url=WEBAPP_URL,
    )
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=LOCK_TIMEOUT_MS)
    session_factory = build_session_factory(engine)
    bot = FakeBot()
    queue = MagicMock()
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        bot=cast(Bot, bot),
        celery_client=queue,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield Environment(http, engine, session_factory, bot, queue)
    await engine.dispose()


async def seed_post(
    environment: Environment,
    path: str = "resolved",
    *,
    caption_applied: bool = True,
    preview: bool = False,
) -> tuple[int, int]:
    """a unique track per call; the stored caption is the rebuilt one unless told otherwise."""
    marker = uuid.uuid4().hex
    async with environment.session_factory() as session:
        track = Track(
            source_url=f"test://{marker}",
            source_url_normalized=f"test://{marker}",
            title=f"Покаяння {marker}",
            artists=[{"name": f"Виконавець {marker}", "hashtag": None, "spotify_id": None}],
            cover_url="https://cdn.test/cover.jpg",
            platform_links={
                "youtubeMusic": {
                    "url": "https://music.youtube.com/watch?v=n4bPzdIs_nc",
                    "source": "ytmusic",
                    "match": "confirmed",
                }
            },
        )
        session.add(track)
        await session.flush()

        post = Post(track_id=track.id, created_by_user_id=ADMIN_ID)
        session.add(post)
        await session.flush()
        _, html = rebuild_caption(post, track)
        post.caption = html if caption_applied else "<b>old</b>"
        post.draft_chat_id = DRAFT_CHAT_ID
        post.draft_message_id = post.id
        if preview:
            post.preview_chat_id = DRAFT_CHAT_ID
            post.preview_message_id = post.id + PREVIEW_MESSAGE_OFFSET
            post.preview_params_hash = "a" * 64
        for step in PATH_STEPS[path]:
            set_post_status(post, step, error="boom" if step is PostStatus.FAILED else None)

        post_id, track_id = post.id, track.id
        await session.commit()
    return post_id, track_id


async def test_a_draft_goes_to_publishing_then_the_task_is_queued_then_its_keyboard_goes(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)

    response = await environment.publish_cover(post_id)

    assert response.status_code == 200
    assert response.json() == {"post_id": post_id, "status_label": "Публікується"}
    environment.queue.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME, kwargs={"post_id": post_id, "published_by_user_id": ADMIN_ID}
    )
    assert environment.bot.calls == [
        {
            "chat_id": DRAFT_CHAT_ID,
            "message_id": post_id,
            "reply_markup": None,
            "request_timeout": 15,
        }
    ]
    assert (await environment.load(post_id)).status is PostStatus.PUBLISHING


@pytest.mark.parametrize("path", ["failed_downloading", "failed_publishing"])
async def test_a_failed_draft_without_a_preview_is_published_as_a_cover(
    environment: Environment, path: str
) -> None:
    post_id, _ = await seed_post(environment, path)

    response = await environment.publish_cover(post_id)

    assert response.status_code == 200
    started = await environment.load(post_id)
    assert (started.status, started.failed_from_status, started.last_error) == (
        PostStatus.PUBLISHING,
        None,
        None,
    )


@pytest.mark.parametrize("path", ["editing", "rendering", "failed_rendering"])
async def test_a_post_on_the_video_path_is_refused_and_keeps_its_status(
    environment: Environment, path: str
) -> None:
    post_id, _ = await seed_post(environment, path)
    before = await environment.load(post_id)

    response = await environment.publish_cover(post_id)

    assert (response.status_code, response.json()["code"]) == (409, "not_publishable")
    after = await environment.load(post_id)
    assert (after.status, after.failed_from_status) == (before.status, before.failed_from_status)
    environment.queue.send_task.assert_not_called()
    assert environment.bot.calls == []


async def test_a_post_with_a_preview_is_refused(environment: Environment) -> None:
    post_id, _ = await seed_post(environment, "failed_publishing", preview=True)

    response = await environment.publish_cover(post_id)

    assert (response.status_code, response.json()["code"]) == (409, "not_publishable")
    environment.queue.send_task.assert_not_called()


async def test_an_old_stamp_is_stale_and_changes_nothing(environment: Environment) -> None:
    post_id, _ = await seed_post(environment)

    response = await environment.publish_cover(post_id, "2026-01-01T00:00:00Z")

    assert (response.status_code, response.json()["code"]) == (409, "stale_data")
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()


async def test_a_caption_not_yet_applied_to_the_draft_is_refused_and_nothing_goes_out(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, caption_applied=False)

    response = await environment.publish_cover(post_id)

    assert (response.status_code, response.json()["code"]) == (409, "caption_not_applied")
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()
    assert environment.bot.calls == []


async def test_a_post_held_by_another_transaction_is_busy_at_once_and_unchanged(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)
    stamp = (await environment.load(post_id)).updated_at.isoformat()

    async with environment.session_factory() as holder:
        await holder.get(Post, post_id, with_for_update=True)
        response = await environment.publish_cover(post_id, stamp)
        await holder.rollback()

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()


async def test_a_failing_keyboard_removal_does_not_fail_the_request(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)
    environment.bot.error = TelegramBadRequest(
        method=EditMessageReplyMarkup(chat_id=1, message_id=1), message="gone"
    )

    response = await environment.publish_cover(post_id)

    assert response.status_code == 200
    environment.queue.send_task.assert_called_once()


async def test_a_refused_queue_fails_the_post_at_publishing_and_retry_republishes(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)
    environment.queue.send_task.side_effect = ConnectionError("redis is down")

    response = await environment.publish_cover(post_id)

    assert (response.status_code, response.json()["code"]) == (503, "dispatch_failed")
    assert f"/retry {post_id}" in response.json()["message"]
    failed = await environment.load(post_id)
    assert (failed.status, failed.failed_from_status, failed.last_error) == (
        PostStatus.FAILED,
        PostStatus.PUBLISHING,
        PUBLISH_DISPATCH_FAILED_ERROR,
    )
    assert environment.bot.calls == []

    message = AsyncMock()
    message.text = f"/retry {post_id}"
    message.from_user = SimpleNamespace(id=ADMIN_ID)
    queue = MagicMock()
    await handle_retry(message, environment.session_factory, queue)

    queue.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME, kwargs={"post_id": post_id, "published_by_user_id": ADMIN_ID}
    )
    assert (await environment.load(post_id)).status is PostStatus.PUBLISHING


async def test_the_cover_goes_out_as_a_photo_by_the_admin_and_a_second_request_is_refused(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, migrated_test_database_url: str
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "database_url", migrated_test_database_url)
    monkeypatch.setattr(worker_tasks.settings, "channel_id", CHANNEL_ID)
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(worker_tasks, "send_alert", AsyncMock(return_value=True))
    chat = MagicMock()
    chat.id = CHANNEL_ID
    chat.username = "test_channel"
    monkeypatch.setattr(worker_tasks, "preflight_channel", AsyncMock(return_value=chat))

    async def fake_publish(
        bot: object, post: Post, chat_id: int, cover_url: str | None, video: object | None = None
    ) -> None:
        post.channel_chat_id = chat_id
        post.channel_message_id = 400_000 + post.id
        post.channel_message_kind = "video" if video is not None else "photo"

    publish = AsyncMock(side_effect=fake_publish)
    monkeypatch.setattr(worker_tasks, "publish_to_channel", publish)
    post_id, _ = await seed_post(environment)
    assert (await environment.publish_cover(post_id)).status_code == 200

    await worker_tasks.perform_publish_post(post_id, ADMIN_ID)

    published = await environment.load(post_id)
    assert published.status is PostStatus.PUBLISHED
    assert published.channel_message_kind == "photo"
    assert published.published_by_user_id == ADMIN_ID
    assert publish.await_args is not None
    assert publish.await_args.args[4:] == () or publish.await_args.args[4] is None

    again = await environment.publish_cover(post_id)
    assert (again.status_code, again.json()["code"]) == (409, "not_publishable")


async def test_the_draft_keyboard_carries_no_publication_and_a_tap_on_an_old_button_changes_nothing(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)
    callback = AsyncMock()
    callback.data = f"publish:{post_id}"
    callback.from_user = SimpleNamespace(id=ADMIN_ID)
    callback.message = SimpleNamespace(chat=SimpleNamespace(id=DRAFT_CHAT_ID), message_id=post_id)
    queue = MagicMock()

    await handle_callback(callback, AsyncMock(), environment.session_factory, queue)

    queue.send_task.assert_not_called()
    assert "/edit" in callback.answer.await_args.args[0]
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED


async def test_prepared_audio_goes_back_to_a_draft_and_the_draft_gets_its_keyboard_back(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, "editing")

    response = await environment.cancel(post_id)

    assert response.status_code == 200
    assert response.json() == {"post_id": post_id, "status_label": "Чернетка готова"}
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()
    assert len(environment.bot.calls) == 1
    call = environment.bot.calls[0]
    assert (call["chat_id"], call["message_id"], call["request_timeout"]) == (
        DRAFT_CHAT_ID,
        post_id,
        15,
    )
    texts = [button.text for row in call["reply_markup"].inline_keyboard for button in row]
    assert texts == ["Зробити відео", "Редагувати"]


async def test_a_cancelled_post_can_prepare_its_audio_again(environment: Environment) -> None:
    post_id, _ = await seed_post(environment, "editing")
    assert (await environment.cancel(post_id)).status_code == 200
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED

    prepared = await environment.prepare(post_id)

    assert prepared.status_code == 200
    assert (await environment.load(post_id)).status is PostStatus.DOWNLOADING
    environment.queue.send_task.assert_called_once_with(
        PREPARE_POST_AUDIO_TASK_NAME, kwargs={"post_id": post_id}
    )


async def test_a_cancelled_post_publishes_its_cover(environment: Environment) -> None:
    post_id, _ = await seed_post(environment, "editing")
    assert (await environment.cancel(post_id)).status_code == 200

    cover = await environment.publish_cover(post_id)

    assert cover.status_code == 200
    assert (await environment.load(post_id)).status is PostStatus.PUBLISHING


async def test_an_old_stamp_cannot_cancel_the_preparation(environment: Environment) -> None:
    post_id, _ = await seed_post(environment, "editing")

    response = await environment.cancel(post_id, "2026-01-01T00:00:00Z")

    assert (response.status_code, response.json()["code"]) == (409, "stale_data")
    assert (await environment.load(post_id)).status is PostStatus.EDITING
    assert environment.bot.calls == []


@pytest.mark.parametrize(
    ("path", "preview", "sentence_part"),
    [
        ("resolved", False, "Скасувати можна лише"),
        ("rendering", False, "Рендер уже почався"),
    ],
)
async def test_nothing_but_prepared_audio_can_be_cancelled(
    environment: Environment, path: str, preview: bool, sentence_part: str
) -> None:
    post_id, _ = await seed_post(environment, path, preview=preview)
    before = (await environment.load(post_id)).status

    response = await environment.cancel(post_id)

    assert (response.status_code, response.json()["code"]) == (409, "not_cancellable")
    assert sentence_part in response.json()["message"]
    assert (await environment.load(post_id)).status is before
    assert environment.bot.calls == []


async def test_a_post_held_by_another_transaction_cannot_be_cancelled_and_stays_prepared(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, "editing")

    async with environment.session_factory() as holder:
        await holder.get(Post, post_id, with_for_update=True)
        response = await environment.cancel(post_id)
        await holder.rollback()

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert (await environment.load(post_id)).status is PostStatus.EDITING
    assert environment.bot.calls == []


async def test_a_track_held_by_another_transaction_times_out_as_busy_and_stays_prepared(
    environment: Environment,
) -> None:
    post_id, track_id = await seed_post(environment, "editing")

    async with environment.session_factory() as holder:
        await holder.get(Track, track_id, with_for_update={"key_share": True})
        response = await environment.cancel(post_id)
        await holder.rollback()

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert (await environment.load(post_id)).status is PostStatus.EDITING


async def test_a_failing_keyboard_restore_does_not_fail_the_cancel(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, "editing")
    environment.bot.error = TelegramBadRequest(
        method=EditMessageReplyMarkup(chat_id=1, message_id=1), message="message to edit not found"
    )

    response = await environment.cancel(post_id)

    assert response.status_code == 200
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED


async def test_a_late_download_of_a_cancelled_post_does_nothing(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, migrated_test_database_url: str
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "database_url", migrated_test_database_url)
    download = MagicMock()
    monkeypatch.setattr(worker_tasks, "download_audio_off_transaction", download)
    post_id, _ = await seed_post(environment, "editing")
    assert (await environment.cancel(post_id)).status_code == 200

    await worker_tasks.perform_download_post_audio(post_id, then_render=False)
    await worker_tasks.perform_download_post_audio(post_id, then_render=True)

    download.assert_not_called()
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED


async def test_the_detail_tells_the_editor_what_may_be_done(environment: Environment) -> None:
    draft_id, _ = await seed_post(environment)
    ready_id, _ = await seed_post(environment, "editing")

    draft = (
        await environment.http.get(f"/api/posts/{draft_id}", headers=environment.headers())
    ).json()
    ready = (
        await environment.http.get(f"/api/posts/{ready_id}", headers=environment.headers())
    ).json()

    assert draft["cover_publication"] == {"allowed": True, "reason": None}
    assert draft["fragment"]["cancellable"] is False
    assert ready["cover_publication"]["allowed"] is False
    assert "скасуйте" in ready["cover_publication"]["reason"]
    assert ready["fragment"]["cancellable"] is True
