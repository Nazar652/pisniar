"""Schema-level guarantees the ORM alone cannot enforce: the status CHECKs, the fragment-bounds CHECK, the `languages`
default and CHECK, the partial unique index, `ON DELETE RESTRICT` on `post.track_id`, the media artifact tables
(positive-size CHECKs, idempotency keys, `ON DELETE CASCADE`) and the lyric lines."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import (
    CHANNEL_MESSAGE_KINDS,
    MANUAL_FIELD_NAMES,
    AudioAsset,
    Lyric,
    Post,
    Render,
    SourceFetch,
    Track,
    renderable_lyrics_statement,
)
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
    """nothing in the bot writes `languages`, so the schema default is what puts a language on every post."""
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


async def test_a_new_track_has_no_manual_fields_and_no_removed_platforms(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)

    await db_session.refresh(track)

    assert track.manual_fields == []
    assert track.removed_platforms == []


async def test_manual_fields_accepts_every_listed_name(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    track.manual_fields = list(MANUAL_FIELD_NAMES)

    await db_session.flush()  # must not raise


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("manual_fields", '{"title": true}', "ck_track_manual_fields_is_array"),
        ("manual_fields", '["title", "isrc"]', "ck_track_manual_fields_known_names"),
        ("manual_fields", '["cover_url"]', "ck_track_manual_fields_known_names"),
        ("removed_platforms", '"spotify"', "ck_track_removed_platforms_is_array"),
    ],
)
async def test_manual_field_columns_reject_a_wrong_shape_or_an_unknown_name(
    db_session: AsyncSession,
    column: str,
    value: str,
    constraint: str,
) -> None:
    marker = uuid.uuid4().hex

    with pytest.raises(IntegrityError, match=constraint):
        await db_session.execute(
            text(
                f"INSERT INTO track (source_url, source_url_normalized, {column}) "
                f"VALUES (:url, :url, CAST(:value AS jsonb))"
            ),
            {"url": f"test://{marker}", "value": value},
        )


async def test_channel_message_kind_accepts_each_listed_kind_with_a_message(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    for message_id, kind in enumerate(CHANNEL_MESSAGE_KINDS, start=1):
        db_session.add(
            Post(
                track_id=track.id,
                created_by_user_id=1,
                channel_chat_id=-100,
                channel_message_id=message_id,
                channel_message_kind=kind,
            )
        )

    await db_session.flush()  # must not raise


async def test_channel_message_kind_rejects_an_unknown_kind(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    db_session.add(
        Post(
            track_id=track.id,
            created_by_user_id=1,
            channel_chat_id=-100,
            channel_message_id=1,
            channel_message_kind="audio",
        )
    )

    with pytest.raises(IntegrityError, match="ck_post_channel_message_kind_valid"):
        await db_session.flush()


async def test_channel_message_kind_without_a_message_is_rejected(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1, channel_message_kind="photo"))

    with pytest.raises(IntegrityError, match="ck_post_channel_message_kind_matches_ref"):
        await db_session.flush()


async def test_a_channel_message_without_a_kind_is_rejected(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    db_session.add(
        Post(
            track_id=track.id,
            created_by_user_id=1,
            channel_chat_id=-100,
            channel_message_id=1,
        )
    )

    with pytest.raises(IntegrityError, match="ck_post_channel_message_kind_matches_ref"):
        await db_session.flush()


async def test_a_post_keeps_the_submitter_and_the_publisher_apart(
    db_session: AsyncSession,
) -> None:
    """with two admins the submitter and the publisher differ, and the publisher stays unknown until publication."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=111)
    db_session.add(post)
    await db_session.flush()
    await db_session.refresh(post)

    assert post.published_by_user_id is None

    post.published_by_user_id = 222
    await db_session.flush()

    # read before `expire_all()`: afterwards `post.id` is a lazy refresh.
    post_id = post.id
    db_session.expire_all()

    reloaded = await db_session.get(Post, post_id)
    assert reloaded is not None
    assert (reloaded.created_by_user_id, reloaded.published_by_user_id) == (111, 222)


