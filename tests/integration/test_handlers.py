"""Link submission and `/retry` against a real Postgres: classification branches, race-safe get-or-create, the
duplicate-track receipt note, and `/retry`'s legal-transition guard."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.bot.handlers import (
    DISPATCH_FAILED_ERROR,
    get_or_create_track,
    handle_link,
    handle_retry,
    handle_start,
)
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
        track = await session.scalar(select(Track).where(Track.source_url_normalized == classified.normalized_url))
        assert track is not None
        post = await session.scalar(select(Post).where(Post.track_id == track.id))
        assert post is not None
        assert post.status is PostStatus.RESOLVING

    celery_client.send_task.assert_called_once_with(RESOLVE_POST_TASK_NAME, kwargs={"post_id": post.id})
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
        track = await session.scalar(select(Track).where(Track.source_url_normalized == classified.normalized_url))
        assert track is not None
        youtube_link = track.platform_links["youtube"]
        assert isinstance(youtube_link, dict)
        assert youtube_link["source"] == "user_input"
        assert youtube_link["match"] == "manual"


async def test_a_pair_resolves_from_the_platform_link_and_seeds_the_clip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """a clip alone resolves to a raw YouTube title and half the platforms; the platform link is also the track's
    canonical identity."""
    platform_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    clip_url = f"https://www.youtube.com/watch?v={uuid.uuid4().hex}"
    message = make_message(f"{platform_url} {clip_url}")
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    async with session_factory() as session:
        track = await session.scalar(
            select(Track).where(Track.source_url_normalized == classify_url(platform_url).normalized_url)
        )
        assert track is not None
        assert track.source_url == platform_url

        youtube_link = track.platform_links["youtube"]
        assert isinstance(youtube_link, dict)
        assert youtube_link["url"] == classify_url(clip_url).normalized_url
        assert youtube_link["source"] == "user_input"
        assert youtube_link["match"] == "manual"

        clip_track = await session.scalar(
            select(Track).where(Track.source_url_normalized == classify_url(clip_url).normalized_url)
        )
        assert clip_track is None

    assert "post_id=" in message.answer.await_args.args[0]


async def test_a_reversed_pair_lands_on_the_same_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """the order of the two links carries no meaning — only which of them is a clip does."""
    platform_url = f"https://www.deezer.com/track/{uuid.uuid4().hex}"
    clip_url = f"https://www.youtube.com/watch?v={uuid.uuid4().hex}"
    message = make_message(f"{clip_url}\n{platform_url}")
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    async with session_factory() as session:
        track = await session.scalar(
            select(Track).where(Track.source_url_normalized == classify_url(platform_url).normalized_url)
        )
        assert track is not None
        youtube_link = track.platform_links["youtube"]
        assert isinstance(youtube_link, dict)
        assert youtube_link["url"] == classify_url(clip_url).normalized_url


async def test_a_pair_attaches_the_clip_to_an_already_known_track(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """the motivating case is a song already posted from its platform link: without this the pair would be a silent
    no-op on the second submission."""
    platform_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    clip_url = f"https://www.youtube.com/watch?v={uuid.uuid4().hex}"
    celery_client = MagicMock()

    await handle_link(make_message(platform_url), session_factory, celery_client)
    await handle_link(make_message(f"{platform_url} {clip_url}"), session_factory, celery_client)

    async with session_factory() as session:
        track = await session.scalar(
            select(Track).where(Track.source_url_normalized == classify_url(platform_url).normalized_url)
        )
        assert track is not None
        youtube_link = track.platform_links["youtube"]
        assert isinstance(youtube_link, dict)
        assert youtube_link["url"] == classify_url(clip_url).normalized_url


async def test_two_platform_links_never_reach_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    second_url = f"https://www.deezer.com/track/{uuid.uuid4().hex}"
    message = make_message(f"{first_url} {second_url}")
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    assert "кліп" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()

    async with session_factory() as session:
        created = await session.scalar(
            select(Track).where(
                Track.source_url_normalized.in_(
                    [
                        classify_url(first_url).normalized_url,
                        classify_url(second_url).normalized_url,
                    ]
                )
            )
        )
        assert created is None


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
            track, _ = await get_or_create_track(session, marker_url, classified)
            await session.commit()
            winner_committed.set()
            return track

    async def run_loser() -> tuple[Track, bool]:
        async with session_factory() as session:
            await winner_committed.wait()
            return await get_or_create_track(session, marker_url, classified)

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

    celery_client.send_task.assert_called_once_with(RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id})


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
    """the self-edge exists for Celery redelivery only; as a button it would let two tasks both see `draft_message_id
    IS NULL` and send two drafts."""
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
    """a btree entry past ~2704 bytes raises ProgramLimitExceededError, not an IntegrityError, so the race fallback
    would never catch it."""
    over_long_url = "https://open.spotify.com/track/" + "a" * MAX_NORMALIZED_URL_BYTES
    message = make_message(over_long_url)
    celery_client = MagicMock()

    await handle_link(message, session_factory, celery_client)

    assert "задовге" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()

    async with session_factory() as session:
        created = await session.scalar(
            select(Track).where(Track.source_url_normalized == classify_url(over_long_url).normalized_url)
        )
        assert created is None


async def test_handle_retry_reopens_a_resolving_post_whose_worker_died(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """a post whose worker died would otherwise stay RESOLVING, with no exit but a manual `UPDATE` in psql."""
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

        # straight SQL: `updated_at` is an ORM `onupdate`, so an attribute write here would just stamp it with `now()`
        # again.
        await session.execute(
            text("UPDATE post SET updated_at = now() - make_interval(secs => :age) WHERE id = :id"),
            {"age": TASK_TIME_LIMIT_SECONDS + 60, "id": post_id},
        )
        await session.commit()

    message = make_message(f"/retry {post_id}")
    celery_client = MagicMock()

    await handle_retry(message, session_factory, celery_client)

    assert "Повторюю" in message.answer.await_args.args[0]
    celery_client.send_task.assert_called_once_with(RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id})


async def test_handle_retry_answers_instead_of_blocking_when_the_row_is_locked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """an unlocked status read would let two near-simultaneous `/retry` both dispatch; `nowait` also keeps the handler
    off a live resolve's `FOR UPDATE`."""
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


