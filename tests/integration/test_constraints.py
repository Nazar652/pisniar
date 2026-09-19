"""Schema-level guarantees the ORM alone cannot enforce: the status CHECK,
the fragment-bounds CHECK, the partial unique index, and `ON DELETE
RESTRICT` on `post.track_id`."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, SourceFetch, Track

pytestmark = pytest.mark.integration


async def make_track(session: AsyncSession) -> Track:
    marker = uuid.uuid4().hex
    track = Track(source_url=f"test://{marker}", source_url_normalized=f"test://{marker}")
    session.add(track)
    await session.flush()
    return track


async def test_status_check_constraint_rejects_unknown_status(db_session: AsyncSession) -> None:
    track = await make_track(db_session)

    with pytest.raises(IntegrityError, match="ck_post_status_valid"):
        await db_session.execute(
            text(
                "INSERT INTO post (track_id, status, created_by_user_id) "
                "VALUES (:track_id, 'nonsense', 1)"
            ),
            {"track_id": track.id},
        )


async def test_fragment_bounds_check_rejects_end_before_start(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = Post(
        track_id=track.id,
        created_by_user_id=1,
        fragment_start_ms=1000,
        fragment_end_ms=500,
    )
    db_session.add(post)

    with pytest.raises(IntegrityError, match="ck_post_fragment_bounds_valid"):
        await db_session.flush()


async def test_fragment_bounds_check_rejects_one_sided_bound(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, fragment_start_ms=5000)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_fragment_bounds_valid"):
        await db_session.flush()


async def test_fragment_bounds_check_rejects_negative_start(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = Post(
        track_id=track.id,
        created_by_user_id=1,
        fragment_start_ms=-2000,
        fragment_end_ms=5000,
    )

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_fragment_bounds_valid"):
        await db_session.flush()


async def test_fragment_bounds_check_allows_null_bounds(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1)
    db_session.add(post)

    await db_session.flush()  # must not raise

    assert post.id is not None


async def test_partial_unique_index_rejects_duplicate_channel_message(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(
        Post(track_id=track.id, created_by_user_id=1, channel_chat_id=1, channel_message_id=42)
    )
    await db_session.flush()

    db_session.add(
        Post(track_id=track.id, created_by_user_id=1, channel_chat_id=1, channel_message_id=42)
    )
    with pytest.raises(IntegrityError, match="uq_post_channel_message"):
        await db_session.flush()


async def test_partial_unique_index_allows_multiple_null_channel_message(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1))
    db_session.add(Post(track_id=track.id, created_by_user_id=1))

    await db_session.flush()  # must not raise: NULL never equals NULL


async def test_channel_ref_check_rejects_a_one_sided_pair(db_session: AsyncSession) -> None:
    """Same one-sided-pair hazard as the fragment bounds: `channel_chat_id`
    alone (or `channel_message_id` alone) would otherwise pass the partial
    unique index untouched — its predicate only fires once *both* columns
    are non-NULL — and silently leave a post with half a channel reference."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, channel_chat_id=1)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_channel_ref_complete"):
        await db_session.flush()


async def test_partial_unique_index_rejects_duplicate_draft_message(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(
        Post(track_id=track.id, created_by_user_id=1, draft_chat_id=1, draft_message_id=42)
    )
    await db_session.flush()

    db_session.add(
        Post(track_id=track.id, created_by_user_id=1, draft_chat_id=1, draft_message_id=42)
    )
    with pytest.raises(IntegrityError, match="uq_post_draft_message"):
        await db_session.flush()


async def test_draft_ref_check_rejects_a_one_sided_pair(db_session: AsyncSession) -> None:
    """Same one-sided-pair hazard as `channel_ref_complete`, applied to the
    draft delivery columns (idempotency guard for redelivery, D-28)."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, draft_chat_id=1)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_draft_ref_complete"):
        await db_session.flush()


async def test_source_fetch_unique_constraint_rejects_duplicate_source_and_request_key(
    db_session: AsyncSession,
) -> None:
    db_session.add(SourceFetch(source="deezer", request_key="isrc:ABC123", payload={"found": True}))
    await db_session.flush()

    db_session.add(
        SourceFetch(source="deezer", request_key="isrc:ABC123", payload={"found": False})
    )
    with pytest.raises(IntegrityError, match="uq_source_fetch_source_request_key"):
        await db_session.flush()


async def test_track_deletion_is_restricted_while_a_post_references_it(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1))
    await db_session.flush()

    # Raw SQL, not `session.delete(track)`: asserts the database constraint
    # directly, independent of the ORM's `passive_deletes` configuration (D-23).
    with pytest.raises(IntegrityError, match="fk_post_track_id_track"):
        await db_session.execute(text("DELETE FROM track WHERE id = :id"), {"id": track.id})
