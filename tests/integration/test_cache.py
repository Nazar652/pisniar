"""`resolve.cache.SourceCache`: hit/miss, negative caching, the one-hour TTL, and two
invariants: a cache write is visible to other tasks at once, and it never touches the
caller's business transaction, not even when it loses a race.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import SourceFetch, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.resolve.cache import SourceCache

pytestmark = pytest.mark.integration


def unique_key(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


async def stored_row(session: AsyncSession, request_key: str) -> SourceFetch | None:
    return await session.scalar(
        select(SourceFetch).where(
            SourceFetch.source == "deezer", SourceFetch.request_key == request_key
        )
    )


async def test_cache_miss_calls_fetch_and_persists_result(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    request_key = unique_key("isrc:MISS")
    call_count = 0

    async def fetch() -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"found": True, "data": {"id": "123"}}

    payload = await source_cache.get_or_fetch("deezer", request_key, fetch)

    assert payload == {"found": True, "data": {"id": "123"}}
    assert call_count == 1
    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert stored.payload == {"found": True, "data": {"id": "123"}}


async def test_cache_hit_never_calls_fetch(source_cache: SourceCache) -> None:
    request_key = unique_key("isrc:HIT")

    async def first_fetch() -> dict[str, object]:
        return {"found": False}

    await source_cache.get_or_fetch("deezer", request_key, first_fetch)

    async def second_fetch() -> dict[str, object]:
        raise AssertionError("fetch() must not be called on a fresh cache hit")

    payload = await source_cache.get_or_fetch("deezer", request_key, second_fetch)

    assert payload == {"found": False}


async def test_negative_result_is_cached_too(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    request_key = unique_key("isrc:MISSING")

    async def fetch() -> dict[str, object]:
        return {"found": False}

    payload = await source_cache.get_or_fetch("deezer", request_key, fetch)

    assert payload == {"found": False}
    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert stored.payload == {"found": False}


async def test_stale_row_is_a_miss_and_is_refreshed_in_place(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    """Otherwise a negative answer for a release song.link has not indexed yet would
    outlive every `/retry` the admin sends."""
    request_key = unique_key("isrc:STALE")
    db_session.add(
        SourceFetch(
            source="deezer",
            request_key=request_key,
            payload={"found": False},
            fetched_at=datetime.now(UTC) - timedelta(seconds=source_cache.ttl_seconds + 60),
        )
    )
    await db_session.commit()

    async def fetch() -> dict[str, object]:
        return {"found": True, "data": "fresh"}

    payload = await source_cache.get_or_fetch("deezer", request_key, fetch)

    assert payload == {"found": True, "data": "fresh"}

    db_session.expire_all()
    row_count = await db_session.scalar(
        select(func.count())
        .select_from(SourceFetch)
        .where(SourceFetch.source == "deezer", SourceFetch.request_key == request_key)
    )
    assert row_count == 1
    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert stored.payload == {"found": True, "data": "fresh"}


async def test_fetched_at_is_written_from_the_clock_at_write_time(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    """`server_default now()` is the transaction's start time, which a long resolve
    drags tens of seconds into the past."""
    request_key = unique_key("isrc:CLOCK")

    async def fetch() -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"found": True}

    before_fetch = datetime.now(UTC)
    await source_cache.get_or_fetch("deezer", request_key, fetch)
    after_write = datetime.now(UTC)

    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert before_fetch <= stored.fetched_at <= after_write


async def test_cache_write_is_visible_before_the_business_transaction_commits(
    source_cache: SourceCache, db_session: AsyncSession, migrated_test_database_url: str
) -> None:
    """Two tasks on the same artist must share cached answers instead of each
    re-running nine throttled requests and then racing on UNIQUE."""
    request_key = unique_key("isrc:VISIBLE")
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"
    db_session.add(Track(source_url=marker_url, source_url_normalized=marker_url))
    await db_session.flush()

    async def fetch() -> dict[str, object]:
        return {"found": True, "data": "shared"}

    await source_cache.get_or_fetch("deezer", request_key, fetch)

    engine = build_engine(migrated_test_database_url)
    try:
        async with build_session_factory(engine)() as observer_session:
            visible = await stored_row(observer_session, request_key)
    finally:
        await engine.dispose()

    assert visible is not None
    assert visible.payload == {"found": True, "data": "shared"}


async def test_losing_the_unique_race_leaves_the_business_transaction_intact(
    migrated_test_database_url: str,
) -> None:
    """The loser's INSERT runs before the winner commits, while its business session
    holds uncommitted work and an ORM object that must survive both."""
    engine = build_engine(migrated_test_database_url)
    session_factory = build_session_factory(engine)
    cache = SourceCache(session_factory=session_factory)
    request_key = unique_key("isrc:RACED")
    marker_url = f"https://open.spotify.com/track/{uuid.uuid4().hex}"

    try:
        async with session_factory() as business_session, session_factory() as winner_session:
            track = Track(source_url=marker_url, source_url_normalized=marker_url, title="pending")
            business_session.add(track)
            await business_session.flush()

            winner_session.add(
                SourceFetch(
                    source="deezer",
                    request_key=request_key,
                    payload={"found": True, "data": "winner"},
                    fetched_at=datetime.now(UTC),
                )
            )
            # Flushed, not committed: the loser below cannot see this row and
            # blocks on the UNIQUE index instead of reading it.
            await winner_session.flush()

            async def release_winner() -> None:
                await asyncio.sleep(0.2)
                await winner_session.commit()

            async def loser_fetch() -> dict[str, object]:
                return {"found": True, "data": "loser"}

            async def run_loser() -> dict[str, object]:
                return await cache.get_or_fetch("deezer", request_key, loser_fetch)

            _, loser_payload = await asyncio.gather(release_winner(), run_loser())

            assert loser_payload == {"found": True, "data": "loser"}

            # A rollback would have expired `track`, turning this plain
            # attribute read into a lazy refresh and a MissingGreenlet.
            assert track.title == "pending"
            assert business_session.in_transaction()

            await business_session.commit()

        async with session_factory() as verification_session:
            survivor = await verification_session.scalar(
                select(Track).where(Track.source_url_normalized == marker_url)
            )
            assert survivor is not None
            assert survivor.title == "pending"
    finally:
        await engine.dispose()


async def test_a_negative_payload_never_evicts_a_still_fresh_positive_row(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    """Otherwise the last writer wins: a quota error body seconds after a valid album
    blanks the genres of that whole release for the hour, with no request in the logs."""
    request_key = unique_key("album:GUARD")

    await source_cache.get_or_fetch("deezer", request_key, lambda: _payload({"genres": [85]}))
    await source_cache._upsert("deezer", request_key, {"found": False})

    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert stored.payload == {"found": True, "data": {"genres": [85]}}


async def test_a_negative_payload_does_replace_another_negative_one(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    """The guard is about quality, not about freezing the row: a negative may
    always refresh a negative, so the TTL still does its job."""
    request_key = unique_key("album:NEGNEG")

    await source_cache._upsert("deezer", request_key, {"found": False})
    await source_cache._upsert("deezer", request_key, {"found": False, "marker": "second"})

    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert stored.payload == {"found": False, "marker": "second"}


async def test_an_empty_result_list_does_not_evict_a_non_empty_one(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    """The search-shaped payload of the same rule: `{"results": []}` reads
    exactly like "nothing found" and must not overwrite real hits."""
    request_key = unique_key("search:GUARD")

    await source_cache._upsert("deezer", request_key, {"results": [{"id": 1}]})
    await source_cache._upsert("deezer", request_key, {"results": []})

    stored = await stored_row(db_session, request_key)
    assert stored is not None
    assert stored.payload == {"results": [{"id": 1}]}


async def test_a_negative_payload_replaces_a_stale_positive_row(
    source_cache: SourceCache, db_session: AsyncSession
) -> None:
    """Past the TTL the stored positive carries no more authority than the
    fresh negative, so the refresh path stays open."""
    request_key = unique_key("album:STALE")

    await source_cache.get_or_fetch("deezer", request_key, lambda: _payload({"genres": [85]}))
    stored = await stored_row(db_session, request_key)
    assert stored is not None
    stored.fetched_at = datetime.now(UTC) - timedelta(seconds=source_cache.ttl_seconds + 60)
    await db_session.commit()

    await source_cache._upsert("deezer", request_key, {"found": False})

    db_session.expire_all()
    refreshed = await stored_row(db_session, request_key)
    assert refreshed is not None
    assert refreshed.payload == {"found": False}


async def _payload(data: dict[str, object]) -> dict[str, object]:
    return {"found": True, "data": data}
