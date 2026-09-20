"""Schema-level guarantees the ORM alone cannot enforce: the status CHECKs,
the fragment-bounds CHECK, the `languages` default and CHECK, the partial
unique index, `ON DELETE RESTRICT` on `post.track_id`, and step 4A's media
artifact tables (positive-size CHECKs, idempotency keys, `ON DELETE CASCADE`)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import AudioAsset, Post, Render, SourceFetch, Track
from pisniar.db.states import PostStatus

pytestmark = pytest.mark.integration


async def make_track(session: AsyncSession) -> Track:
    marker = uuid.uuid4().hex
    track = Track(source_url=f"test://{marker}", source_url_normalized=f"test://{marker}")
    session.add(track)
    await session.flush()
    return track


async def test_a_new_track_gets_the_default_language_from_the_schema(
    db_session: AsyncSession,
) -> None:
    """Nothing in the bot writes `languages` (D-61) — the default is the
    only thing that puts a language on a post before step 5 exists."""
    track = await make_track(db_session)

    await db_session.refresh(track)

    assert track.languages == ["українська"]


async def test_languages_check_rejects_a_value_that_is_not_an_array(
    db_session: AsyncSession,
) -> None:
    marker = uuid.uuid4().hex

    with pytest.raises(IntegrityError, match="ck_track_languages_is_array"):
        await db_session.execute(
            text(
                "INSERT INTO track (source_url, source_url_normalized, languages) "
                'VALUES (:url, :url, \'{"name": "українська"}\'::jsonb)'
            ),
            {"url": f"test://{marker}"},
        )


async def test_a_post_keeps_the_submitter_and_the_publisher_apart(
    db_session: AsyncSession,
) -> None:
    """D-62: `created_by_user_id` is who sent the link, the new column is
    who approved and published — with two admins they differ, and until
    publication happens the second one is simply unknown."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=111)
    db_session.add(post)
    await db_session.flush()
    await db_session.refresh(post)

    assert post.published_by_user_id is None

    post.published_by_user_id = 222
    await db_session.flush()

    # Read before `expire_all()`: afterwards `post.id` is a lazy refresh.
    post_id = post.id
    db_session.expire_all()

    reloaded = await db_session.get(Post, post_id)
    assert reloaded is not None
    assert (reloaded.created_by_user_id, reloaded.published_by_user_id) == (111, 222)


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


async def test_partial_unique_index_rejects_duplicate_preview_message(
    db_session: AsyncSession,
) -> None:
    """Migration 0005. Two posts claiming one preview message would mean two
    publish buttons pointing at different rows in the same chat."""
    track = await make_track(db_session)
    db_session.add(
        Post(track_id=track.id, created_by_user_id=1, preview_chat_id=1, preview_message_id=42)
    )
    await db_session.flush()

    db_session.add(
        Post(track_id=track.id, created_by_user_id=1, preview_chat_id=1, preview_message_id=42)
    )
    with pytest.raises(IntegrityError, match="uq_post_preview_message"):
        await db_session.flush()


