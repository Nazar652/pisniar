"""Choosing a fragment against a real Postgres: the locks the choice takes, the status it writes,
a refused queue followed by `/retry`, and the whole chain up to a publish tap under the new preview.
Celery, Telegram and ffmpeg are stand-ins; the rows and the files are real.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_render_lyrics import COVER_URL, StubRenderer, make_downloaded_post

from pisniar.bot.handlers import handle_callback, handle_retry
from pisniar.db.models import AudioAsset, Lyric, Post, Render, Track
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.dispatch import RENDER_DISPATCH_FAILED_ERROR
from pisniar.webapp.database import is_lock_failure
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_choice import choose_fragment, enqueue_render
from pisniar.webapp.schemas import FragmentRequest
from pisniar.worker import ass
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.celery_app import PUBLISH_POST_TASK_NAME, RENDER_POST_VIDEO_TASK_NAME

pytestmark = pytest.mark.integration

MUSIC_VIDEO_ID = "n4bPzdIs_nc"
OTHER_VIDEO_ID = "dQw4w9WgXcQ"
USER_ID = 111
CHAT_ID = 111
PREVIEW_MESSAGE_OFFSET = 4_000_000
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def session_factory(migrated_test_database_url: str) -> AsyncIterator[Any]:
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=LOCK_TIMEOUT_MS)
    yield build_session_factory(engine)
    await engine.dispose()


def request_for(
    post: Post,
    start_ms: int,
    end_ms: int,
) -> FragmentRequest:
    return FragmentRequest(start_ms=start_ms, end_ms=end_ms, expected_post_updated_at=post.updated_at)


async def make_previewed_post(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: PostStatus = PostStatus.RENDERED,
    failed_from: PostStatus | None = None,
    audio_video_id: str = MUSIC_VIDEO_ID,
) -> int:
    marker = uuid.uuid4().hex
    async with session_factory() as session:
        track = Track(
            source_url=f"test://{marker}",
            source_url_normalized=f"test://{marker}",
            platform_links={
                "youtubeMusic": {
                    "url": f"https://music.youtube.com/watch?v={MUSIC_VIDEO_ID}",
                    "source": "ytmusic",
                    "match": "confirmed",
                }
            },
        )
        session.add(track)
        await session.flush()
        session.add(
            AudioAsset(
                track_id=track.id,
                video_id=audio_video_id,
                path=f"/data/audio/{track.id}/{audio_video_id}.webm",
                size_bytes=1000,
                duration_ms=180_000,
                ytdlp_version="test",
            )
        )

        post = Post(track_id=track.id, created_by_user_id=USER_ID, caption="caption")
        session.add(post)
        await session.flush()
        post.fragment_start_ms = 0
        post.fragment_end_ms = 45_000
        post.draft_chat_id = CHAT_ID
        post.draft_message_id = post.id
        post.preview_chat_id = CHAT_ID
        post.preview_message_id = post.id + PREVIEW_MESSAGE_OFFSET
        post.preview_params_hash = "a" * 64

        steps = [
            PostStatus.RESOLVING,
            PostStatus.RESOLVED,
            PostStatus.DOWNLOADING,
            PostStatus.DOWNLOADED,
            PostStatus.RENDERING,
        ]
        if failed_from is PostStatus.DOWNLOADED:
            steps = steps[:4]
        if status is PostStatus.RENDERED:
            steps.append(PostStatus.RENDERED)
        elif status is PostStatus.FAILED:
            steps.append(PostStatus.FAILED)
        for step in steps:
            set_post_status(post, step, error="boom" if step is PostStatus.FAILED else None)

        post_id = post.id
        await session.commit()
    return post_id


async def load_post(session_factory: async_sessionmaker[AsyncSession], post_id: int) -> Post:
    async with session_factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        return post


async def test_the_choice_holds_the_post_and_the_track_but_lets_a_new_post_of_the_track_in(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)

    async with session_factory() as writer, session_factory() as other:
        choice = await choose_fragment(writer, post_id, request_for(post, 60_000, 105_000), USER_ID)
        assert choice is not None

        with pytest.raises(DBAPIError) as post_lock:
            await other.execute(text("SELECT id FROM post WHERE id = :id FOR UPDATE NOWAIT"), {"id": post_id})
        await other.rollback()
        with pytest.raises(DBAPIError) as track_lock:
            await other.execute(
                text("SELECT id FROM track WHERE id = :id FOR NO KEY UPDATE NOWAIT"),
                {"id": post.track_id},
            )
        await other.rollback()
        key_share = await other.execute(
            text("SELECT id FROM track WHERE id = :id FOR KEY SHARE NOWAIT"),
            {"id": post.track_id},
        )
        await other.rollback()
        await writer.commit()

    assert is_lock_failure(post_lock.value) and is_lock_failure(track_lock.value)
    assert key_share.scalar_one() == post.track_id

    stored = await load_post(session_factory, post_id)
    assert stored.status is PostStatus.RENDERING
    assert (stored.fragment_start_ms, stored.fragment_end_ms) == (60_000, 105_000)
    assert choice.superseded_preview == (CHAT_ID, post_id + PREVIEW_MESSAGE_OFFSET)
    assert stored.preview_message_id == post_id + PREVIEW_MESSAGE_OFFSET


async def test_a_post_held_by_another_transaction_is_refused_at_once_and_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)

    async with session_factory() as holder, session_factory() as writer:
        await holder.get(Post, post_id, with_for_update=True)

        with pytest.raises(DBAPIError) as refused:
            await choose_fragment(writer, post_id, request_for(post, 60_000, 105_000), USER_ID)
        await writer.rollback()
        await holder.rollback()

    assert is_lock_failure(refused.value)
    assert (await load_post(session_factory, post_id)).status is PostStatus.RENDERED


async def test_a_track_held_by_another_transaction_times_out_as_a_lock_failure_and_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)

    async with session_factory() as holder, session_factory() as writer:
        await holder.get(Track, post.track_id, with_for_update={"key_share": True})

        with pytest.raises(DBAPIError) as refused:
            await choose_fragment(writer, post_id, request_for(post, 60_000, 105_000), USER_ID)
        await writer.rollback()
        await holder.rollback()

    assert is_lock_failure(refused.value)
    stored = await load_post(session_factory, post_id)
    assert stored.status is PostStatus.RENDERED
    assert (stored.fragment_start_ms, stored.fragment_end_ms) == (0, 45_000)


@pytest.mark.parametrize("failed_from", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
async def test_a_failed_render_is_started_again_from_failed(
    session_factory: async_sessionmaker[AsyncSession], failed_from: PostStatus
) -> None:
    post_id = await make_previewed_post(session_factory, status=PostStatus.FAILED, failed_from=failed_from)
    post = await load_post(session_factory, post_id)
    assert (post.status, post.failed_from_status) == (PostStatus.FAILED, failed_from)

    async with session_factory() as session:
        choice = await choose_fragment(session, post_id, request_for(post, 60_000, 105_000), USER_ID)
        await session.commit()

    assert choice is not None
    stored = await load_post(session_factory, post_id)
    assert (stored.status, stored.failed_from_status, stored.last_error) == (
        PostStatus.RENDERING,
        None,
        None,
    )


async def test_audio_of_another_recording_is_audio_missing_and_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory, audio_video_id=OTHER_VIDEO_ID)
    post = await load_post(session_factory, post_id)

    async with session_factory() as session:
        with pytest.raises(ApiProblem) as refusal:
            await choose_fragment(session, post_id, request_for(post, 60_000, 105_000), USER_ID)
        await session.rollback()

    assert (refusal.value.status_code, refusal.value.code) == (409, "audio_missing")
    assert (await load_post(session_factory, post_id)).status is PostStatus.RENDERED


async def test_a_post_changed_after_the_editor_opened_is_stale_and_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)
    stale = FragmentRequest(start_ms=60_000, end_ms=105_000, expected_post_updated_at=STAMP)

    async with session_factory() as session:
        with pytest.raises(ApiProblem) as refusal:
            await choose_fragment(session, post_id, stale, USER_ID)
        await session.rollback()

    assert (refusal.value.status_code, refusal.value.code) == (409, "stale_data")
    assert (await load_post(session_factory, post_id)).fragment_start_ms == post.fragment_start_ms


async def test_a_missing_post_is_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    request = FragmentRequest(start_ms=0, end_ms=1_000, expected_post_updated_at=STAMP)

    async with session_factory() as session:
        assert await choose_fragment(session, 9_000_000_000, request, USER_ID) is None


async def test_a_refused_queue_fails_the_post_at_rendering_and_retry_renders(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)
    async with session_factory() as session:
        choice = await choose_fragment(session, post_id, request_for(post, 60_000, 105_000), USER_ID)
        await session.commit()
    assert choice is not None

    broken_queue = MagicMock()
    broken_queue.send_task.side_effect = ConnectionError("redis is down")
    with pytest.raises(ApiProblem) as refusal:
        await enqueue_render(broken_queue, session_factory, choice, USER_ID)

    assert (refusal.value.status_code, refusal.value.code) == (503, "dispatch_failed")
    assert f"/retry {post_id}" in refusal.value.message
    failed = await load_post(session_factory, post_id)
    assert (failed.status, failed.failed_from_status, failed.last_error) == (
        PostStatus.FAILED,
        PostStatus.RENDERING,
        RENDER_DISPATCH_FAILED_ERROR,
    )
    assert (failed.fragment_start_ms, failed.fragment_end_ms) == (60_000, 105_000)

    message = AsyncMock()
    message.text = f"/retry {post_id}"
    message.from_user = SimpleNamespace(id=USER_ID)
    queue = MagicMock()
    await handle_retry(message, session_factory, queue)

    queue.send_task.assert_called_once_with(RENDER_POST_VIDEO_TASK_NAME, kwargs={"post_id": post_id})
    assert (await load_post(session_factory, post_id)).status is PostStatus.RENDERING


async def test_a_working_queue_receives_the_render_task_and_the_post_stays_rendering(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)
    async with session_factory() as session:
        choice = await choose_fragment(session, post_id, request_for(post, 60_000, 105_000), USER_ID)
        await session.commit()
    assert choice is not None
    queue = MagicMock()

    await enqueue_render(queue, session_factory, choice, USER_ID)

    queue.send_task.assert_called_once_with(RENDER_POST_VIDEO_TASK_NAME, kwargs={"post_id": post_id})
    assert (await load_post(session_factory, post_id)).status is PostStatus.RENDERING


class CountingPreview:
    """the Telegram upload's stand-in: every preview is a new message of its own."""

    def __init__(self) -> None:
        self.sent = 0

    async def __call__(self, post: Post, outcome: worker_tasks.RenderOutcome) -> None:
        self.sent += 1
        post.preview_chat_id = CHAT_ID
        post.preview_message_id = post.id + PREVIEW_MESSAGE_OFFSET + self.sent


