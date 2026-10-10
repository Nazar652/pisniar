"""`video_for_publication` against the real schema: the render a post publishes is the one
`preview_params_hash` points at, whatever newer rows exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Render, Track
from pisniar.db.states import PostStatus
from pisniar.worker import storage as media_storage
from pisniar.worker import tasks as worker_tasks

pytestmark = pytest.mark.integration

EARLIER = datetime(2026, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 2, 1, tzinfo=UTC)
PREVIEWED_HASH = "a" * 64
NEWER_HASH = "b" * 64


async def make_previewed_post(session: AsyncSession, params_hash: str | None) -> Post:
    marker = uuid.uuid4().hex
    track = Track(source_url=f"test://{marker}", source_url_normalized=f"test://{marker}")
    session.add(track)
    await session.flush()

    post = Post(track_id=track.id, created_by_user_id=1, status=PostStatus.PUBLISHING)
    post.preview_chat_id = 555
    post.preview_message_id = 99
    post.preview_params_hash = params_hash
    session.add(post)
    await session.flush()
    return post


def add_render(
    session: AsyncSession,
    post: Post,
    params_hash: str,
    media_root: Path,
    updated_at: datetime = EARLIER,
) -> Path:
    path = media_storage.render_file_path(media_root, post.id, params_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video bytes")
    session.add(
        Render(
            post_id=post.id,
            params_hash=params_hash,
            path=str(path),
            size_bytes=11,
            duration_ms=45_000,
            updated_at=updated_at,
        )
    )
    return path


async def test_publication_takes_the_pointed_render_even_when_a_newer_one_exists(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post = await make_previewed_post(db_session, PREVIEWED_HASH)
    previewed_path = add_render(db_session, post, PREVIEWED_HASH, tmp_path)
    add_render(db_session, post, NEWER_HASH, tmp_path, LATER)
    await db_session.flush()

    video = await worker_tasks.video_for_publication(db_session, post)

    assert video is not None
    assert video.path == previewed_path


async def test_a_pointer_without_a_matching_render_row_fails_loudly(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a newer row must never stand in for the one the admin saw."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post = await make_previewed_post(db_session, PREVIEWED_HASH)
    add_render(db_session, post, NEWER_HASH, tmp_path)
    await db_session.flush()

    with pytest.raises(worker_tasks.RenderedVideoUnavailableError):
        await worker_tasks.video_for_publication(db_session, post)


async def test_a_post_without_a_preview_has_no_video(db_session: AsyncSession) -> None:
    post = Post(track_id=0, created_by_user_id=1)

    assert await worker_tasks.video_for_publication(db_session, post) is None
