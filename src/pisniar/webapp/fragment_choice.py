"""Starting a render with the bounds the admin chose. The database part locks the post, then the
track, and writes the bounds and the status; the queue and then the old preview's keyboard come
after the commit, never inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from aiogram import Bot
from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.locks import lock_post_without_waiting, lock_track_for_write
from pisniar.db.states import PostStatus, set_post_status
from pisniar.dispatch import RENDER_DISPATCH_FAILED_ERROR, dispatch_task, mark_dispatch_failure
from pisniar.logging import get_logger
from pisniar.telegram.preview import remove_preview_keyboard
from pisniar.webapp.applying import TELEGRAM_TIMEOUT_SECONDS
from pisniar.webapp.editing import STALE_DATA_MESSAGE, assign_if_changed
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_view import AUDIO_MISSING_MESSAGE, current_audio_asset
from pisniar.webapp.schemas import FragmentRequest
from pisniar.webapp.status import choosability_of
from pisniar.worker.celery_app import RENDER_POST_VIDEO_TASK_NAME
from pisniar.worker.fragment import FragmentBounds, FragmentBoundsError, validate_chosen_bounds

logger = get_logger(__name__)

INVALID_FRAGMENT_MESSAGE = "Фрагмент не підходить: {reason}."
FRAGMENT_UNCHANGED_MESSAGE = (
    "Відео вже відрендерено саме з цим фрагментом. Оберіть інші межі, щоб зробити нове."
)
DISPATCH_FAILED_MESSAGE = (
    "Не вдалося поставити відео в чергу. Повторіть спробу командою /retry {post_id} у чаті з ботом."
)


@dataclass(frozen=True)
class FragmentChoice:
    """what the committed choice leaves for the steps after it: plain values, no ORM row."""

    post_id: int
    bounds: FragmentBounds
    superseded_preview: tuple[int, int] | None


def refuse(status_code: int, code: str, message: str, user_id: int, post_id: int) -> NoReturn:
    logger.warning("fragment_render_refused", reason=code, user_id=user_id, post_id=post_id)
    raise ApiProblem(status_code, code, message)


async def choose_fragment(
    session: AsyncSession, post_id: int, request: FragmentRequest, user_id: int
) -> FragmentChoice | None:
    """the caller commits. `None` for a missing post; every refusal leaves the rows untouched."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    verdict = choosability_of(post.status, post.failed_from_status)
    if not verdict.choosable:
        assert verdict.reason is not None
        refuse(409, "not_choosable", verdict.reason, user_id, post_id)
    if post.updated_at != request.expected_post_updated_at:
        refuse(409, "stale_data", STALE_DATA_MESSAGE, user_id, post_id)

    track = await lock_track_for_write(session, post.track_id)
    if track is None:
        return None

    asset = await current_audio_asset(session, track)
    if asset is None:
        refuse(409, "audio_missing", AUDIO_MISSING_MESSAGE, user_id, post_id)

    try:
        bounds = validate_chosen_bounds(request.start_ms, request.end_ms, asset.duration_ms)
    except FragmentBoundsError as error:
        refuse(
            422, "invalid_fragment", INVALID_FRAGMENT_MESSAGE.format(reason=error), user_id, post_id
        )

    stored = (post.fragment_start_ms, post.fragment_end_ms)
    if post.status is PostStatus.RENDERED and stored == (bounds.start_ms, bounds.end_ms):
        refuse(409, "fragment_unchanged", FRAGMENT_UNCHANGED_MESSAGE, user_id, post_id)

    superseded_preview = (
        None
        if post.preview_chat_id is None or post.preview_message_id is None
        else (post.preview_chat_id, post.preview_message_id)
    )
    assign_if_changed(post, "fragment_start_ms", bounds.start_ms)
    assign_if_changed(post, "fragment_end_ms", bounds.end_ms)
    set_post_status(post, PostStatus.RENDERING)

    return FragmentChoice(post_id, bounds, superseded_preview)


async def release_superseded_preview(bot: Bot, choice: FragmentChoice, user_id: int) -> None:
    """runs after a successful enqueue, so the old preview stays usable if the render never starts.
    Cosmetic: a Telegram refusal is logged by `remove_preview_keyboard`, anything else here."""
    if choice.superseded_preview is None:
        return

    chat_id, message_id = choice.superseded_preview
    try:
        await remove_preview_keyboard(
            bot, chat_id, message_id, choice.post_id, request_timeout=TELEGRAM_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning(
            "fragment_preview_keyboard_removal_failed",
            user_id=user_id,
            post_id=choice.post_id,
            exc_info=True,
        )


async def enqueue_render(
    celery_client: Celery,
    session_factory: async_sessionmaker[AsyncSession],
    choice: FragmentChoice,
    user_id: int,
) -> None:
    """a queue that refuses the task fails the post, or it would sit in `rendering` with nothing
    to move it."""
    sent = await dispatch_task(
        celery_client,
        RENDER_POST_VIDEO_TASK_NAME,
        {"post_id": choice.post_id},
        "fragment_render_dispatch_failed",
        user_id=user_id,
        post_id=choice.post_id,
    )
    if not sent:
        await mark_dispatch_failure(
            session_factory,
            choice.post_id,
            expected_status=PostStatus.RENDERING,
            error=RENDER_DISPATCH_FAILED_ERROR,
        )
        refuse(
            503,
            "dispatch_failed",
            DISPATCH_FAILED_MESSAGE.format(post_id=choice.post_id),
            user_id,
            choice.post_id,
        )

    logger.info(
        "fragment_render_dispatched",
        user_id=user_id,
        post_id=choice.post_id,
        start_ms=choice.bounds.start_ms,
        end_ms=choice.bounds.end_ms,
    )
