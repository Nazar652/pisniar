"""Link submission and `/retry` against a real Postgres: classification
branches, race-safe get-or-create, the duplicate-track receipt note, and
`/retry`'s legal-transition guard."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.bot.handlers import _get_or_create_track, handle_link, handle_retry, handle_start
from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.resolve.urls import MAX_NORMALIZED_URL_BYTES, classify_url
from pisniar.worker.celery_app import RESOLVE_POST_TASK_NAME, TASK_TIME_LIMIT_SECONDS

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_factory(migrated_test_database_url: str):
    engine = build_engine(migrated_test_database_url)
    factory = build_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def make_message(text: str, user_id: int = 111) -> AsyncMock:
    message = AsyncMock()
    message.text = text
    message.from_user = SimpleNamespace(id=user_id)
    return message


async def test_handle_start_sends_ukrainian_greeting() -> None:
    message = make_message("/start")

    await handle_start(message)

    message.answer.assert_awaited_once()
    assert "Піснярні" in message.answer.await_args.args[0]


async def test_handle_link_rejects_album_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = make_message("https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j")
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    assert "альбом" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()


async def test_handle_link_rejects_unrecognized_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = make_message("hello there, not a link")
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    assert "Не розпізнав" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()


async def test_handle_link_creates_track_and_post_and_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    message = make_message(marker_url)
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    async with session_factory() as session:
        classified = classify_url(marker_url)
        track = await session.scalar(
            select(Track).where(Track.source_url_normalized == classified.normalized_url)
        )
        assert track is not None
        post = await session.scalar(select(Post).where(Post.track_id == track.id))
        assert post is not None
        assert post.status is PostStatus.RESOLVING

    celery_client.send_task.assert_called_once_with(
        RESOLVE_POST_TASK_NAME, kwargs={"post_id": post.id}
    )
    assert f"post_id={post.id}" in message.answer.await_args.args[0]


async def test_handle_link_seeds_user_input_for_youtube_clip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    video_url = f"https://www.youtube.com/watch?v={uuid.uuid4().hex}"
    message = make_message(video_url)
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    async with session_factory() as session:
        classified = classify_url(video_url)
        track = await session.scalar(
            select(Track).where(Track.source_url_normalized == classified.normalized_url)
        )
        assert track is not None
        youtube_link = track.platform_links["youtube"]
        assert isinstance(youtube_link, dict)
        assert youtube_link["source"] == "user_input"
        assert youtube_link["match"] == "manual"


async def test_handle_link_notes_duplicate_on_resubmission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    celery_client = MagicMock()

    await handle_link(make_message(marker_url), session_factory, celery_client)
    second_message = make_message(marker_url)
    await handle_link(second_message, session_factory, celery_client)

    receipt = second_message.answer.await_args.args[0]
    assert "уже реєструвався" in receipt


async def test_handle_link_reports_dispatch_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    message = make_message(marker_url)
    celery_client = MagicMock()
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_link(message, session_factory, celery_client)

    assert "Спробуйте /retry" in message.answer.await_args.args[0]


async def test_get_or_create_track_handles_concurrent_insert_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    classified = classify_url(marker_url)
    winner_committed = asyncio.Event()

    async def run_winner() -> Track:
        async with session_factory() as session:
            track, _existed = await _get_or_create_track(session, marker_url, classified)
            await session.commit()
            winner_committed.set()
            return track

    async def run_loser() -> tuple[Track, bool]:
        async with session_factory() as session:
            await winner_committed.wait()
            return await _get_or_create_track(session, marker_url, classified)

    winner_track, (loser_track, loser_existed) = await asyncio.gather(run_winner(), run_loser())

    assert loser_existed is True
    assert loser_track.id == winner_track.id


async def test_handle_retry_rejects_invalid_format(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = make_message("/retry not-a-number")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    assert "Формат" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()


async def test_handle_retry_rejects_unknown_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = make_message("/retry 999999999")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    assert "не знайдено" in message.answer.await_args.args[0]


async def test_handle_retry_dispatches_for_a_failed_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.flush()
        post = Post(track_id=track.id, created_by_user_id=111)
        session.add(post)
        await session.flush()
        set_post_status(post, PostStatus.RESOLVING)
        set_post_status(post, PostStatus.FAILED, error="boom")
        post_id = post.id
        await session.commit()

    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    async with session_factory() as session:
        reloaded_post = await session.get(Post, post_id)
        assert reloaded_post is not None
        assert reloaded_post.status is PostStatus.RESOLVING

    celery_client.send_task.assert_called_once_with(
        RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id}
    )


async def test_handle_retry_rejects_illegal_transition_from_resolved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.flush()
        post = Post(track_id=track.id, created_by_user_id=111, caption="x")
        session.add(post)
        await session.flush()
        set_post_status(post, PostStatus.RESOLVING)
        set_post_status(post, PostStatus.RESOLVED)
        post_id = post.id
        await session.commit()

    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    assert "недоступний" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()


async def test_handle_retry_refuses_a_post_that_is_still_resolving(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """D-43/F-9: the `RESOLVING -> RESOLVING` self-edge exists for Celery
    redelivery only. As a button it let two tasks both see
    `draft_message_id IS NULL` and send two drafts."""
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.flush()
        post = Post(track_id=track.id, created_by_user_id=111)
        session.add(post)
        await session.flush()
        set_post_status(post, PostStatus.RESOLVING)
        post_id = post.id
        await session.commit()

    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    assert "уже обробляється" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()


async def test_handle_link_refuses_an_over_long_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F-22: `source_url_normalized` is UNIQUE, and a btree entry past
    ~2704 bytes raises ProgramLimitExceededError — not an IntegrityError,
    so the existing race fallback would never catch it."""
    over_long_url = "https://open.spotify.com/track/" + "a" * MAX_NORMALIZED_URL_BYTES
    message = make_message(over_long_url)
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    assert "задовге" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()

    async with session_factory() as session:
        created = await session.scalar(
            select(Track).where(
                Track.source_url_normalized == classify_url(over_long_url).normalized_url
            )
        )
        assert created is None