async def test_status_check_constraint_rejects_unknown_status(db_session: AsyncSession) -> None:
    track = await make_track(db_session)

    with pytest.raises(IntegrityError, match="ck_post_status_valid"):
        await db_session.execute(
            text("INSERT INTO post (track_id, status, created_by_user_id) VALUES (:track_id, 'nonsense', 1)"),
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


@pytest.mark.parametrize("description", ["", "я" * 501, "\U0001f600" * 501])
async def test_description_check_rejects_an_empty_and_an_over_long_text(
    db_session: AsyncSession, description: str
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1, description=description))

    with pytest.raises(IntegrityError, match="ck_post_description_length"):
        await db_session.flush()


@pytest.mark.parametrize("description", [None, "я", "я" * 500, "\U0001f600" * 500])
async def test_description_check_allows_null_and_the_limits(db_session: AsyncSession, description: str | None) -> None:
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, description=description)
    db_session.add(post)

    await db_session.flush()  # must not raise

    assert post.description == description


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
        Post(
            track_id=track.id,
            created_by_user_id=1,
            channel_chat_id=1,
            channel_message_id=42,
            channel_message_kind="photo",
        )
    )
    await db_session.flush()

    db_session.add(
        Post(
            track_id=track.id,
            created_by_user_id=1,
            channel_chat_id=1,
            channel_message_id=42,
            channel_message_kind="photo",
        )
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
    """the partial unique index fires only once both columns are non-NULL, so
    without this CHECK half a channel reference would pass silently."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, channel_chat_id=1)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_channel_ref_complete"):
        await db_session.flush()


async def test_partial_unique_index_rejects_duplicate_draft_message(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1, draft_chat_id=1, draft_message_id=42))
    await db_session.flush()

    db_session.add(Post(track_id=track.id, created_by_user_id=1, draft_chat_id=1, draft_message_id=42))
    with pytest.raises(IntegrityError, match="uq_post_draft_message"):
        await db_session.flush()


async def test_draft_ref_check_rejects_a_one_sided_pair(db_session: AsyncSession) -> None:
    """same one-sided-pair hazard as `channel_ref_complete`, on the draft columns that guard redelivery."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, draft_chat_id=1)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_draft_ref_complete"):
        await db_session.flush()


async def test_partial_unique_index_rejects_duplicate_preview_message(
    db_session: AsyncSession,
) -> None:
    """two posts claiming one preview message would put two publish buttons for different rows into one chat."""
    track = await make_track(db_session)
    previewed = {
        "track_id": track.id,
        "created_by_user_id": 1,
        "preview_chat_id": 1,
        "preview_message_id": 42,
        "preview_params_hash": "a" * 64,
    }
    db_session.add(Post(**previewed))
    await db_session.flush()

    db_session.add(Post(**previewed))
    with pytest.raises(IntegrityError, match="uq_post_preview_message"):
        await db_session.flush()


async def test_a_preview_message_without_its_params_hash_is_rejected(
    db_session: AsyncSession,
) -> None:
    """publication takes the render the hash points at; a preview with none has no file."""
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1, preview_chat_id=1, preview_message_id=42))

    with pytest.raises(IntegrityError, match="ck_post_preview_params_hash_matches_ref"):
        await db_session.flush()


async def test_a_params_hash_without_a_preview_message_is_rejected(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1, preview_params_hash="a" * 64))

    with pytest.raises(IntegrityError, match="ck_post_preview_params_hash_matches_ref"):
        await db_session.flush()


async def test_preview_ref_check_rejects_a_one_sided_pair(db_session: AsyncSession) -> None:
    """half a reference is a message the bot can neither edit nor find, and
    `preview_message_id IS NULL` is the delivery idempotency guard."""
    track = await make_track(db_session)
    post = Post(track_id=track.id, created_by_user_id=1, preview_chat_id=1)

    db_session.add(post)
    with pytest.raises(IntegrityError, match="ck_post_preview_ref_complete"):
        await db_session.flush()