async def test_a_stale_retry_twice_in_a_row_dispatches_only_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """the RESOLVING self-edge changes no column, so `onupdate` never fires; without the manual restamp every later
    /retry would pass the guard and queue another task."""
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

        await session.execute(
            text("UPDATE post SET updated_at = now() - make_interval(secs => :age) WHERE id = :id"),
            {"age": TASK_TIME_LIMIT_SECONDS + 60, "id": post_id},
        )
        await session.commit()

    celery_client = MagicMock()
    first_message = make_message(f"/retry {post_id}")
    second_message = make_message(f"/retry {post_id}")

    await handle_retry(first_message, session_factory, celery_client)
    await handle_retry(second_message, session_factory, celery_client)

    assert "Повторюю" in first_message.answer.await_args.args[0]
    assert "уже обробляється" in second_message.answer.await_args.args[0]
    celery_client.send_task.assert_called_once_with(RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id})


async def test_a_dispatch_failure_leaves_the_post_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """the post is committed as RESOLVING before the dispatch; left there, /retry would refuse the very advice
    («спробуйте /retry») the bot just gave."""
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    message = make_message(marker_url)
    celery_client = MagicMock()
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_link(message, session_factory, celery_client)

    assert "Спробуйте /retry" in message.answer.await_args.args[0]

    async with session_factory() as session:
        classified = classify_url(marker_url)
        track = await session.scalar(select(Track).where(Track.source_url_normalized == classified.normalized_url))
        assert track is not None
        post = await session.scalar(select(Post).where(Post.track_id == track.id))
        assert post is not None
        post_id = post.id
        assert post.status is PostStatus.FAILED
        assert post.last_error is not None

    celery_client.send_task.side_effect = None
    retry_message = make_message(f"/retry {post_id}")

    await handle_retry(retry_message, session_factory, celery_client)

    assert "Повторюю" in retry_message.answer.await_args.args[0]


async def test_a_failed_retry_dispatch_does_not_strand_the_post_in_resolving(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """the /retry side of the same commit-then-dispatch order: the advice to try again must be one that /retry then
    accepts."""
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
    celery_client.send_task.side_effect = RuntimeError("broker down")

    await handle_retry(message, session_factory, celery_client)

    assert "Не вдалося поставити задачу" in message.answer.await_args.args[0]

    async with session_factory() as session:
        reloaded_post = await session.get(Post, post_id)
        assert reloaded_post is not None
        assert reloaded_post.status is PostStatus.FAILED
        assert reloaded_post.last_error == DISPATCH_FAILED_ERROR


async def test_handle_link_answers_when_the_row_it_needs_is_locked(
    migrated_test_database_url: str,
) -> None:
    """`INSERT INTO post` takes `FOR KEY SHARE` on the track, and a lock timeout is a `DBAPIError` that `except
    IntegrityError` does not cover."""
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=300)
    factory = build_session_factory(engine)
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    message = make_message(marker_url)
    celery_client = MagicMock()

    try:
        async with factory() as seed:
            seed.add(Track(source_url=marker_url, source_url_normalized=marker_url))
            await seed.commit()

        async with factory() as holder:
            await holder.execute(select(Track.id).where(Track.source_url_normalized == marker_url).with_for_update())

            await asyncio.wait_for(handle_link(message, factory, celery_client), timeout=30)

            await holder.rollback()
    finally:
        await engine.dispose()

    assert "База даних" in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()
