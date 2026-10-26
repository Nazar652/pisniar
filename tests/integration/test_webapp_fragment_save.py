"""Saving a fragment against a real Postgres: the bounds are the only thing written, the stamp moves only on a
change, the preview's own render row decides staleness, and the locks are the choice's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_webapp_fragment_render import CHAT_ID, PREVIEW_MESSAGE_OFFSET, USER_ID, load_post, make_previewed_post
from test_webapp_fragment_render import request_for as choose_request

from pisniar.db.models import Post, Render
from pisniar.db.previews import preview_fragment_bounds
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus
from pisniar.webapp.database import is_lock_failure
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_choice import save_fragment
from pisniar.webapp.service import fetch_post_detail

pytestmark = pytest.mark.integration

PREVIEW_HASH = "a" * 64
NEWER_HASH = "b" * 64


@pytest_asyncio.fixture
async def session_factory(migrated_test_database_url: str, test_lock_timeout_ms: int) -> AsyncIterator[Any]:
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=test_lock_timeout_ms)
    yield build_session_factory(engine)
    await engine.dispose()


async def add_render(
    session_factory: async_sessionmaker[AsyncSession],
    post_id: int,
    params_hash: str,
    bounds: tuple[int, int] | None,
) -> None:
    async with session_factory() as session:
        session.add(
            Render(
                post_id=post_id,
                params_hash=params_hash,
                path="p",
                size_bytes=1,
                duration_ms=1,
                fragment_start_ms=None if bounds is None else bounds[0],
                fragment_end_ms=None if bounds is None else bounds[1],
            )
        )
        await session.commit()


async def save(
    session_factory: async_sessionmaker[AsyncSession],
    post_id: int,
    start_ms: int,
    end_ms: int,
) -> Any:
    post = await load_post(session_factory, post_id)
    async with session_factory() as session:
        saved = await save_fragment(session, post_id, choose_request(post, start_ms, end_ms), USER_ID)
        await session.commit()
    return saved


async def test_a_save_stores_the_bounds_moves_the_stamp_and_leaves_the_rest_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    await add_render(session_factory, post_id, PREVIEW_HASH, (0, 45_000))
    before = await load_post(session_factory, post_id)

    saved = await save(session_factory, post_id, 60_000, 105_000)

    after = await load_post(session_factory, post_id)
    assert saved is not None
    assert (saved.start_ms, saved.end_ms, saved.preview_stale) == (60_000, 105_000, True)
    assert saved.post_updated_at == after.updated_at
    assert after.updated_at > before.updated_at
    assert (after.fragment_start_ms, after.fragment_end_ms) == (60_000, 105_000)
    assert after.status is PostStatus.RENDERED
    assert (after.preview_chat_id, after.preview_message_id, after.preview_params_hash) == (
        CHAT_ID,
        post_id + PREVIEW_MESSAGE_OFFSET,
        PREVIEW_HASH,
    )
    async with session_factory() as session:
        renders = (await session.scalars(select(Render).where(Render.post_id == post_id))).all()
    assert [(render.params_hash, render.fragment_start_ms, render.fragment_end_ms) for render in renders] == [
        (PREVIEW_HASH, 0, 45_000)
    ]


async def test_equal_bounds_write_nothing_and_the_stamp_stays(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    await add_render(session_factory, post_id, PREVIEW_HASH, (0, 45_000))
    before = await load_post(session_factory, post_id)

    saved = await save(session_factory, post_id, 0, 45_000)

    assert saved is not None
    assert (saved.post_updated_at, saved.preview_stale) == (before.updated_at, False)
    assert (await load_post(session_factory, post_id)).updated_at == before.updated_at


async def test_saving_the_preview_bounds_back_clears_the_stale_flag(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    await add_render(session_factory, post_id, PREVIEW_HASH, (0, 45_000))

    await save(session_factory, post_id, 60_000, 105_000)
    back = await save(session_factory, post_id, 0, 45_000)

    assert back is not None
    assert back.preview_stale is False


async def test_the_preview_row_decides_not_the_newest_render(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    await add_render(session_factory, post_id, PREVIEW_HASH, (0, 45_000))
    await add_render(session_factory, post_id, NEWER_HASH, (60_000, 105_000))

    async with session_factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        assert await preview_fragment_bounds(session, post) == (0, 45_000)
    saved = await save(session_factory, post_id, 60_000, 105_000)

    assert saved is not None
    assert saved.preview_stale is True


async def test_a_preview_row_without_recorded_bounds_is_never_stale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    await add_render(session_factory, post_id, PREVIEW_HASH, None)

    saved = await save(session_factory, post_id, 60_000, 105_000)

    assert saved is not None
    assert saved.preview_stale is False
    async with session_factory() as session:
        detail = await fetch_post_detail(session, post_id)
    assert detail is not None
    assert detail.fragment.preview_stale is False


async def test_the_detail_reports_a_stale_preview_after_a_save(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    await add_render(session_factory, post_id, PREVIEW_HASH, (0, 45_000))

    async with session_factory() as session:
        fresh = await fetch_post_detail(session, post_id)
    await save(session_factory, post_id, 60_000, 105_000)
    async with session_factory() as session:
        stale = await fetch_post_detail(session, post_id)

    assert fresh is not None and stale is not None
    assert (fresh.fragment.preview_stale, stale.fragment.preview_stale) == (False, True)
    assert (stale.fragment.start_ms, stale.fragment.end_ms) == (60_000, 105_000)


async def test_a_post_held_by_another_transaction_is_refused_at_once_and_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)

    async with session_factory() as holder, session_factory() as writer:
        await holder.get(Post, post_id, with_for_update=True)

        with pytest.raises(DBAPIError) as refused:
            await save_fragment(writer, post_id, choose_request(post, 60_000, 105_000), USER_ID)
        await writer.rollback()
        await holder.rollback()

    assert is_lock_failure(refused.value)
    assert (await load_post(session_factory, post_id)).fragment_start_ms == 0


async def test_the_save_holds_the_post_and_the_track_until_the_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)

    async with session_factory() as writer, session_factory() as other:
        saved = await save_fragment(writer, post_id, choose_request(post, 60_000, 105_000), USER_ID)
        assert saved is not None

        with pytest.raises(DBAPIError) as post_lock:
            await other.execute(text("SELECT id FROM post WHERE id = :id FOR UPDATE NOWAIT"), {"id": post_id})
        await other.rollback()
        with pytest.raises(DBAPIError) as track_lock:
            await other.execute(
                text("SELECT id FROM track WHERE id = :id FOR NO KEY UPDATE NOWAIT"), {"id": post.track_id}
            )
        await other.rollback()
        await writer.commit()

    assert is_lock_failure(post_lock.value) and is_lock_failure(track_lock.value)


async def test_a_stale_stamp_and_a_missing_post_change_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_id = await make_previewed_post(session_factory)
    post = await load_post(session_factory, post_id)
    request = choose_request(post, 60_000, 105_000).model_copy(
        update={"expected_post_updated_at": post.created_at.replace(year=2020)}
    )

    async with session_factory() as session:
        with pytest.raises(ApiProblem) as refusal:
            await save_fragment(session, post_id, request, USER_ID)
        await session.rollback()
        assert await save_fragment(session, 9_000_000_000, request, USER_ID) is None

    assert refusal.value.code == "stale_data"
    assert (await load_post(session_factory, post_id)).fragment_start_ms == 0
