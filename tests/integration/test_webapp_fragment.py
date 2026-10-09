"""`fetch_fragment_source` against a real Postgres: which posts can have their fragment chosen,
which recording counts as current, and the refusals that say why.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import AudioAsset, Post, Track
from pisniar.db.states import PostStatus
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_view import FragmentSource, fetch_fragment_source
from pisniar.webapp.status import FRAGMENT_PUBLISHED_REASON, MEDIA_IN_PROGRESS_REASON

pytestmark = pytest.mark.integration

MUSIC_VIDEO_ID = "n4bPzdIs_nc"
OTHER_VIDEO_ID = "dQw4w9WgXcQ"
USER_ID = 111


def music_link(video_id: str) -> dict[str, object]:
    return {
        "url": f"https://music.youtube.com/watch?v={video_id}",
        "source": "ytmusic",
        "match": "confirmed",
    }


async def seed_post(
    session: AsyncSession,
    status: PostStatus,
    *,
    failed_from: PostStatus | None = None,
    audio_video_id: str | None = MUSIC_VIDEO_ID,
    linked_video_id: str | None = MUSIC_VIDEO_ID,
) -> Post:
    marker = uuid.uuid4().hex
    track = Track(
        source_url=f"test://{marker}",
        source_url_normalized=f"test://{marker}",
        title="Тема",
        artists=[{"name": "Артист", "hashtag": None, "spotify_id": None}],
        platform_links={}
        if linked_video_id is None
        else {"youtubeMusic": music_link(linked_video_id)},
    )
    session.add(track)
    await session.flush()

    if audio_video_id is not None:
        session.add(
            AudioAsset(
                track_id=track.id,
                video_id=audio_video_id,
                path=f"/data/audio/{track.id}/{audio_video_id}.webm",
                size_bytes=1000,
                duration_ms=180_000,
                ytdlp_version="2026.01.01",
            )
        )
    post = Post(
        track_id=track.id,
        created_by_user_id=USER_ID,
        status=status,
        failed_from_status=failed_from,
        fragment_start_ms=10_000,
        fragment_end_ms=55_000,
    )
    session.add(post)
    await session.flush()
    return post


async def test_a_rendered_post_gives_the_source_of_its_current_recording(
    db_session: AsyncSession,
) -> None:
    post = await seed_post(db_session, PostStatus.RENDERED)

    source = await fetch_fragment_source(db_session, post.id, USER_ID)

    assert source == FragmentSource(
        post_id=post.id,
        post_updated_at=post.updated_at,
        track_id=post.track_id,
        video_id=MUSIC_VIDEO_ID,
        audio_duration_ms=180_000,
        stored_start_ms=10_000,
        stored_end_ms=55_000,
    )


@pytest.mark.parametrize("stage", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
async def test_a_render_that_failed_gives_a_source(
    db_session: AsyncSession, stage: PostStatus
) -> None:
    post = await seed_post(db_session, PostStatus.FAILED, failed_from=stage)

    assert await fetch_fragment_source(db_session, post.id, USER_ID) is not None


async def test_a_missing_post_is_none(db_session: AsyncSession) -> None:
    assert await fetch_fragment_source(db_session, 9_000_000_000, USER_ID) is None


@pytest.mark.parametrize(
    ("status", "failed_from", "sentence"),
    [
        (PostStatus.PUBLISHED, None, FRAGMENT_PUBLISHED_REASON),
        (PostStatus.RENDERING, None, MEDIA_IN_PROGRESS_REASON),
        (PostStatus.DOWNLOADING, None, MEDIA_IN_PROGRESS_REASON),
        (PostStatus.RESOLVED, None, None),
        (PostStatus.FAILED, PostStatus.PUBLISHING, None),
    ],
)
async def test_a_post_that_cannot_be_chosen_is_409_with_a_sentence(
    db_session: AsyncSession,
    status: PostStatus,
    failed_from: PostStatus | None,
    sentence: str | None,
) -> None:
    post = await seed_post(db_session, status, failed_from=failed_from)

    with pytest.raises(ApiProblem) as refusal:
        await fetch_fragment_source(db_session, post.id, USER_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "not_choosable")
    assert sentence is None or refusal.value.message == sentence
    assert status.value not in refusal.value.message.lower()


async def test_audio_of_another_recording_is_audio_missing(db_session: AsyncSession) -> None:
    post = await seed_post(db_session, PostStatus.RENDERED, audio_video_id=OTHER_VIDEO_ID)

    with pytest.raises(ApiProblem) as refusal:
        await fetch_fragment_source(db_session, post.id, USER_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "audio_missing")


@pytest.mark.parametrize(
    ("audio_video_id", "linked_video_id"),
    [(None, MUSIC_VIDEO_ID), (MUSIC_VIDEO_ID, None)],
)
async def test_no_audio_row_or_no_youtube_music_link_is_audio_missing(
    db_session: AsyncSession, audio_video_id: str | None, linked_video_id: str | None
) -> None:
    post = await seed_post(
        db_session,
        PostStatus.RENDERED,
        audio_video_id=audio_video_id,
        linked_video_id=linked_video_id,
    )

    with pytest.raises(ApiProblem) as refusal:
        await fetch_fragment_source(db_session, post.id, USER_ID)

    assert refusal.value.code == "audio_missing"