async def test_two_posts_may_both_have_no_preview(db_session: AsyncSession) -> None:
    """the index is partial for the same reason the draft one is: every post
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

    db_session.add(SourceFetch(source="deezer", request_key="isrc:ABC123", payload={"found": False}))
    with pytest.raises(IntegrityError, match="uq_source_fetch_source_request_key"):
        await db_session.flush()


async def test_track_deletion_is_restricted_while_a_post_references_it(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Post(track_id=track.id, created_by_user_id=1))
    await db_session.flush()

    # raw SQL, not `session.delete(track)`: asserts the database constraint
    # directly, independent of the ORM's `passive_deletes` configuration.
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
    """a zero-byte artifact is the shape a silent yt-dlp failure takes, so a row
    for one must not be insertable at all."""
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
    """the database half of the download idempotency guard: a redelivered
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

    db_session.add(make_audio_asset(track, video_id="kJQP7kiw5Fk", path="/data/audio/kJQP7kiw5Fk.opus"))

    await db_session.flush()  # must not raise


async def test_deleting_a_track_cascades_to_its_audio(db_session: AsyncSession) -> None:
    """CASCADE, not `post.track_id`'s RESTRICT: a file without a row is harmless,
    while a row without its file is not."""
    track = await make_track(db_session)
    db_session.add(make_audio_asset(track))
    await db_session.flush()

    await db_session.execute(text("DELETE FROM track WHERE id = :id"), {"id": track.id})

    remaining = await db_session.execute(select(AudioAsset.id).where(AudioAsset.track_id == track.id))
    assert remaining.scalars().all() == []


async def test_a_lyric_line_may_lack_a_timecode_but_never_have_a_negative_one(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    db_session.add(Lyric(track_id=track.id, line_index=0, start_ms=None, text="без часу", source="x"))
    db_session.add(Lyric(track_id=track.id, line_index=1, start_ms=0, text="з часом", source="x"))
    await db_session.flush()  # must not raise

    db_session.add(Lyric(track_id=track.id, line_index=2, start_ms=-1, text="до нуля", source="x"))
    with pytest.raises(IntegrityError, match="ck_lyric_start_ms_non_negative"):
        await db_session.flush()


async def test_the_render_read_never_returns_a_line_without_a_timecode(
    db_session: AsyncSession,
) -> None:
    track = await make_track(db_session)
    for line_index, start_ms in enumerate((None, 1_000, None, 5_000)):
        db_session.add(
            Lyric(
                track_id=track.id,
                line_index=line_index,
                start_ms=start_ms,
                text=f"рядок {line_index}",
                source="manual",
            )
        )
    await db_session.flush()

    rows = (await db_session.scalars(renderable_lyrics_statement(track.id))).all()

    assert [(row.line_index, row.start_ms) for row in rows] == [(1, 1_000), (3, 5_000)]


async def test_a_new_track_has_no_lyrics_marker(db_session: AsyncSession) -> None:
    track = await make_track(db_session)
    await db_session.refresh(track)

    assert track.lyrics_edited_at is None


async def test_render_check_rejects_an_empty_video(db_session: AsyncSession) -> None:
    """ffmpeg can exit 0 on an unusable file; the schema refuses to record such a result as a render."""
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
    """what lets a redelivered render task find the existing artifact instead of running ffmpeg a second time."""
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
    """a text version changes `params_hash`, so the same post gets a second render
    instead of overwriting the approved one."""
    track = await make_track(db_session)
    post = await make_post(db_session, track)
    db_session.add(make_render(post))
    await db_session.flush()

    db_session.add(make_render(post, params_hash="b" * 16, path="/data/render/b.mp4", text_version="lrc:1"))

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
    """the column is TEXT like `status` itself (no native enum), so the CHECK is
    the only thing keeping a typo out of `/retry`'s routing."""
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
