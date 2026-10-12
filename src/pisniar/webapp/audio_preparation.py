"""Starting the audio preparation that comes before the first fragment choice, and giving it up. The database part
locks the post, then the track, and writes `DOWNLOADING` (or `RESOLVED` on a cancel); the queue and the draft's
keyboard come after the commit, never inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from aiogram import Bot
from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.locks import lock_post_without_waiting, lock_track_for_write
from pisniar.db.models import Post
from pisniar.db.states import PostStatus, set_post_status
from pisniar.dispatch import DOWNLOAD_DISPATCH_FAILED_ERROR, dispatch_task, mark_dispatch_failure
from pisniar.logging import get_logger
from pisniar.telegram.caption_edit import MessageRole, keyboard_for_state
from pisniar.telegram.preview import remove_draft_keyboard, restore_draft_keyboard
from pisniar.webapp.applying import TELEGRAM_TIMEOUT_SECONDS
from pisniar.webapp.editing import STALE_DATA_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_view import current_video_id
from pisniar.webapp.schemas import CancelPreparationRequest
from pisniar.webapp.status import cancellation_refusal_of, choosability_of
from pisniar.worker.celery_app import PREPARE_POST_AUDIO_TASK_NAME

logger = get_logger(__name__)

AUDIO_READY_MESSAGE = "Аудіо вже підготовлено."
YOUTUBE_MUSIC_MISSING_MESSAGE = "У треку немає посилання YouTube Music, тому аудіо підготувати не можна."
DISPATCH_FAILED_MESSAGE = "Не вдалося поставити підготовку аудіо в чергу. Спробуйте ще раз за хвилину."


@dataclass(frozen=True)
class AudioPreparation:
    """what the committed request leaves for the steps after it: plain values, no ORM row."""

    post_id: int
    draft: tuple[int, int] | None


def draft_of(post: Post) -> tuple[int, int] | None:
    """the chat and message of the text draft, when the post has one."""
    if post.draft_chat_id is None or post.draft_message_id is None:
        return None
    return post.draft_chat_id, post.draft_message_id


def refuse(
    status_code: int,
    code: str,
    message: str,
    user_id: int,
    post_id: int,
) -> NoReturn:
    logger.warning("audio_preparation_refused", reason=code, user_id=user_id, post_id=post_id)
    raise ApiProblem(status_code, code, message)


async def prepare_audio(
    session: AsyncSession,
    post_id: int,
    user_id: int,
) -> AudioPreparation | None:
    """the caller commits. `None` for a missing post; every refusal leaves the rows untouched."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    verdict = choosability_of(post.status, post.failed_from_status)
    if not verdict.preparable:
        message = AUDIO_READY_MESSAGE if verdict.choosable else verdict.reason
        assert message is not None
        refuse(409, "not_preparable", message, user_id, post_id)

    track = await lock_track_for_write(session, post.track_id)
    if track is None:
        return None

    if current_video_id(track.platform_links) is None:
        refuse(409, "youtube_music_missing", YOUTUBE_MUSIC_MISSING_MESSAGE, user_id, post_id)

    set_post_status(post, PostStatus.DOWNLOADING)

    return AudioPreparation(post_id, draft_of(post))


async def enqueue_audio_preparation(
    celery_client: Celery,
    session_factory: async_sessionmaker[AsyncSession],
    preparation: AudioPreparation,
    user_id: int,
) -> None:
    """a queue that refuses the task fails the post, or it would sit in `downloading` with nothing to move it."""
    sent = await dispatch_task(
        celery_client,
        PREPARE_POST_AUDIO_TASK_NAME,
        {"post_id": preparation.post_id},
        "audio_preparation_dispatch_failed",
        user_id=user_id,
        post_id=preparation.post_id,
    )
    if not sent:
        await mark_dispatch_failure(
            session_factory,
            preparation.post_id,
            expected_status=PostStatus.DOWNLOADING,
            error=DOWNLOAD_DISPATCH_FAILED_ERROR,
        )
        refuse(503, "dispatch_failed", DISPATCH_FAILED_MESSAGE, user_id, preparation.post_id)

    logger.info("audio_preparation_dispatched", user_id=user_id, post_id=preparation.post_id)


async def release_draft_keyboard(
    bot: Bot,
    draft: tuple[int, int] | None,
    post_id: int,
    user_id: int,
) -> None:
    """runs after a successful enqueue: the draft's buttons are dead once the post goes down another path. Cosmetic,
    so nothing here fails the request."""
    if draft is None:
        return

    chat_id, message_id = draft
    try:
        await remove_draft_keyboard(bot, chat_id, message_id, post_id, request_timeout=TELEGRAM_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("draft_keyboard_removal_failed", user_id=user_id, post_id=post_id, exc_info=True)


@dataclass(frozen=True)
class CancelledPreparation:
    """plain values only: the draft whose keyboard comes back once the commit is done."""

    post_id: int
    draft: tuple[int, int] | None


def refuse_cancel(
    status_code: int,
    code: str,
    message: str,
    user_id: int,
    post_id: int,
) -> NoReturn:
    logger.warning("audio_preparation_cancel_refused", reason=code, user_id=user_id, post_id=post_id)
    raise ApiProblem(status_code, code, message)


async def cancel_preparation(
    session: AsyncSession,
    post_id: int,
    request: CancelPreparationRequest,
    user_id: int,
) -> CancelledPreparation | None:
    """the caller commits. `None` for a missing post; the audio stays on the volume, and the track is locked so no
    other writer sees the post half-way."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    refusal = cancellation_refusal_of(post.status)
    if refusal is not None:
        refuse_cancel(409, "not_cancellable", refusal, user_id, post_id)
    if post.updated_at != request.expected_post_updated_at:
        refuse_cancel(409, "stale_data", STALE_DATA_MESSAGE, user_id, post_id)

    if await lock_track_for_write(session, post.track_id) is None:
        return None

    set_post_status(post, PostStatus.RESOLVED)
    return CancelledPreparation(post_id, draft_of(post))


async def restore_draft(
    bot: Bot,
    cancelled: CancelledPreparation,
    webapp_public_url: str | None,
    user_id: int,
) -> None:
    """after the commit: the draft gets back the buttons of a draft. Cosmetic, never raises."""
    if cancelled.draft is None:
        return

    chat_id, message_id = cancelled.draft
    keyboard = keyboard_for_state(PostStatus.RESOLVED, MessageRole.DRAFT, cancelled.post_id, chat_id, webapp_public_url)
    try:
        await restore_draft_keyboard(
            bot,
            chat_id,
            message_id,
            cancelled.post_id,
            keyboard,
            request_timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "draft_keyboard_restore_failed",
            user_id=user_id,
            post_id=cancelled.post_id,
            exc_info=True,
        )
