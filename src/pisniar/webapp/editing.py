"""Previewing and saving an edit of a post's fields.

The preview only reads. The save locks the post `FOR UPDATE NOWAIT`, then the track, checks both
versions and writes; Telegram is never called here.
"""

from __future__ import annotations

from typing import NoReturn

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.locks import lock_post_without_waiting, lock_track_for_write
from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.logging import get_logger
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fields import PlannedFields, StoredFields, plan_fields
from pisniar.webapp.schemas import FieldsInput, FieldsUpdate, PostDetail, PreviewResponse
from pisniar.webapp.service import caption_sha256, caption_view_of, detail_of
from pisniar.webapp.status import editability_of

logger = get_logger(__name__)

MEDIA_STATUSES = (
    PostStatus.DOWNLOADING,
    PostStatus.DOWNLOADED,
    PostStatus.RENDERING,
    PostStatus.EDITING,
)
STALE_DATA_MESSAGE = "Дані змінились відтоді, як ви їх відкрили. Перегляньте їх ще раз."
MEDIA_IN_PROGRESS_MESSAGE = (
    "Посилання YouTube Music не можна змінити, поки для цього треку готується відео. "
    "Дочекайтесь прев'ю або помилки."
)
POST_NOT_FOUND_MESSAGE = "Пост не знайдено."


def stored_fields_of(post: Post, track: Track) -> StoredFields:
    return StoredFields(
        title=track.title,
        artists=track.artists,
        languages=track.languages,
        genres=post.genres,
        year=track.year,
        release_type=track.release_type,
        release_title=track.release_title,
        album_id_spotify=track.album_id_spotify,
        platform_links=track.platform_links,
        manual_fields=track.manual_fields,
        removed_platforms=track.removed_platforms,
    )


def assign_if_changed(row: Post | Track, name: str, value: object) -> bool:
    """an equal value is not assigned at all, so a save without changes writes nothing and
    leaves `updated_at` alone."""
    if getattr(row, name) == value:
        return False
    setattr(row, name, value)
    return True


def apply_plan(post: Post, track: Track, planned: PlannedFields) -> None:
    assign_if_changed(track, "title", planned.title)
    assign_if_changed(track, "artists", planned.artists)
    assign_if_changed(track, "languages", planned.languages)
    assign_if_changed(track, "year", planned.year)
    assign_if_changed(track, "release_type", planned.release_type)
    assign_if_changed(track, "release_title", planned.release_title)
    assign_if_changed(track, "album_id_spotify", planned.album_id_spotify)
    assign_if_changed(track, "platform_links", planned.platform_links)
    assign_if_changed(track, "manual_fields", planned.manual_fields)
    assign_if_changed(track, "removed_platforms", planned.removed_platforms)
    assign_if_changed(post, "genres", planned.genres)


def refuse_unless_editable(post: Post, user_id: int, refusal_event: str) -> None:
    verdict = editability_of(post.status, post.failed_from_status)
    if not verdict.editable:
        logger.warning(refusal_event, reason="not_editable", user_id=user_id, post_id=post.id)
        raise ApiProblem(409, "not_editable", verdict.reason or "Пост зараз не можна редагувати.")


def preview_of_plan(planned: PlannedFields, post: Post) -> PreviewResponse:
    view, _ = caption_view_of(
        title=planned.title,
        artists=planned.artists,
        year=planned.year,
        release_type=planned.release_type,
        release_title=planned.release_title,
        album_id_spotify=planned.album_id_spotify,
        languages=planned.languages,
        genres=planned.genres,
        platform_links=planned.platform_links,
    )
    stored_sha = caption_sha256(post.caption) if post.caption is not None else None
    return PreviewResponse(
        caption=view,
        stored_caption_sha256=stored_sha,
        differs_from_stored=stored_sha != view.sha256,
    )


async def preview_fields(
    session: AsyncSession, post_id: int, update: FieldsInput, current_year: int, user_id: int
) -> PreviewResponse | None:
    """what the caption would be with these fields, without writing anything."""
    row = (
        await session.execute(
            select(Post, Track).join(Track, Track.id == Post.track_id).where(Post.id == post_id)
        )
    ).one_or_none()
    if row is None:
        return None
    post, track = row

    refuse_unless_editable(post, user_id, "fields_preview_refused")
    planned = plan_fields(update, stored_fields_of(post, track), current_year)
    preview = preview_of_plan(planned, post)

    logger.info(
        "fields_previewed",
        user_id=user_id,
        post_id=post_id,
        changed_fields=list(planned.changed_fields),
        visible_length=preview.caption.visible_length,
    )
    return preview


async def count_media_posts(session: AsyncSession, track_id: int) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Post)
        .where(Post.track_id == track_id, Post.status.in_(MEDIA_STATUSES))
    )
    return count or 0


async def save_fields(
    session: AsyncSession, post_id: int, update: FieldsUpdate, current_year: int, user_id: int
) -> PostDetail | None:
    """the caller commits. The post is locked first, then the track, and nothing is written
    unless both rows still carry the `updated_at` the editor was opened with."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    refuse_unless_editable(post, user_id, "fields_save_refused")
    if post.updated_at != update.expected_post_updated_at:
        refuse_stale(user_id, post_id)

    track = await lock_track_for_write(session, post.track_id)
    if track is None:
        return None
    if track.updated_at != update.expected_track_updated_at:
        refuse_stale(user_id, post_id)

    planned = plan_fields(update, stored_fields_of(post, track), current_year)
    if planned.youtube_music_changed and await count_media_posts(session, track.id) > 0:
        logger.warning(
            "fields_save_refused", reason="media_in_progress", user_id=user_id, post_id=post_id
        )
        raise ApiProblem(409, "media_in_progress", MEDIA_IN_PROGRESS_MESSAGE)

    apply_plan(post, track, planned)
    await session.flush()
    await session.refresh(post)
    await session.refresh(track)

    logger.info(
        "fields_saved",
        user_id=user_id,
        post_id=post_id,
        changed_fields=list(planned.changed_fields),
        manual_fields=planned.manual_fields,
    )
    return await detail_of(session, post, track)


def refuse_stale(user_id: int, post_id: int) -> NoReturn:
    logger.warning("fields_save_refused", reason="stale_data", user_id=user_id, post_id=post_id)
    raise ApiProblem(409, "stale_data", STALE_DATA_MESSAGE)