async def test_handle_retry_reopens_a_resolving_post_whose_worker_died(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2-2: D-43 closed `/retry` from `RESOLVING`, which was right for a
    live task but left a post whose worker died with no exit at all — only
    a manual `UPDATE` in psql, documented nowhere."""
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.flush()
        post = Post(track_id=track.id, created_by_user_id=111)
        session.add(post)
        await session.flush()
        set_post_status(post, PostStatus.RESOLVING)
        post_id = post.id
        await session.commit()

        # Straight SQL: `updated_at` is an ORM `onupdate`, so an attribute
        # write here would just stamp it with `now()` again.
        await session.execute(
            text("UPDATE post SET updated_at = now() - make_interval(secs => :age) WHERE id = :id"),
            {"age": TASK_TIME_LIMIT_SECONDS + 60, "id": post_id},
        )
        await session.commit()

    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    assert "Повторюю" in message.answer.await_args.args[0]
    celery_client.send_task.assert_called_once_with(
        RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id}
    )


async def test_handle_retry_answers_instead_of_blocking_when_the_row_is_locked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2-12: the status was read without a lock, so two near-simultaneous
    `/retry` both passed the check and both dispatched. `nowait` also keeps
    the handler from blocking behind a live resolve's `FOR UPDATE`."""
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    async with session_factory() as session:
        track = Track(source_url=marker_url, source_url_normalized=marker_url)
        session.add(track)
        await session.flush()
        post = Post(track_id=track.id, created_by_user_id=111)
        session.add(post)
        await session.flush()
        set_post_status(post, PostStatus.RESOLVING)
        set_post_status(post, PostStatus.FAILED, error="boom")
        post_id = post.id
        await session.commit()

    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    async with session_factory() as holder:
        locked = await holder.get(Post, post_id, with_for_update=True)
        assert locked is not None

        await asyncio.wait_for(handle_retry(message, session_factory, celery_client), timeout=10)

        await holder.rollback()

    assert "обробляється воркером" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()
