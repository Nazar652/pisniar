"""Transaction 2 of the media tasks with a faked session: what `finish_download` and
`finish_render` write, in which order, and what they leave alone when the post has
moved on or its lock cannot be taken.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from pisniar.db.models import Post
from pisniar.db.states import PostStatus
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.lyrics import LyricsQuery
from pisniar.worker.tasks import (
    DownloadClaim,
    DownloadOutcome,
    PreviewReference,
    RenderClaim,
    RenderOutcome,
)

POST_ID = 7
PREVIEW_CHAT_ID = 555
PREVIEW_MESSAGE_ID = 99


class FakeSession:
    def __init__(self, state: SimpleNamespace) -> None:
        self.state = state

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None

    async def get(self, model: type, identifier: int, **options: Any) -> Post | None:
        assert options == {"with_for_update": True}
        if self.state.lock_error:
            raise DBAPIError("SELECT ... FOR UPDATE", {}, Exception("lock timeout"))
        return self.state.post

    async def execute(self, statement: object) -> None:
        self.state.order.append("execute")
        self.state.statements.append(statement)

    async def commit(self) -> None:
        self.state.order.append("commit")

    async def rollback(self) -> None:
        self.state.order.append("rollback")


@pytest.fixture
def database() -> SimpleNamespace:
    state = SimpleNamespace(post=None, lock_error=False, order=[], statements=[])
    state.session_factory = lambda: FakeSession(state)
    return state


def make_post(status: PostStatus, **overrides: Any) -> Post:
    post = Post(id=POST_ID, track_id=3, created_by_user_id=1)
    post.status = status
    post.fragment_start_ms = None
    post.fragment_end_ms = None
    for name, value in overrides.items():
        setattr(post, name, value)
    return post


DOWNLOAD_CLAIM = DownloadClaim(
    post_id=POST_ID, track_id=3, platform_links={}, track_duration_ms=None
)
DOWNLOAD_OUTCOME = DownloadOutcome(
    video_id="n4bPzdIs_nc",
    path=Path("/data/audio/3/n4bPzdIs_nc.opus"),
    size_bytes=3_400_000,
    duration_ms=212_000,
    ytdlp_version="2025.12.8",
)


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch, database: SimpleNamespace) -> AsyncMock:
    mock = AsyncMock(side_effect=lambda *args: database.order.append("chain"))
    monkeypatch.setattr(worker_tasks, "chain_render_after_download", mock)
    return mock


async def test_a_finished_download_records_the_asset_the_bounds_and_the_status(
    database: SimpleNamespace, chain: AsyncMock
) -> None:
    database.post = make_post(PostStatus.DOWNLOADING)

    await worker_tasks.finish_download(database.session_factory, DOWNLOAD_CLAIM, DOWNLOAD_OUTCOME)

    assert database.post.status is PostStatus.DOWNLOADED
    assert (database.post.fragment_start_ms, database.post.fragment_end_ms) == (0, 45_000)
    assert len(database.statements) == 1


async def test_the_render_is_chained_only_after_the_download_commits(
    database: SimpleNamespace, chain: AsyncMock
) -> None:
    database.post = make_post(PostStatus.DOWNLOADING)

    await worker_tasks.finish_download(database.session_factory, DOWNLOAD_CLAIM, DOWNLOAD_OUTCOME)

    assert database.order == ["execute", "commit", "chain"]
    chain.assert_awaited_once_with(database.session_factory, POST_ID)


async def test_bounds_already_on_the_post_survive_the_download(
    database: SimpleNamespace, chain: AsyncMock
) -> None:
    database.post = make_post(
        PostStatus.DOWNLOADING, fragment_start_ms=60_000, fragment_end_ms=105_000
    )

    await worker_tasks.finish_download(database.session_factory, DOWNLOAD_CLAIM, DOWNLOAD_OUTCOME)

    assert (database.post.fragment_start_ms, database.post.fragment_end_ms) == (60_000, 105_000)


async def test_a_locked_post_ends_the_download_without_chaining(
    database: SimpleNamespace, chain: AsyncMock
) -> None:
    database.lock_error = True

    await worker_tasks.finish_download(database.session_factory, DOWNLOAD_CLAIM, DOWNLOAD_OUTCOME)

    assert database.order == ["rollback"]
    chain.assert_not_awaited()


@pytest.mark.parametrize("status", [None, PostStatus.FAILED, PostStatus.DOWNLOADED])
async def test_a_post_that_is_no_longer_downloading_is_left_alone(
    status: PostStatus | None, database: SimpleNamespace, chain: AsyncMock
) -> None:
    post = None if status is None else make_post(status)
    database.post = post

    await worker_tasks.finish_download(database.session_factory, DOWNLOAD_CLAIM, DOWNLOAD_OUTCOME)

    assert database.order == []
    chain.assert_not_awaited()
    if post is not None:
        assert post.status is status


RENDER_CLAIM = RenderClaim(
    post_id=POST_ID,
    track_id=3,
    video_id="n4bPzdIs_nc",
    audio_path="/data/audio/3/n4bPzdIs_nc.opus",
    audio_duration_ms=212_000,
    cover_url=None,
    fragment_start_ms=0,
    fragment_end_ms=45_000,
    lyrics_query=LyricsQuery(track_id=3, artist_names=("A",), title="T", duration_ms=None),
)
RENDER_OUTCOME = RenderOutcome(
    params_hash="a" * 64,
    path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
    size_bytes=2_000_000,
    duration_ms=45_000,
    text_version=None,
    lyrics_reason="lyrics_absent",
)


@pytest.fixture
def preview(monkeypatch: pytest.MonkeyPatch, database: SimpleNamespace) -> AsyncMock:
    async def deliver(post: Post, outcome: RenderOutcome) -> None:
        database.order.append("preview")
        post.preview_chat_id = PREVIEW_CHAT_ID
        post.preview_message_id = PREVIEW_MESSAGE_ID

    mock = AsyncMock(side_effect=deliver)
    monkeypatch.setattr(worker_tasks, "deliver_preview_for", mock)
    return mock


async def test_a_finished_render_sends_the_preview_then_marks_rendered_and_commits(
    database: SimpleNamespace, preview: AsyncMock
) -> None:
    database.post = make_post(PostStatus.RENDERING)

    reference = await worker_tasks.finish_render(
        database.session_factory, RENDER_CLAIM, RENDER_OUTCOME
    )

    assert database.order == ["execute", "preview", "commit"]
    assert database.post.status is PostStatus.RENDERED
    assert reference == PreviewReference(chat_id=PREVIEW_CHAT_ID, message_id=PREVIEW_MESSAGE_ID)


async def test_a_failed_preview_delivery_leaves_the_post_rendering_and_uncommitted(
    database: SimpleNamespace, preview: AsyncMock
) -> None:
    preview.side_effect = RuntimeError("telegram is down")
    database.post = make_post(PostStatus.RENDERING)

    with pytest.raises(RuntimeError, match="telegram is down"):
        await worker_tasks.finish_render(database.session_factory, RENDER_CLAIM, RENDER_OUTCOME)

    assert database.post.status is PostStatus.RENDERING
    assert "commit" not in database.order


async def test_a_locked_post_ends_the_render_without_a_preview(
    database: SimpleNamespace, preview: AsyncMock
) -> None:
    database.lock_error = True

    reference = await worker_tasks.finish_render(
        database.session_factory, RENDER_CLAIM, RENDER_OUTCOME
    )

    assert reference is None
    assert database.order == ["rollback"]
    preview.assert_not_awaited()


@pytest.mark.parametrize("status", [None, PostStatus.FAILED, PostStatus.RENDERED])
async def test_a_post_that_is_no_longer_rendering_gets_no_second_preview(
    status: PostStatus | None, database: SimpleNamespace, preview: AsyncMock
) -> None:
    database.post = None if status is None else make_post(status)

    reference = await worker_tasks.finish_render(
        database.session_factory, RENDER_CLAIM, RENDER_OUTCOME
    )

    assert reference is None
    assert database.order == []
    preview.assert_not_awaited()


OLD_PREVIEW_CHAT_ID = 556
OLD_PREVIEW_MESSAGE_ID = 77
OLD_PARAMS_HASH = "b" * 64


@pytest.fixture
def superseded_removal(monkeypatch: pytest.MonkeyPatch, database: SimpleNamespace) -> AsyncMock:
    async def remove(post_id: int, superseded: PreviewReference) -> None:
        database.order.append("remove")

    mock = AsyncMock(side_effect=remove)
    monkeypatch.setattr(worker_tasks, "remove_superseded_preview_keyboard", mock)
    return mock


def make_previewed_post(status: PostStatus) -> Post:
    return make_post(
        status,
        preview_chat_id=OLD_PREVIEW_CHAT_ID,
        preview_message_id=OLD_PREVIEW_MESSAGE_ID,
        preview_params_hash=OLD_PARAMS_HASH,
    )


async def test_the_first_preview_points_the_post_at_its_render_and_removes_nothing(
    database: SimpleNamespace, preview: AsyncMock, superseded_removal: AsyncMock
) -> None:
    database.post = make_post(PostStatus.RENDERING)

    await worker_tasks.finish_render(database.session_factory, RENDER_CLAIM, RENDER_OUTCOME)

    assert database.post.preview_params_hash == RENDER_OUTCOME.params_hash
    superseded_removal.assert_not_awaited()


async def test_a_new_preview_replaces_the_reference_and_the_hash_in_one_commit(
    database: SimpleNamespace, preview: AsyncMock, superseded_removal: AsyncMock
) -> None:
    database.post = make_previewed_post(PostStatus.RENDERING)

    reference = await worker_tasks.finish_render(
        database.session_factory, RENDER_CLAIM, RENDER_OUTCOME
    )

    assert database.order == ["execute", "preview", "commit", "remove"]
    assert database.post.status is PostStatus.RENDERED
    assert database.post.preview_message_id == PREVIEW_MESSAGE_ID
    assert database.post.preview_params_hash == RENDER_OUTCOME.params_hash
    assert reference == PreviewReference(chat_id=PREVIEW_CHAT_ID, message_id=PREVIEW_MESSAGE_ID)
    superseded_removal.assert_awaited_once_with(
        POST_ID, PreviewReference(chat_id=OLD_PREVIEW_CHAT_ID, message_id=OLD_PREVIEW_MESSAGE_ID)
    )


async def test_a_failed_new_preview_keeps_the_old_reference_and_removes_nothing(
    database: SimpleNamespace, preview: AsyncMock, superseded_removal: AsyncMock
) -> None:
    preview.side_effect = RuntimeError("telegram is down")
    database.post = make_previewed_post(PostStatus.RENDERING)

    with pytest.raises(RuntimeError, match="telegram is down"):
        await worker_tasks.finish_render(database.session_factory, RENDER_CLAIM, RENDER_OUTCOME)

    assert database.post.status is PostStatus.RENDERING
    assert database.post.preview_message_id == OLD_PREVIEW_MESSAGE_ID
    assert database.post.preview_params_hash == OLD_PARAMS_HASH
    superseded_removal.assert_not_awaited()


def patch_bot_client(monkeypatch: pytest.MonkeyPatch, bot: object) -> None:
    @asynccontextmanager
    async def client() -> AsyncIterator[object]:
        yield bot

    monkeypatch.setattr(worker_tasks, "bot_client", client)


async def test_the_superseded_keyboard_is_removed_from_the_old_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AsyncMock()
    patch_bot_client(monkeypatch, bot)

    await worker_tasks.remove_superseded_preview_keyboard(
        POST_ID, PreviewReference(chat_id=OLD_PREVIEW_CHAT_ID, message_id=OLD_PREVIEW_MESSAGE_ID)
    )

    assert bot.edit_message_reply_markup.await_args.kwargs == {
        "chat_id": OLD_PREVIEW_CHAT_ID,
        "message_id": OLD_PREVIEW_MESSAGE_ID,
        "reply_markup": None,
    }


async def test_a_failure_removing_the_superseded_keyboard_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = RuntimeError("network")
    patch_bot_client(monkeypatch, bot)

    await worker_tasks.remove_superseded_preview_keyboard(
        POST_ID, PreviewReference(chat_id=OLD_PREVIEW_CHAT_ID, message_id=OLD_PREVIEW_MESSAGE_ID)
    )


@pytest.mark.parametrize(("previewed", "draft_keyboard_removed"), [(False, True), (True, False)])
async def test_only_the_first_preview_removes_the_draft_keyboard(
    previewed: bool, draft_keyboard_removed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_bot_client(monkeypatch, AsyncMock())
    deliver = AsyncMock()
    remove_draft = AsyncMock()
    monkeypatch.setattr(worker_tasks, "deliver_preview", deliver)
    monkeypatch.setattr(worker_tasks, "remove_draft_keyboard", remove_draft)
    post = (
        make_previewed_post(PostStatus.RENDERING) if previewed else make_post(PostStatus.RENDERING)
    )

    await worker_tasks.deliver_preview_for(post, RENDER_OUTCOME)

    deliver.assert_awaited_once()
    assert remove_draft.await_count == (1 if draft_keyboard_removed else 0)