@pytest.fixture
def pipeline(
    monkeypatch: pytest.MonkeyPatch,
    migrated_test_database_url: str,
    tmp_path: Path,
) -> tuple[StubRenderer, CountingPreview]:
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
    preview = CountingPreview()
    monkeypatch.setattr(
        worker_tasks,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(serve_cover)),
    )
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", renderer)
    monkeypatch.setattr(worker_tasks.video_render, "render_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(worker_tasks, "deliver_preview_for", preview)
    return renderer, preview


async def test_a_new_choice_renders_a_new_preview_and_only_its_button_publishes(
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: tuple[StubRenderer, CountingPreview],
    tmp_path: Path,
) -> None:
    renderer, preview = pipeline
    post_id, track_id, _, _ = await make_downloaded_post(session_factory)
    async with session_factory() as session:
        session.add_all(
            [
                Lyric(track_id=track_id, line_index=0, start_ms=10_000, text="Рано", source="manual"),
                Lyric(track_id=track_id, line_index=1, start_ms=70_000, text="Пізно", source="manual"),
            ]
        )
        await session.commit()

    await worker_tasks.perform_render_post_video(post_id)
    first = await load_post(session_factory, post_id)
    assert first.status is PostStatus.RENDERED
    old_preview = (first.preview_chat_id, first.preview_message_id)
    assert first.preview_params_hash is not None

    async with session_factory() as session:
        choice = await choose_fragment(session, post_id, request_for(first, 60_000, 105_000), USER_ID)
        await session.commit()
    assert choice is not None
    assert choice.superseded_preview == old_preview

    await worker_tasks.perform_render_post_video(post_id)

    second = await load_post(session_factory, post_id)
    assert second.status is PostStatus.RENDERED
    assert (second.fragment_start_ms, second.fragment_end_ms) == (60_000, 105_000)
    assert preview.sent == 2
    assert (second.preview_chat_id, second.preview_message_id) != old_preview
    assert second.preview_params_hash not in (None, first.preview_params_hash)
    assert renderer.subtitles[0] is not None and renderer.subtitles[1] is not None
    assert f"current,,0,0,0,,{{\\pos(360,{ass.CURRENT_SLOT_Y})}}Рано" in renderer.subtitles[0]
    assert f"current,,0,0,0,,{{\\pos(360,{ass.CURRENT_SLOT_Y})}}Пізно" in renderer.subtitles[1]
    async with session_factory() as session:
        renders = await session.scalar(select(func.count()).select_from(Render).where(Render.post_id == post_id))
    assert renders == 2

    stale_tap = SimpleNamespace(chat=SimpleNamespace(id=old_preview[0]), message_id=old_preview[1])
    stale = AsyncMock()
    stale.data = f"publish:{post_id}"
    stale.from_user = SimpleNamespace(id=USER_ID)
    stale.message = stale_tap
    queue = MagicMock()
    await handle_callback(stale, AsyncMock(), session_factory, queue)

    queue.send_task.assert_not_called()
    assert "застаріле" in stale.answer.await_args.args[0]
    assert (await load_post(session_factory, post_id)).status is PostStatus.RENDERED

    current = AsyncMock()
    current.data = f"publish:{post_id}"
    current.from_user = SimpleNamespace(id=USER_ID)
    current.message = SimpleNamespace(
        chat=SimpleNamespace(id=second.preview_chat_id), message_id=second.preview_message_id
    )
    await handle_callback(current, AsyncMock(), session_factory, queue)

    assert queue.send_task.call_args.args[0] == PUBLISH_POST_TASK_NAME
    async with session_factory() as session:
        publishing = await session.get(Post, post_id)
        assert publishing is not None
        assert publishing.status is PostStatus.PUBLISHING
        video = await worker_tasks.video_for_publication(session, publishing)
    assert video is not None
    assert video.path.name == f"{second.preview_params_hash}.mp4"
    assert video.path.is_relative_to(tmp_path)
