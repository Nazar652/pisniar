"""Preparing the audio before the first fragment choice against a real Postgres: the locks the request takes, the status
it writes, a refused queue followed by `/retry`, a failing keyboard, and the whole chain up to a first preview rendered
from the chosen bounds. Celery, Telegram and ffmpeg are stand-ins; the rows are real."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import EditMessageReplyMarkup
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_render_lyrics import COVER_URL, StubRenderer

from pisniar.bot.handlers import handle_callback, handle_retry
from pisniar.config import Settings
from pisniar.db.models import AudioAsset, Lyric, Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.dispatch import DOWNLOAD_DISPATCH_FAILED_ERROR
from pisniar.webapp.app import create_webapp
from pisniar.webapp.audio_preparation import prepare_audio
from pisniar.webapp.auth import issue_session_token
from pisniar.webapp.database import is_lock_failure
from pisniar.webapp.errors import ApiProblem
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.celery_app import (
    DOWNLOAD_POST_AUDIO_TASK_NAME,
    PREPARE_POST_AUDIO_TASK_NAME,
    RENDER_POST_VIDEO_TASK_NAME,
)

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 111
NOW = 1_800_000_000
LOCK_TIMEOUT_MS = 400
VIDEO_ID = "n4bPzdIs_nc"
DURATION_MS = 212_000
DRAFT_CHAT_ID = ADMIN_ID
PREVIEW_MESSAGE_OFFSET = 5_000_000

PATH_STEPS = {
    "resolved": [PostStatus.RESOLVING, PostStatus.RESOLVED],
    "downloading": [PostStatus.RESOLVING, PostStatus.RESOLVED, PostStatus.DOWNLOADING],
    "editing": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.EDITING,
    ],
    "failed_downloading": [
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.FAILED,
    ],
}


@dataclass
class FakeBot:
    """records the keyboard removals; `error` makes every one of them fail."""

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

    async def start(self, post_id: int) -> httpx.Response:
        return await self.http.post(f"/api/posts/{post_id}/fragment/audio", headers=self.headers())

    async def load(self, post_id: int) -> Post:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            assert post is not None
            return post


@pytest_asyncio.fixture
async def environment(migrated_test_database_url: str) -> AsyncIterator[Environment]:
    settings = Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        admin_ids=str(ADMIN_ID),
        alert_chat_id=1,
        channel_id=-1,
        database_url=migrated_test_database_url,
        spotify_client_id="id",
        spotify_client_secret="secret",
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
    music_link: bool = True,
) -> tuple[int, int]:
    """a unique artist and title per call; returns the post id and the track id."""
    marker = uuid.uuid4().hex
    platform_links = (
        {
            "youtubeMusic": {
                "url": f"https://music.youtube.com/watch?v={VIDEO_ID}",
                "source": "ytmusic",
                "match": "confirmed",
            }
        }
        if music_link
        else {}
    )
    async with environment.session_factory() as session:
        track = Track(
            source_url=f"test://{marker}",
            source_url_normalized=f"test://{marker}",
            title=f"Покаяння {marker}",
            artists=[{"name": f"Виконавець {marker}", "hashtag": None, "spotify_id": None}],
            duration_ms=DURATION_MS,
            cover_url=COVER_URL,
            platform_links=platform_links,
        )
        session.add(track)
        await session.flush()

        post = Post(track_id=track.id, created_by_user_id=ADMIN_ID, caption="caption")
        session.add(post)
        await session.flush()
        post.draft_chat_id = DRAFT_CHAT_ID
        post.draft_message_id = post.id
        for step in PATH_STEPS[path]:
            set_post_status(post, step, error="boom" if step is PostStatus.FAILED else None)

        post_id, track_id = post.id, track.id
        await session.commit()
    return post_id, track_id


async def test_the_request_holds_the_post_and_the_track_but_lets_a_new_post_of_the_track_in(
    environment: Environment,
) -> None:
    post_id, track_id = await seed_post(environment)

    async with environment.session_factory() as writer, environment.session_factory() as other:
        preparation = await prepare_audio(writer, post_id, ADMIN_ID)
        assert preparation is not None

        with pytest.raises(DBAPIError) as post_lock:
            await other.execute(text("SELECT id FROM post WHERE id = :id FOR UPDATE NOWAIT"), {"id": post_id})
        await other.rollback()
        with pytest.raises(DBAPIError) as track_lock:
            await other.execute(
                text("SELECT id FROM track WHERE id = :id FOR NO KEY UPDATE NOWAIT"),
                {"id": track_id},
            )
        await other.rollback()
        key_share = await other.execute(
            text("SELECT id FROM track WHERE id = :id FOR KEY SHARE NOWAIT"), {"id": track_id}
        )
        await other.rollback()
        await writer.commit()

    assert is_lock_failure(post_lock.value) and is_lock_failure(track_lock.value)
    assert key_share.scalar_one() == track_id
    assert (await environment.load(post_id)).status is PostStatus.DOWNLOADING
    assert (preparation.post_id, preparation.draft) == (post_id, (DRAFT_CHAT_ID, post_id))


async def test_a_post_held_by_another_transaction_is_busy_at_once_and_unchanged(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)

    async with environment.session_factory() as holder:
        await holder.get(Post, post_id, with_for_update=True)

        response = await environment.start(post_id)
        await holder.rollback()

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()
    assert environment.bot.calls == []


async def test_a_track_held_by_another_transaction_times_out_as_busy_and_changes_nothing(
    environment: Environment,
) -> None:
    post_id, track_id = await seed_post(environment)

    async with environment.session_factory() as holder:
        await holder.get(Track, track_id, with_for_update={"key_share": True})

        response = await environment.start(post_id)
        await holder.rollback()

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()
    assert environment.bot.calls == []


@pytest.mark.parametrize(("path", "code"), [("editing", "not_preparable"), ("downloading", "not_preparable")])
async def test_a_post_that_cannot_be_prepared_is_refused_and_keeps_its_status(
    environment: Environment,
    path: str,
    code: str,
) -> None:
    post_id, _ = await seed_post(environment, path)
    before = (await environment.load(post_id)).status

    response = await environment.start(post_id)

    assert (response.status_code, response.json()["code"]) == (409, code)
    assert (await environment.load(post_id)).status is before
    environment.queue.send_task.assert_not_called()


async def test_a_track_without_youtube_music_is_refused_and_the_post_stays_a_draft(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, music_link=False)

    response = await environment.start(post_id)

    assert (response.status_code, response.json()["code"]) == (409, "youtube_music_missing")
    assert (await environment.load(post_id)).status is PostStatus.RESOLVED
    environment.queue.send_task.assert_not_called()
    assert environment.bot.calls == []


async def test_a_missing_post_is_404(environment: Environment) -> None:
    response = await environment.start(9_000_000_000)

    assert (response.status_code, response.json()["code"]) == (404, "post_not_found")


async def test_a_post_with_ready_audio_is_refused_by_the_function_with_nothing_written(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, "editing")

    async with environment.session_factory() as session:
        with pytest.raises(ApiProblem) as refusal:
            await prepare_audio(session, post_id, ADMIN_ID)
        await session.rollback()

    assert (refusal.value.status_code, refusal.value.code) == (409, "not_preparable")


async def test_a_started_preparation_queues_the_prepare_task_then_clears_the_draft_keyboard(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)

    response = await environment.start(post_id)

    assert response.status_code == 200
    assert response.json() == {"post_id": post_id, "status_label": "Завантажується аудіо"}
    environment.queue.send_task.assert_called_once_with(PREPARE_POST_AUDIO_TASK_NAME, kwargs={"post_id": post_id})
    assert environment.bot.calls == [
        {
            "chat_id": DRAFT_CHAT_ID,
            "message_id": post_id,
            "reply_markup": None,
            "request_timeout": 15,
        }
    ]
    assert (await environment.load(post_id)).status is PostStatus.DOWNLOADING


async def test_a_failing_keyboard_removal_does_not_fail_the_request(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)
    environment.bot.error = TelegramAPIError(method=EditMessageReplyMarkup(chat_id=1, message_id=1), message="gone")

    response = await environment.start(post_id)

    assert response.status_code == 200
    environment.queue.send_task.assert_called_once()
    assert (await environment.load(post_id)).status is PostStatus.DOWNLOADING


async def test_a_refused_queue_fails_the_post_at_downloading_and_retry_downloads_with_a_render(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment)
    environment.queue.send_task.side_effect = ConnectionError("redis is down")

    response = await environment.start(post_id)

    assert (response.status_code, response.json()["code"]) == (503, "dispatch_failed")
    failed = await environment.load(post_id)
    assert (failed.status, failed.failed_from_status, failed.last_error) == (
        PostStatus.FAILED,
        PostStatus.DOWNLOADING,
        DOWNLOAD_DISPATCH_FAILED_ERROR,
    )
    assert environment.bot.calls == []

    message = AsyncMock()
    message.text = f"/retry {post_id}"
    message.from_user = SimpleNamespace(id=ADMIN_ID)
    queue = MagicMock()
    await handle_retry(message, environment.session_factory, queue)

    queue.send_task.assert_called_once_with(DOWNLOAD_POST_AUDIO_TASK_NAME, kwargs={"post_id": post_id})
    assert (await environment.load(post_id)).status is PostStatus.DOWNLOADING


async def test_a_failed_preparation_can_be_started_again(environment: Environment) -> None:
    post_id, _ = await seed_post(environment, "failed_downloading")

    response = await environment.start(post_id)

    assert response.status_code == 200
    started = await environment.load(post_id)
    assert (started.status, started.failed_from_status, started.last_error) == (
        PostStatus.DOWNLOADING,
        None,
        None,
    )


async def test_a_post_with_ready_audio_ignores_every_button_in_the_chat(
    environment: Environment,
) -> None:
    post_id, _ = await seed_post(environment, "editing")
    bot = AsyncMock()
    queue = MagicMock()
    answers = []

    for action in ("publish", "render"):
        callback = AsyncMock()
        callback.data = f"{action}:{post_id}"
        callback.from_user = SimpleNamespace(id=ADMIN_ID)
        callback.message = SimpleNamespace(chat=SimpleNamespace(id=DRAFT_CHAT_ID), message_id=post_id)
        await handle_callback(callback, bot, environment.session_factory, queue)
        answers.append(callback.answer.await_args.args[0])

    assert all("аудіо вже підготовлено" in answer for answer in answers)
    queue.send_task.assert_not_called()
    bot.edit_message_reply_markup.assert_not_awaited()
    assert (await environment.load(post_id)).status is PostStatus.EDITING


@pytest.fixture
def worker(
    monkeypatch: pytest.MonkeyPatch,
    migrated_test_database_url: str,
    tmp_path: Path,
) -> StubRenderer:
    """nothing here may reach Telegram or the network: `.env` carries real credentials in Docker."""
    monkeypatch.setattr(worker_tasks.settings, "database_url", migrated_test_database_url)
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    monkeypatch.setattr(worker_tasks.settings, "media_min_free_bytes", 1)
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(worker_tasks, "send_alert", AsyncMock(return_value=True))

    async def serve_cover(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == COVER_URL
        return httpx.Response(200, content=b"\xff\xd8cover")

    renderer = StubRenderer()
    monkeypatch.setattr(
        worker_tasks,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(serve_cover)),
    )
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", renderer)
    monkeypatch.setattr(worker_tasks.video_render, "render_thumbnail", lambda *a, **k: None)

    async def deliver_preview(post: Post, outcome: worker_tasks.RenderOutcome) -> None:
        post.preview_chat_id = DRAFT_CHAT_ID
        post.preview_message_id = post.id + PREVIEW_MESSAGE_OFFSET

    monkeypatch.setattr(worker_tasks, "deliver_preview_for", deliver_preview)
    return renderer


def stub_download(monkeypatch: pytest.MonkeyPatch, track_id: int) -> MagicMock:
    """yt-dlp's stand-in; the returned mock is the broker the worker would chain a render on."""
    outcome = worker_tasks.DownloadOutcome(
        video_id=VIDEO_ID,
        path=Path(f"/data/audio/{track_id}/{VIDEO_ID}.opus"),
        size_bytes=3_400_000,
        duration_ms=DURATION_MS,
        ytdlp_version="test",
    )
    monkeypatch.setattr(worker_tasks, "download_audio_off_transaction", lambda *args: outcome)
    chained = MagicMock()
    monkeypatch.setattr(worker_tasks.celery_app, "send_task", chained)
    return chained


