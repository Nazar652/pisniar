"""`claim_post_for_rendering` against the real schema: the RENDERING self-edge changes no column,
so the stored `updated_at` moves only because the claim writes it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import AudioAsset, Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus
from pisniar.worker import tasks as worker_tasks

pytestmark = pytest.mark.integration

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


async def test_the_claim_of_a_rendering_post_stores_a_newer_updated_at(
    migrated_test_database_url: str,
    test_lock_timeout_ms: int,
) -> None:
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=test_lock_timeout_ms)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    marker = uuid.uuid4().hex
    try:
        async with session_factory() as session:
            track = Track(
                source_url=f"test://{marker}",
                source_url_normalized=f"test://{marker}",
                platform_links={"youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}},
                artists=[{"name": "Burned Time Machine", "hashtag": None, "spotify_id": None}],
                title="Покаяння",
                duration_ms=212_000,
            )
            session.add(track)
            await session.flush()
            session.add(
                AudioAsset(
                    track_id=track.id,
                    video_id="n4bPzdIs_nc",
                    path="/data/audio/test/n4bPzdIs_nc.opus",
                    size_bytes=1,
                    duration_ms=212_000,
                    ytdlp_version="test",
                )
            )
            post = Post(
                track_id=track.id,
                created_by_user_id=1,
                status=PostStatus.RENDERING,
                fragment_start_ms=0,
                fragment_end_ms=45_000,
                updated_at=LONG_AGO,
            )
            session.add(post)
            await session.commit()
            post_id = post.id

        claim = await worker_tasks.claim_post_for_rendering(session_factory, post_id)

        assert claim is not None
        async with session_factory() as session:
            stored = (await session.execute(select(Post.updated_at).where(Post.id == post_id))).scalar_one()
        assert stored > LONG_AGO
        assert (datetime.now(UTC) - stored).total_seconds() < 60
    finally:
        async with session_factory() as session:
            owned = select(Track.id).where(Track.source_url == f"test://{marker}")
            await session.execute(delete(Post).where(Post.track_id.in_(owned)))
            await session.execute(delete(Track).where(Track.source_url == f"test://{marker}"))
            await session.commit()
        await engine.dispose()
