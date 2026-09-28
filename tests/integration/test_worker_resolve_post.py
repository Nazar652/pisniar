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
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.resolve.matching import is_valid_isrc
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


def unique_isrc() -> str:
    # Tracks of earlier runs stay in the test database, so the random part has to be wide.
    return f"UA{uuid.uuid4().hex[:3].upper()}{uuid.uuid4().int % 10**7:07d}"


def test_unique_isrc_is_a_valid_isrc() -> None:
    assert all(is_valid_isrc(unique_isrc()) for _ in range(1000))


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


async def test_the_caption_is_built_from_the_row_not_from_the_resolve(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """The album id must round-trip through its column, and the languages the
    admin sets must beat the schema default no resolve overwrites."""
    resolved = ResolvedTrack(
        title="Сумніви",
        artists=[ArtistInfo(name="Тол")],
        year=2013,
        release_type="album",
        release_title="Синдром Бажання",
        album_id_spotify="4yYXqedvSKuJlgI2IWsB0j",
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track.languages = ["українська", "англійська"]
    await db_session.commit()

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded_post = await db_session.get(Post, post_id)
    assert reloaded_post is not None
    reloaded_track = await db_session.get(Track, reloaded_post.track_id)
    assert reloaded_track is not None

    assert reloaded_track.album_id_spotify == "4yYXqedvSKuJlgI2IWsB0j"
    assert reloaded_post.caption is not None
    assert "https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j" in reloaded_post.caption
    assert "#українська #англійська" in reloaded_post.caption
    assert reloaded_track.languages == ["українська", "англійська"]


async def test_resolve_post_cleans_up_a_track_that_was_never_resolved(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """With no `resolved_at` and no stored links there is nothing to lose, so an
    empty first resolve removes the row."""
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
    """With the one-hour TTL a re-fetch is routine, and deleting here would take a
    resolved track's manual link and hashtag with it, irreversibly."""
    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track_id = track.id

    track.resolved_at = datetime.now(UTC)
    track.isrc = unique_isrc()
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

    # This track really was resolved once before, so the re-resolve wording applies.
    assert reloaded_post.last_error is not None
    assert "збережені дані" in reloaded_post.last_error


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
    """RESOLVED means the draft is in the admin's chat, so a delivery failure
    happens while the post is still RESOLVING, which may legally fail."""
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
    """`ON DELETE RESTRICT` protects the track, but the COUNT guard is what has
    to notice, and the second post must be left untouched."""
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
    """A `manual` link and the admin's hashtag are what automation never
    reproduces; the fresh resolve returns neither and loses Deezer too."""
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
        isrc=unique_isrc(),
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
    """A Spotify link and a YouTube link of one recording make two tracks that
    `source_url_normalized` never connects; only the ISRC can."""
    shared_isrc = unique_isrc()
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
        isrc=unique_isrc(),
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
    """RESOLVED has no edge back to RESOLVING and `/retry` is closed there, so
    swallowing this would leave the post with no draft and no way out."""
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
    """A failed commit expires every ORM object; touching the expired post in the
    error handler raises `MissingGreenlet` and strands a post whose draft is out."""
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
    """A transient source failure must not write `isrc = NULL`: the duplicate-ISRC
    warning for that track could then never fire again."""
    stored_isrc = unique_isrc()
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
    """Deezer not answering is not the same statement as "this release has no
    genres"."""
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
    # The caption is built from the kept genres too, or the two columns of one
    # row would contradict each other.
    assert reloaded_post.caption is not None
    assert "#рок" in reloaded_post.caption


async def test_the_track_lock_does_not_block_a_concurrent_post_insert(
    db_session: AsyncSession, migrated_test_database_url: str
) -> None:
    """Every `INSERT INTO post` takes `FOR KEY SHARE` on the track, which conflicts
    with `FOR UPDATE` but not with `FOR NO KEY UPDATE`."""
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


async def test_a_permanent_commit_failure_still_reaches_the_admin_with_an_alert(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """Telegram does not depend on Postgres, so a Postgres that is down for good
    must not take the alert with it."""
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

    async def always_failing_commit(self: AsyncSession) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    post_id = await make_resolving_post(db_session)
    monkeypatch.setattr(AsyncSession, "commit", always_failing_commit)

    await worker_tasks._resolve_post(post_id)

    monkeypatch.undo()
    point_task_at_test_database.assert_awaited()
    alert_text = point_task_at_test_database.await_args_list[-1].args[2]
    assert "resolving" in alert_text


async def test_a_locked_post_row_fails_fast_and_still_alerts(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    migrated_test_database_url: str,
    point_task_at_test_database: AsyncMock,
) -> None:
    """With `lock_timeout` every lock acquisition can raise, the post lock
    included, and it must still end in a status and an alert."""
    monkeypatch.setattr(worker_tasks, "LOCK_TIMEOUT_MS", 300)

    post_id = await make_resolving_post(db_session)

    engine = build_engine(migrated_test_database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as holder:
            assert await holder.get(Post, post_id, with_for_update=True) is not None

            await asyncio.wait_for(worker_tasks._resolve_post(post_id), timeout=30)

            await holder.rollback()
    finally:
        await engine.dispose()

    point_task_at_test_database.assert_awaited()
    alert_text = point_task_at_test_database.await_args_list[-1].args[2]
    assert "заблоковано" in alert_text


async def test_a_youtube_input_whose_resolve_found_nothing_delivers_no_draft(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """The clip is seeded into `platform_links` before any network call, so it
    must not make an empty resolve look like a result."""
    video_url = f"https://www.youtube.com/watch?v={uuid.uuid4().hex[:11]}"
    track = Track(source_url=video_url, source_url_normalized=video_url)
    track.platform_links = {
        "youtube": {"url": video_url, "source": "user_input", "match": "manual"}
    }
    db_session.add(track)
    await db_session.flush()
    track_id = track.id

    post = Post(track_id=track.id, created_by_user_id=111)
    db_session.add(post)
    await db_session.flush()
    set_post_status(post, PostStatus.RESOLVING)
    post_id = post.id
    await db_session.commit()

    resolved = ResolvedTrack(
        platform_links={
            "youtube": PlatformLink(url=video_url, source="user_input", match=MatchClass.MANUAL)
        }
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded_post = await db_session.get(Post, post_id)
    assert reloaded_post is not None

    assert reloaded_post.status is PostStatus.FAILED
    assert reloaded_post.caption is None
    deliver_draft_mock.assert_not_awaited()
    assert await db_session.get(Track, track_id) is not None

    # The empty-resolve wording, not the missing-title one: the difference
    # is precisely whether the seeded clip counted as a resolve result.
    assert reloaded_post.last_error is not None
    assert "не повернув нічого" in reloaded_post.last_error

    # On a first resolve the only stored link is the admin's own URL, so the
    # re-resolve wording ("трек уже має збережені дані") would be false.
    assert "ваше власне посилання" in reloaded_post.last_error
    assert "збережені дані" not in reloaded_post.last_error


async def test_a_resolve_without_a_title_is_not_delivered_as_a_draft(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """Links without a track name are a failed resolve; marking it RESOLVED would
    make that failure indistinguishable from success."""
    resolved = ResolvedTrack(
        platform_links={
            "spotify": PlatformLink(
                url="https://open.spotify.com/track/x",
                source="spotify",
                match=MatchClass.EXACT_ISRC,
            )
        }
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    post = await db_session.get(Post, post_id)
    assert post is not None

    assert post.status is PostStatus.FAILED
    deliver_draft_mock.assert_not_awaited()


async def test_cleanup_releases_the_track_lock_before_talking_to_telegram(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    migrated_test_database_url: str,
    deliver_draft_mock: AsyncMock,
) -> None:
    """The `remaining_posts != 0` branch must end its transaction before
    `build_bot()` and `send_alert()`, or the bot's `INSERT INTO post` waits behind it."""
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=ResolvedTrack()))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track_id = post.track_id

    db_session.add(Post(track_id=track_id, created_by_user_id=222))
    await db_session.commit()

    engine = build_engine(migrated_test_database_url)
    session_factory = build_session_factory(engine)
    lock_was_free = False

    async def probe_the_track_lock(bot: object, chat_id: int, text_message: str) -> bool:
        nonlocal lock_was_free
        async with session_factory() as probe:
            await probe.execute(
                select(Track.id).where(Track.id == track_id).with_for_update(nowait=True)
            )
            lock_was_free = True
            await probe.rollback()
        return True

    monkeypatch.setattr(worker_tasks, "send_alert", AsyncMock(side_effect=probe_the_track_lock))
    try:
        await asyncio.wait_for(worker_tasks._resolve_post(post_id), timeout=30)
    finally:
        await engine.dispose()

    assert lock_was_free is True


async def test_two_posts_of_the_same_track_both_resolve_successfully(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, deliver_draft_mock: AsyncMock
) -> None:
    """Holding the track lock across the whole resolve would make the second post
    of the same track hit `lock_timeout`."""
    monkeypatch.setattr(worker_tasks, "LOCK_TIMEOUT_MS", 300)
    shared_isrc = unique_isrc()

    async def slow_resolve(*args: object, **kwargs: object) -> ResolvedTrack:
        await asyncio.sleep(1.0)
        return ResolvedTrack(
            isrc=shared_isrc,
            title="Покаяння",
            artists=[ArtistInfo(name="Burned Time Machine")],
            platform_links={
                "spotify": PlatformLink(
                    url="https://open.spotify.com/track/x",
                    source="spotify",
                    match=MatchClass.EXACT_ISRC,
                )
            },
        )

    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(side_effect=slow_resolve))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    first_post_id = await make_resolving_post(db_session)
    first_post = await db_session.get(Post, first_post_id)
    assert first_post is not None

    second_post = Post(track_id=first_post.track_id, created_by_user_id=222)
    db_session.add(second_post)
    await db_session.flush()
    set_post_status(second_post, PostStatus.RESOLVING)
    second_post_id = second_post.id
    await db_session.commit()

    await asyncio.wait_for(
        asyncio.gather(
            worker_tasks._resolve_post(first_post_id), worker_tasks._resolve_post(second_post_id)
        ),
        timeout=60,
    )

    db_session.expire_all()
    for resolved_post_id in (first_post_id, second_post_id):
        reloaded = await db_session.get(Post, resolved_post_id)
        assert reloaded is not None
        assert reloaded.status is PostStatus.RESOLVED, reloaded.last_error


async def test_a_failed_source_is_reported_to_the_admin_after_delivery(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """Isolating a failing source trades a loud failure for a poorer post, which
    is acceptable only while the admin still hears about it."""
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
        source_failures=["deezer:isrc: quota limit exceeded"],
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
    assert any("quota limit exceeded" in text for text in alert_texts)

    # `/retry` from RESOLVED is refused, so the alert has to name the one route
    # that does work, otherwise it reports without a remedy.
    assert any("надішліть посилання ще раз" in text for text in alert_texts)


async def test_a_cleanup_whose_database_write_fails_still_reaches_the_admin(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """`_clean_up_non_track` runs outside the task's `try`, so a Postgres restart
    must not leave the post `resolving` with no `last_error` and no alert."""
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=ResolvedTrack()))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)

    async def always_failing_commit(self: AsyncSession) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(AsyncSession, "commit", always_failing_commit)

    await worker_tasks._resolve_post(post_id)

    monkeypatch.undo()
    point_task_at_test_database.assert_awaited()
    alert_text = point_task_at_test_database.await_args_list[-1].args[2]
    assert "прибрати запис не вдалося" in alert_text

    db_session.expire_all()
    surviving_post = await db_session.get(Post, post_id)
    assert surviving_post is not None
    assert surviving_post.status is PostStatus.RESOLVING


async def test_a_failed_source_is_named_in_the_cleanup_alert_too(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """Deletion is the loudest outcome this task has, so it must say which
    source's silence produced it."""
    resolved = ResolvedTrack(source_failures=["deezer:isrc: quota limit exceeded"])
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    post_id = await make_resolving_post(db_session)
    await worker_tasks._resolve_post(post_id)

    point_task_at_test_database.assert_awaited()
    alert_text = point_task_at_test_database.await_args_list[-1].args[2]
    assert "quota limit exceeded" in alert_text


async def test_a_slow_draft_delivery_does_not_fail_the_second_post_of_the_track(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    point_task_at_test_database: AsyncMock,
) -> None:
    """`deliver_draft` must not run between the track lock and its commit:
    aiogram's default session timeout is 60 s."""
    monkeypatch.setattr(worker_tasks, "LOCK_TIMEOUT_MS", 300)
    shared_isrc = unique_isrc()

    async def slow_delivery(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(1.0)

    monkeypatch.setattr(worker_tasks, "deliver_draft", AsyncMock(side_effect=slow_delivery))
    monkeypatch.setattr(
        worker_tasks,
        "resolve_track",
        AsyncMock(
            return_value=ResolvedTrack(
                isrc=shared_isrc,
                title="Покаяння",
                artists=[ArtistInfo(name="Burned Time Machine")],
                platform_links={
                    "spotify": PlatformLink(
                        url="https://open.spotify.com/track/x",
                        source="spotify",
                        match=MatchClass.EXACT_ISRC,
                    )
                },
            )
        ),
    )
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    first_post_id = await make_resolving_post(db_session)
    first_post = await db_session.get(Post, first_post_id)
    assert first_post is not None

    second_post = Post(track_id=first_post.track_id, created_by_user_id=222)
    db_session.add(second_post)
    await db_session.flush()
    set_post_status(second_post, PostStatus.RESOLVING)
    second_post_id = second_post.id
    await db_session.commit()

    await asyncio.wait_for(
        asyncio.gather(
            worker_tasks._resolve_post(first_post_id), worker_tasks._resolve_post(second_post_id)
        ),
        timeout=60,
    )

    db_session.expire_all()
    for resolved_post_id in (first_post_id, second_post_id):
        reloaded = await db_session.get(Post, resolved_post_id)
        assert reloaded is not None
        assert reloaded.status is PostStatus.RESOLVED, reloaded.last_error


async def test_a_re_resolve_without_a_fresh_title_uses_the_stored_one(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """The title guard runs after `merge_stored_scalars`, or a nameless answer
    would fail a post whose track already has a title."""
    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None

    track.title = "Покаяння"
    track.artists = [{"name": "Burned Time Machine", "hashtag": None, "spotify_id": None}]
    track.resolved_at = datetime.now(UTC)
    await db_session.commit()

    resolved = ResolvedTrack(
        artists=[ArtistInfo(name="Burned Time Machine")],
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

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded = await db_session.get(Post, post_id)
    assert reloaded is not None
    assert reloaded.status is PostStatus.RESOLVED, reloaded.last_error
    assert reloaded.caption is not None
    assert "Покаяння" in reloaded.caption
    deliver_draft_mock.assert_awaited_once()


async def test_the_duplicate_warning_closes_its_read_transaction_before_telegram(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate check's SELECT opens a transaction, which must not stay open
    through `build_bot()` and two `send_alert` calls."""
    shared_isrc = unique_isrc()
    marker = uuid.uuid4().hex
    earlier_url = f"https://open.spotify.com/track/earlier{marker}"
    earlier_track = Track(
        source_url=earlier_url, source_url_normalized=earlier_url, isrc=shared_isrc
    )
    db_session.add(earlier_track)

    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track.isrc = shared_isrc
    await db_session.commit()

    open_transaction_during_alert: list[bool] = []

    async def probe(bot: object, chat_id: int, text_message: str) -> bool:
        open_transaction_during_alert.append(db_session.in_transaction())
        return True

    monkeypatch.setattr(worker_tasks, "send_alert", AsyncMock(side_effect=probe))

    await worker_tasks._warn_about_isrc_duplicate(db_session, post, track)

    assert open_transaction_during_alert == [False]


async def test_a_degraded_re_resolve_does_not_downgrade_the_stored_title(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    deliver_draft_mock: AsyncMock,
    point_task_at_test_database: AsyncMock,
) -> None:
    """The fresh title is the `songlink:youtube` clip name, not `None`, so
    `merge_stored_scalars` would take it; only the degraded-resolve guard stops it."""
    post_id = await make_resolving_post(db_session)
    post = await db_session.get(Post, post_id)
    assert post is not None
    track = await db_session.get(Track, post.track_id)
    assert track is not None
    track_id = track.id

    track.title = "Bruise Pristine"
    track.artists = [{"name": "Placebo", "hashtag": None, "spotify_id": None}]
    track.resolved_at = datetime.now(UTC)
    await db_session.commit()

    resolved = ResolvedTrack(
        title="Bruise Pristine (Official Video)",
        artists=[ArtistInfo(name="Валентин Стрыкало-Бувайте")],
        source_failures=["spotify:bridge: HTTP 503"],
        platform_links={
            "youtubeMusic": PlatformLink(
                url="https://music.youtube.com/watch?v=abc",
                source="ytmusic",
                match=MatchClass.CONFIRMED,
            )
        },
    )
    monkeypatch.setattr(worker_tasks, "resolve_track", AsyncMock(return_value=resolved))
    monkeypatch.setattr(worker_tasks, "resolve_genres", AsyncMock(return_value=[]))

    await worker_tasks._resolve_post(post_id)

    db_session.expire_all()
    reloaded_track = await db_session.get(Track, track_id)
    assert reloaded_track is not None
    assert reloaded_track.title == "Bruise Pristine"
    assert reloaded_track.artists == [{"name": "Placebo", "hashtag": None, "spotify_id": None}]

    reloaded_post = await db_session.get(Post, post_id)
    assert reloaded_post is not None
    assert reloaded_post.status is PostStatus.RESOLVED, reloaded_post.last_error
    assert reloaded_post.caption is not None
    assert "Bruise Pristine" in reloaded_post.caption
    assert "Official Video" not in reloaded_post.caption
    assert "Валентин" not in reloaded_post.caption