async def test_preview_ref_check_rejects_a_one_sided_pair(db_session: AsyncSession) -> None:
    """Half a reference is a message the bot can neither edit nor find, and
    `preview_message_id IS NULL` is the delivery idempotency guard."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, preview_chat_id=1)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_preview_ref_complete"):
        await db_session.flush()


async def test_two_posts_may_both_have_no_preview(db_session: AsyncSession) -> None:
    """The index is partial for the same reason the draft one is: every post
    starts with both columns null, and NULLs must not collide."""
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1))
    db_session.add(Post(track_id=track.id, created_by_user_id=1))

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


async def make_post(session: AsyncSession, track: Track) -> Post:
    post = Post(track_id=track.id, created_by_user_id=1)
    session.add(post)
    await session.flush()
    return post


def make_audio_asset(track: Track, **overrides: object) -> AudioAsset:
    fields: dict[str, object] = {
        "track_id": track.id,
        "video_id": "dQw4w9WgXcQ",
        "path": f"/data/audio/{track.id}/dQw4w9WgXcQ.opus",
        "size_bytes": 4_200_000,
        "duration_ms": 238_000,
        "ytdlp_version": "2026.09.01",
    }
    return AudioAsset(**{**fields, **overrides})


def make_render(post: Post, **overrides: object) -> Render:
    fields: dict[str, object] = {
        "post_id": post.id,
        "params_hash": "a" * 16,
        "path": f"/data/render/{post.id}/{'a' * 16}.mp4",
        "size_bytes": 2_260_000,
        "duration_ms": 45_000,
    }
    return Render(**{**fields, **overrides})


async def test_audio_asset_check_rejects_an_empty_file(db_session: AsyncSession) -> None:
    """A zero-byte artifact is the shape a silent yt-dlp failure takes — the
    row for one must not be insertable at all (S4-A7's class)."""
    track = await make_track(db_session)
    db_session.add(make_audio_asset(track, size_bytes=0))

    with pytest.raises(IntegrityError, match="ck_audio_asset_size_positive"):
        await db_session.flush()


async def test_audio_asset_check_rejects_a_zero_length_recording(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(make_audio_asset(track, duration_ms=0))

    with pytest.raises(IntegrityError, match="ck_audio_asset_duration_positive"):
        await db_session.flush()


async def test_audio_asset_is_unique_per_track_and_video(db_session: AsyncSession) -> None:
    """The database half of the download idempotency guard: a redelivered
    task must not be able to register the same file twice."""
    track = await make_track(db_session)
    db_session.add(make_audio_asset(track))
    await db_session.flush()

    db_session.add(make_audio_asset(track, path="/data/audio/other.opus"))
    with pytest.raises(IntegrityError, match="uq_audio_asset_track_id_video_id"):
        await db_session.flush()


async def test_a_second_video_for_the_same_track_is_allowed(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    db_session.add(make_audio_asset(track))
    await db_session.flush()

    db_session.add(
        make_audio_asset(track, video_id="kJQP7kiw5Fk", path="/data/audio/kJQP7kiw5Fk.opus")
    )

    await db_session.flush()  # must not raise


async def test_deleting_a_track_cascades_to_its_audio(db_session: AsyncSession) -> None:
    """CASCADE, not the RESTRICT `post.track_id` carries: an artifact is no
    reason to refuse the delete, and a file without a row is the safe side
    of S4-R16 — the reverse, a row without its file, is not."""
    track = await make_track(db_session)
    db_session.add(make_audio_asset(track))
    await db_session.flush()

    await db_session.execute(text("DELETE FROM track WHERE id = :id"), {"id": track.id})

    remaining = await db_session.execute(
        select(AudioAsset.id).where(AudioAsset.track_id == track.id)
    )
    assert remaining.scalars().all() == []


async def test_render_check_rejects_an_empty_video(db_session: AsyncSession) -> None:
    """ffmpeg exiting 0 on an unusable file is step 4's main silent failure
    (S4-R1); the schema refuses to record its result as a render."""
    track = await make_track(db_session)
    post = await make_post(db_session, track)
    db_session.add(make_render(post, size_bytes=0))

    with pytest.raises(IntegrityError, match="ck_render_size_positive"):
        await db_session.flush()


async def test_render_check_rejects_a_zero_length_video(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = await make_post(db_session, track)
    db_session.add(make_render(post, duration_ms=0))

    with pytest.raises(IntegrityError, match="ck_render_duration_positive"):
        await db_session.flush()


async def test_render_is_unique_per_post_and_params_hash(db_session: AsyncSession) -> None:
    """What lets a redelivered render task find the existing artifact instead
    of running ffmpeg a second time (V36)."""
    track = await make_track(db_session)
    post = await make_post(db_session, track)
    db_session.add(make_render(post))
    await db_session.flush()

    db_session.add(make_render(post, path="/data/render/other.mp4"))
    with pytest.raises(IntegrityError, match="uq_render_post_id_params_hash"):
        await db_session.flush()


async def test_a_render_with_different_params_is_a_different_row(
    db_session: AsyncSession,
) -> None:
    """The 4B seam: a text version changes `params_hash`, so the same post
    gets a second render rather than overwriting the approved one."""
    track = await make_track(db_session)
    post = await make_post(db_session, track)
    db_session.add(make_render(post))
    await db_session.flush()

    db_session.add(
        make_render(post, params_hash="b" * 16, path="/data/render/b.mp4", text_version="lrc:1")
    )

    await db_session.flush()  # must not raise


async def test_deleting_a_post_cascades_to_its_renders(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = await make_post(db_session, track)
    db_session.add(make_render(post))
    await db_session.flush()

    await db_session.execute(text("DELETE FROM post WHERE id = :id"), {"id": post.id})

    remaining = await db_session.execute(select(Render.id).where(Render.post_id == post.id))
    assert remaining.scalars().all() == []


async def test_failed_from_status_check_rejects_a_value_that_is_not_a_status(
    db_session: AsyncSession,
) -> None:
    """The column is TEXT like `status` itself (D-6: no native enum), so the
    CHECK is the only thing keeping a typo out of `/retry`'s routing."""
    track = await make_track(db_session)

    with pytest.raises(IntegrityError, match="ck_post_failed_from_status_valid"):
        await db_session.execute(
            text(
                "INSERT INTO post (track_id, status, failed_from_status, created_by_user_id) "
                "VALUES (:track_id, 'failed', 'downlaoding', 1)"
            ),
            {"track_id": track.id},
        )


async def test_failed_from_status_accepts_null_and_a_real_stage(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1)
    db_session.add(post)
    await db_session.flush()

    assert post.failed_from_status is None

    post.failed_from_status = PostStatus.RENDERING
    await db_session.flush()  # must not raise

    assert post.failed_from_status is PostStatus.RENDERING
