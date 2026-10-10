"""Reading the fragment editor's data: the bounds of the current video, the waveform and a signed
audio URL. The database part only reads; the files and the token are handled after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import AudioAsset, Post, Track
from pisniar.logging import get_logger
from pisniar.resolve.urls import stored_link_url, track_video_id_of
from pisniar.webapp.auth import MediaClaims, WebappContext, issue_media_token
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fields import YOUTUBE_MUSIC
from pisniar.webapp.schemas import FragmentPeaks, FragmentView
from pisniar.webapp.status import choosability_of
from pisniar.worker.fragment import (
    MAX_FRAGMENT_LENGTH_MS,
    FragmentBounds,
    FragmentBoundsError,
    bounds_within_track,
    choose_fragment_bounds,
    stored_bounds,
)
from pisniar.worker.peaks_format import PEAKS_PER_SECOND, read_peaks
from pisniar.worker.storage import find_existing_audio, peaks_file_path

logger = get_logger(__name__)

AUDIO_MISSING_MESSAGE = (
    "Аудіо для поточного посилання YouTube Music ще не завантажено, тому фрагмент обрати не можна."
)


@dataclass(frozen=True)
class FragmentSource:
    """what the database says about a post whose fragment can be chosen."""

    post_id: int
    post_updated_at: datetime
    track_id: int
    video_id: str
    audio_duration_ms: int
    stored_start_ms: int | None
    stored_end_ms: int | None


def current_video_id(platform_links: dict[str, object]) -> str | None:
    """the `videoId` of the track's `youtubeMusic` link, the only recording that is ever cut."""
    url = stored_link_url(platform_links, YOUTUBE_MUSIC)
    return None if url is None else track_video_id_of(url)


async def current_audio_asset(session: AsyncSession, track: Track) -> AudioAsset | None:
    """the downloaded audio of the track's current `youtubeMusic` recording, if there is one."""
    video_id = current_video_id(track.platform_links)
    if video_id is None:
        return None
    return await session.scalar(
        select(AudioAsset).where(AudioAsset.track_id == track.id, AudioAsset.video_id == video_id)
    )


async def fetch_fragment_source(
    session: AsyncSession, post_id: int, user_id: int
) -> FragmentSource | None:
    """`None` for a missing post; a 409 when the status or the audio rules the choice out."""
    row = (
        await session.execute(
            select(Post, Track).join(Track, Track.id == Post.track_id).where(Post.id == post_id)
        )
    ).one_or_none()
    if row is None:
        return None
    post, track = row.tuple()

    verdict = choosability_of(post.status, post.failed_from_status)
    if not verdict.choosable:
        logger.warning(
            "fragment_view_refused", reason="not_choosable", user_id=user_id, post_id=post_id
        )
        assert verdict.reason is not None
        raise ApiProblem(409, "not_choosable", verdict.reason)

    asset = await current_audio_asset(session, track)
    if asset is None:
        refuse_audio_missing(user_id, post_id)

    return FragmentSource(
        post_id=post.id,
        post_updated_at=post.updated_at,
        track_id=track.id,
        video_id=asset.video_id,
        audio_duration_ms=asset.duration_ms,
        stored_start_ms=post.fragment_start_ms,
        stored_end_ms=post.fragment_end_ms,
    )


def refuse_audio_missing(user_id: int, post_id: int) -> NoReturn:
    logger.warning(
        "fragment_view_refused", reason="audio_missing", user_id=user_id, post_id=post_id
    )
    raise ApiProblem(409, "audio_missing", AUDIO_MISSING_MESSAGE)


def displayed_bounds(source: FragmentSource) -> FragmentBounds:
    """the bounds a render would use: the stored ones while they fit the file, else the default."""
    try:
        stored = stored_bounds(source.stored_start_ms, source.stored_end_ms)
    except FragmentBoundsError:
        stored = None

    if stored is not None and bounds_within_track(stored, source.audio_duration_ms):
        return stored
    return choose_fragment_bounds(source.audio_duration_ms)


def peaks_of(context: WebappContext, source: FragmentSource) -> FragmentPeaks | None:
    values = read_peaks(peaks_file_path(context.media_root, source.track_id, source.video_id))
    if values is None:
        logger.warning(
            "fragment_peaks_unavailable", post_id=source.post_id, track_id=source.track_id
        )
        return None
    return FragmentPeaks(peaks_per_second=PEAKS_PER_SECOND, values=values)


def build_fragment_view(
    context: WebappContext, source: FragmentSource, user_id: int
) -> FragmentView:
    """the audio file must still be on the volume; the URL carries a token and is never logged."""
    if find_existing_audio(context.media_root, source.track_id, source.video_id) is None:
        refuse_audio_missing(user_id, source.post_id)

    bounds = displayed_bounds(source)
    peaks = peaks_of(context, source)
    issued_at = int(context.clock())
    token = issue_media_token(
        MediaClaims(
            user_id=user_id,
            post_id=source.post_id,
            track_id=source.track_id,
            video_id=source.video_id,
        ),
        context.bot_token,
        issued_at,
        context.media_url_ttl_seconds,
    )

    logger.info(
        "fragment_view_read",
        user_id=user_id,
        post_id=source.post_id,
        start_ms=bounds.start_ms,
        end_ms=bounds.end_ms,
        has_peaks=peaks is not None,
    )
    return FragmentView(
        post_updated_at=source.post_updated_at,
        start_ms=bounds.start_ms,
        end_ms=bounds.end_ms,
        audio_duration_ms=source.audio_duration_ms,
        max_length_ms=MAX_FRAGMENT_LENGTH_MS,
        peaks=peaks,
        audio_url=f"/api/posts/{source.post_id}/audio?token={token}",
        audio_url_expires_at=datetime.fromtimestamp(issued_at + context.media_url_ttl_seconds, UTC),
    )
