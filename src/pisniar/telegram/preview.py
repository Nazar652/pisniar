"""Video preview delivery, the mirror of `draft.py` and the only safeguard before
publication, which cannot be revoked: the admin sees the exact file first.
`editMessageMedia` cannot turn a text draft into media, hence a new message.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from pisniar.db.models import Post
from pisniar.logging import get_logger
from pisniar.telegram.caption_edit import EditOutcome, classify_edit_error
from pisniar.telegram.keyboards import preview_keyboard
from pisniar.telegram.video import RenderedVideo, send_rendered_video

logger = get_logger(__name__)


async def deliver_preview(
    bot: Bot,
    post: Post,
    chat_id: int,
    video: RenderedVideo,
    webapp_public_url: str | None = None,
) -> None:
    """always sends a new message and points the post at it: the caller's status under the
    post lock is the guard. A failure propagates: the preview is the product."""
    if post.caption is None:
        raise ValueError(f"post {post.id} has no caption to preview")

    sent_message = await send_rendered_video(
        bot,
        chat_id,
        post.caption,
        video,
        reply_markup=preview_keyboard(post.id, chat_id, webapp_public_url),
    )
    post.preview_chat_id = chat_id
    post.preview_message_id = sent_message.message_id


async def remove_preview_keyboard(
    bot: Bot,
    chat_id: int,
    message_id: int,
    post_id: int,
    request_timeout: int | None = None,
) -> None:
    """a superseded preview must not offer publication. Cosmetic, so a Telegram refusal is logged
    with the post and never raised."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
            request_timeout=request_timeout,
        )
    except TelegramAPIError as error:
        if classify_edit_error(error) is EditOutcome.NOT_MODIFIED:
            logger.info(
                "preview_superseded_keyboard_already_removed",
                post_id=post_id,
                message_id=message_id,
            )
            return
        logger.warning(
            "preview_superseded_keyboard_removal_failed",
            post_id=post_id,
            chat_id=chat_id,
            message_id=message_id,
            exc_info=True,
        )


async def remove_draft_keyboard(bot: Bot, post: Post) -> None:
    """otherwise the cover photo could still go out instead of the video.
    Cosmetic (the status transition is the real guard), so it never raises."""
    if post.draft_chat_id is None or post.draft_message_id is None:
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=post.draft_chat_id, message_id=post.draft_message_id, reply_markup=None
        )
    except TelegramAPIError:
        # already removed on the button tap; this pass covers the routes
        # (a `/retry`, a redelivery) that never went through one.
        logger.warning("preview_draft_keyboard_removal_failed", post_id=post.id, exc_info=True)