async def test_the_audio_is_prepared_without_a_render_and_the_post_waits_as_editing(
    environment: Environment,
    worker: StubRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id = await seed_post(environment)
    chained = stub_download(monkeypatch, track_id)
    assert (await environment.start(post_id)).status_code == 200

    await worker_tasks.perform_download_post_audio(post_id, then_render=False)

    waiting = await environment.load(post_id)
    assert waiting.status is PostStatus.EDITING
    assert (waiting.fragment_start_ms, waiting.fragment_end_ms) == (0, 45_000)
    chained.assert_not_called()
    async with environment.session_factory() as session:
        asset = await session.scalar(select(AudioAsset).where(AudioAsset.track_id == track_id))
    assert asset is not None and asset.duration_ms == DURATION_MS

    detail = (await environment.http.get(f"/api/posts/{post_id}", headers=environment.headers())).json()
    assert detail["status_label"] == "Аудіо готове — оберіть фрагмент"
    assert detail["fragment"]["choosable"] is True
    assert detail["editable"] is True


async def test_the_first_preview_comes_from_the_fragment_chosen_before_any_video(
    environment: Environment,
    worker: StubRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id = await seed_post(environment)
    async with environment.session_factory() as session:
        session.add_all(
            [
                Lyric(track_id=track_id, line_index=0, start_ms=10_000, text="Рано", source="manual"),
                Lyric(track_id=track_id, line_index=1, start_ms=70_000, text="Пізно", source="manual"),
            ]
        )
        await session.commit()
    stub_download(monkeypatch, track_id)

    assert (await environment.start(post_id)).status_code == 200
    await worker_tasks.perform_download_post_audio(post_id, then_render=False)
    detail = (await environment.http.get(f"/api/posts/{post_id}", headers=environment.headers())).json()

    chosen = await environment.http.post(
        f"/api/posts/{post_id}/fragment",
        json={
            "start_ms": 60_000,
            "end_ms": 105_000,
            "expected_post_updated_at": detail["expected_post_updated_at"],
        },
        headers=environment.headers(),
    )

    assert chosen.status_code == 200
    assert environment.queue.send_task.call_args.args[0] == RENDER_POST_VIDEO_TASK_NAME
    assert (await environment.load(post_id)).status is PostStatus.RENDERING

    await worker_tasks.perform_render_post_video(post_id)

    rendered = await environment.load(post_id)
    assert rendered.status is PostStatus.RENDERED
    assert (rendered.fragment_start_ms, rendered.fragment_end_ms) == (60_000, 105_000)
    assert len(worker.subtitles) == 1
    assert worker.subtitles[0] is not None
    assert "current,,0,0,0,,{\\pos(360,1050)}Пізно" in worker.subtitles[0]
    assert "current,,0,0,0,,{\\pos(360,1050)}Рано" not in worker.subtitles[0]


async def test_a_render_task_that_finds_ready_audio_does_nothing(
    environment: Environment, worker: StubRenderer
) -> None:
    post_id, _ = await seed_post(environment, "editing")

    await worker_tasks.perform_render_post_video(post_id)

    assert (await environment.load(post_id)).status is PostStatus.EDITING
    assert worker.subtitles == []


async def test_a_redelivered_prepare_at_a_post_waiting_for_the_choice_does_nothing(
    environment: Environment,
    worker: StubRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id = await seed_post(environment, "editing")
    chained = stub_download(monkeypatch, track_id)
    download = MagicMock()
    monkeypatch.setattr(worker_tasks, "download_audio_off_transaction", download)

    await worker_tasks.perform_download_post_audio(post_id, then_render=False)

    assert (await environment.load(post_id)).status is PostStatus.EDITING
    download.assert_not_called()
    chained.assert_not_called()
