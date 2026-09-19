"""`worker.tasks._resolve_post()` against a real Postgres: status
transitions, track persistence, the non-track cleanup path, redelivery
idempotency, and the failed-resolution path. `resolve_track`/`resolve_genres`/
`deliver_draft` are faked — those have their own dedicated test suites.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.resolve.types import ArtistInfo, MatchClass, PlatformLink, ResolvedTrack
from pisniar.worker import tasks as worker_tasks

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def point_task_at_test_database(
    monkeypatch: pytest.MonkeyPatch, migrated_test_database_url: str
) -> AsyncMock:
    monkeypatch.setattr(worker_tasks.settings, "database_url", migrated_test_database_url)
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=AsyncMock()))
    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_tasks, "send_alert", send_alert_mock)
    return send_alert_mock


@pytest.fixture
def deliver_draft_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(worker_tasks, "deliver_draft", mock)
    return mock


async def make_resolving_post(db_session: AsyncSession) -> int:
    # A real, classify_url()-recognized TRACK shape — `_resolve_and_deliver`
    # runs the "not a track" cleanup path first, before calling `resolve_track`.
    marker = uuid.uuid4().hex
    track_url = f"https://open.spotify.com/track/{marker}"
    track = Track(source_url=track_url, source_url_normalized=track_url)
    db_session.add(track)
    await db_session.flush()

    post = Post(track_id=track.id, created_by_user_id=111)
    db_session.add(post)
    await db_session.flush()
    set_post_status(post, PostStatus.RESOLVING)
    await db_session.commit()
    return post.id


async def test_resolve_post_persists_track_and_marks_resolved(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    resolved = ResolvedTrack(
        isrc="GBAYE9200070",
        title="Creep",
        artists=[ArtistInfo(name="Radiohead")],
        year=1993,
        release_type="album",
        release_title="Pablo Honey",
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None

    assert post.status is PostStatus.RESOLVED
    assert post.caption is not None
    assert "Creep" in post.caption
    assert track.isrc == "GBAYE9200070"
    assert track.title == "Creep"
    assert track.resolved_at is not None
    deliver_draft_mock.assert_awaited_once()


async def test_resolve_post_cleans_up_a_track_that_was_never_resolved(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """R2-1, half one: with no `resolved_at` and no stored links there is
    nothing to lose, so the first empty resolve still removes the row."""
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=ResolvedTrack()))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track_id = post.track_id
    await db_session.commit()

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    remaining_post = await db_session.get(Post, post_id)
    remaining_track = await db_session.get(Track, track_id)

    assert remaining_post is None
    assert remaining_track is None


async def test_an_empty_re_resolve_fails_the_post_instead_of_deleting_the_track(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """R2-1, half two: the D-40 TTL made a re-fetch routine, so one song.link
    404 used to delete an already-resolved track along with the admin's
    manual link and hashtag — irreversibly, with no post left to `/retry`.
    """
    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track_id = track.id

    track.resolved_at = datetime.now(UTC)
    track.isrc = f"UA{uuid.uuid4().hex[:3].upper()}2500009"
    track.artists = [{"name": "0%Mercury", "hashtag": "ZeroPercentMercury", "spotify_id": None}]
    track.platform_links = {
        "spotify": {
            "url": "https://open.spotify.com/track/manual",
            "source": "user_input",
            "match": "manual",
        }
    }
    await db_session.commit()

    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=ResolvedTrack()))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded_post = await db_session.get(Post, post_id)
    reloaded_track = await db_session.get(Track, track_id)

    assert reloaded_post is not None
    assert reloaded_post.status is PostStatus.FAILED
    assert reloaded_track is not None
    assert reloaded_track.platform_links["spotify"] == {
        "url": "https://open.spotify.com/track/manual",
        "source": "user_input",
        "match": "manual",
    }
    stored_artist = reloaded_track.artists[0]
    assert isinstance(stored_artist, dict)
    assert stored_artist["hashtag"] == "ZeroPercentMercury"
    point_task_at_test_database.assert_awaited()


async def test_resolve_post_marks_failed_on_resolve_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(side_effect=RuntimeError("boom")))

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None

    assert post.status is PostStatus.FAILED
    assert post.last_error is not None
    assert "boom" in post.last_error


async def test_resolve_post_redelivers_without_recomputing_when_already_resolved(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    resolve_track_mock = AsyncMock()
    monkeypatch.setattr(worker_tasks, "resolve_track", resolve_track_mock)

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    set_post_status(post, PostStatus.RESOLVED)
    post.caption = "already resolved"
    await db_session.commit()

    await worker_tasks._resolve_post(post_id)

    resolve_track_mock.assert_not_awaited()
    deliver_draft_mock.assert_awaited_once()


async def test_resolve_post_returns_early_for_unknown_post_id(
    db_session: AsyncSession,
) -> None:
    await worker_tasks._resolve_post(999999999)  # must not raise


async def test_delivery_failure_leaves_the_post_failed_with_its_caption(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    point_task_at_test_database: AsyncMock,
) -> None:
    """A1/D-38/F-1: RESOLVED now means "the draft is in the admin's chat".
    A delivery failure happens while the post is still RESOLVING, whose edge
    to FAILED is legal — the old order raised IllegalStatusTransitionError
    from inside the error handler and rolled everything back with no alert.
    """
    resolved = ResolvedTrack(
        isrc="GBAYE9200070",
        title="Creep",
        artists=[ArtistInfo(name="Radiohead")],
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        worker_tasks, "deliver_draft", AsyncMock(side_effect=RuntimeError("telegram down"))
    )

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None

    assert post.status is PostStatus.FAILED
    assert post.last_error is not None
    assert "telegram down" in post.last_error
    assert post.caption is not None
    assert "Creep" in post.caption
    assert post.draft_message_id is None
    point_task_at_test_database.assert_awaited()


async def test_cleanup_removes_only_the_post_when_the_track_has_another_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """F-26: `ON DELETE RESTRICT` protects the track, but the COUNT guard is
    what has to notice — and the second post must be left untouched."""
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=ResolvedTrack()))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track_id = post.track_id

    second_post = Post(track_id=track_id, created_by_user_id=222)
    db_session.add(second_post)
    await db_session.flush()
    second_post_id = second_post.id
    await db_session.commit()

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    assert await db_session.get(Post, post_id) is None
    assert await db_session.get(Post, second_post_id) is not None
    assert await db_session.get(Track, track_id) is not None


async def test_re_resolve_keeps_the_manual_link_and_the_artist_hashtag(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """B2/D-42/F-3: the stored row holds the only two things automation can
    never reproduce — a `manual` link and the admin's hashtag override. The
    fresh resolve deliberately returns neither, plus loses Deezer entirely.
    """
    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track_id = track.id

    track.artists = [{"name": "0%Mercury", "hashtag": "ZeroPercentMercury", "spotify_id": None}]
    track.platform_links = {
        "spotify": {
            "url": "https://open.spotify.com/track/manual",
            "source": "user_input",
            "match": "manual",
        },
        "deezer": {
            "url": "https://www.deezer.com/track/9",
            "source": "deezer",
            "match": "exact_isrc",
        },
    }
    await db_session.commit()

    resolved = ResolvedTrack(
        isrc=f"UA{uuid.uuid4().hex[:3].upper()}2500003",
        title="Покаяння",
        artists=[ArtistInfo(name="0%Mercury")],
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/automatic",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded = await db_session.get(Track, track_id)
    assert reloaded is not None

    stored_links = dict(reloaded.platform_links)
    assert stored_links["spotify"] == {
        "url": "https://open.spotify.com/track/manual",
        "source": "user_input",
        "match": "manual",
    }
    assert stored_links["deezer"] == {
        "url": "https://www.deezer.com/track/9",
        "source": "deezer",
        "match": "exact_isrc",
    }
    assert reloaded.artists == [
        {"name": "0%Mercury", "hashtag": "ZeroPercentMercury", "spotify_id": None}
    ]

    reloaded_post = await db_session.get(Post, post_id)
    assert reloaded_post is not None
    assert reloaded_post.caption is not None
    assert "#ZeroPercentMercury" in reloaded_post.caption


async def test_duplicate_isrc_raises_an_admin_warning(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """B3/D-41/M23: a Spotify link today and a YouTube link for the same
    recording tomorrow produce two tracks that `source_url_normalized` can
    never connect — only the ISRC can."""
    shared_isrc = f"UA{uuid.uuid4().hex[:3].upper()}2500001"
    earlier_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    earlier_track = Track(
        source_url=earlier_url, source_url_normalized=earlier_url, isrc=shared_isrc
    )
    db_session.add(earlier_track)
    await db_session.flush()
    earlier_track_id = earlier_track.id
    await db_session.commit()

    resolved = ResolvedTrack(
        isrc=shared_isrc,
        title="Creep",
        artists=[ArtistInfo(name="Radiohead")],
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None
    assert post.status is PostStatus.RESOLVED

    alert_texts = [call.args[2] for call in point_task_at_test_database.await_args_list]
    assert any(shared_isrc in text and str(earlier_track_id) in text for text in alert_texts)


async def test_no_duplicate_warning_for_the_first_track_with_that_isrc(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    resolved = ResolvedTrack(
        isrc=f"UA{uuid.uuid4().hex[:3].upper()}2500002",
        title="Creep",
        artists=[ArtistInfo(name="Radiohead")],
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    point_task_at_test_database.assert_not_awaited()


async def test_redelivery_failure_alerts_instead_of_stranding_the_post(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    point_task_at_test_database: AsyncMock,
) -> None:
    """F-14: RESOLVED has no edge back to RESOLVING and `/retry` is closed
    there, so swallowing this left the post with no draft and no way out."""
    monkeypatch.setattr(
        worker_tasks, "deliver_draft", AsyncMock(side_effect=RuntimeError("telegram down"))
    )

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    set_post_status(post, PostStatus.RESOLVED)
    post.caption = "already resolved"
    await db_session.commit()

    await worker_tasks._resolve_post(post_id)

    point_task_at_test_database.assert_awaited_once()
    alert_text = point_task_at_test_database.await_args_list[0].args[2]
    assert "telegram down" in alert_text


async def test_a_commit_failure_after_delivery_still_reaches_failed_with_an_alert(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """R2-2: SQLAlchemy rolls back a failed commit, which expires every ORM
    object — the old `_fail_post(post, ...)` then raised `MissingGreenlet`
    from inside the error handler itself, and D-43 had closed `/retry` from
    `RESOLVING`, so the post was stranded with a draft already in the chat.
    """
    resolved = ResolvedTrack(
        isrc="GBAYE9200070",
        title="Creep",
        artists=[ArtistInfo(name="Radiohead")],
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    original_commit = AsyncSession.commit
    failures_left = 1

    async def flaky_commit(self: AsyncSession) -> None:
        nonlocal failures_left
        if failures_left:
            failures_left -= 1
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        await original_commit(self)

    post_id = await make_resolving_post(db_session)
    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    await worker_tasks._resolve_post(post_id)

    monkeypatch.undo()
    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None

    assert post.status is PostStatus.FAILED
    point_task_at_test_database.assert_awaited()


async def test_an_empty_re_resolve_never_nulls_the_stored_isrc(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """R2-4: scalars were assigned straight from the fresh result, `None`
    included, so a transient source failure wrote `isrc = NULL` and the D-41
    duplicate warning for that track could then never fire again."""
    stored_isrc = f"UA{uuid.uuid4().hex[:3].upper()}2500004"
    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track_id = track.id

    track.isrc = stored_isrc
    track.title = "Покаяння"
    track.year = 2025
    track.resolved_at = datetime.now(UTC)
    await db_session.commit()

    # Only a link comes back this time: no ISRC, no title, no year.
    resolved = ResolvedTrack(
        platform_links={
            "deezer": PlatformLink(
                url="https://www.deezer.com/track/77",
                source="deezer",
                match=MatchClass.CONFIRMED,
            )
        }
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded = await db_session.get(Track, track_id)
    assert reloaded is not None

    assert reloaded.isrc == stored_isrc
    assert reloaded.title == "Покаяння"
    assert reloaded.year == 2025


async def test_an_empty_genre_result_does_not_wipe_the_stored_genre_line(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """R2-4, the `post.genres` half: Deezer not answering is not the same
    statement as "this release has no genres"."""
    resolved = ResolvedTrack(
        title="Creep",
        artists=[ArtistInfo(name="Radiohead")],
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    post.genres = [{"name": "рок", "hashtag": None, "source": "deezer"}]
    await db_session.commit()

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded_post = await db_session.get(Post, post_id)
    assert reloaded_post is not None

    assert reloaded_post.genres == [{"name": "рок", "hashtag": None, "source": "deezer"}]


async def test_the_track_lock_does_not_block_a_concurrent_post_insert(
    db_session: AsyncSession, migrated_test_database_url: str
) -> None:
    """R2-3: Postgres takes `FOR KEY SHARE` on the parent row for every
    `INSERT INTO post`, and that conflicts with `FOR UPDATE` but not with
    `FOR NO KEY UPDATE`. Held for a whole resolve, the old lock left the
    admin without any answer for up to the 360 s task ceiling.
    """
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    track = Track(source_url=marker_url, source_url_normalized=marker_url)
    db_session.add(track)
    await db_session.flush()
    track_id = track.id
    await db_session.commit()

    engine = build_engine(migrated_test_database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as holder:
            locked = await holder.get(Track, track_id, with_for_update={"key_share": True})
            assert locked is not None

            async with session_factory() as writer:
                await writer.execute(text("SET LOCAL lock_timeout = '3s'"))
                writer.add(Post(track_id=track_id, created_by_user_id=222))
                await asyncio.wait_for(writer.flush(), timeout=10)
                await writer.rollback()

            await holder.rollback()
    finally:
        await engine.dispose()
