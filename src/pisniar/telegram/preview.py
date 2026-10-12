"""Video preview delivery, the mirror of `draft.py` and the only safeguard before publication, which cannot be revoked:
the admin sees the exact file first. `editMessageMedia` cannot turn a text draft into media, hence a new message.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

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
    """always sends a new message and points the post at it: the caller's status under the post lock is the guard. A
    failure propagates: the preview is the product."""
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
    """a superseded preview must not offer publication. Cosmetic, so a Telegram refusal is logged with the post and
    never raised."""
    await set_keyboard_quietly(
        bot,
        chat_id,
        message_id,
        post_id,
        None,
        request_timeout,
        failure_event="preview_superseded_keyboard_removal_failed",
        unchanged_event="preview_superseded_keyboard_already_removed",
    )


async def restore_draft_keyboard(
    bot: Bot,
    chat_id: int,
    message_id: int,
    post_id: int,
    reply_markup: InlineKeyboardMarkup | None,
    request_timeout: int | None = None,
) -> None:
    """a draft that went back from a cancelled preparation gets its buttons again. Cosmetic: an unchanged keyboard is a
    success and a Telegram refusal is logged, never raised."""
    await set_keyboard_quietly(
        bot,
        chat_id,
        message_id,
        post_id,
        reply_markup,
        request_timeout,
        failure_event="draft_keyboard_restore_failed",
    )


async def remove_draft_keyboard(
    bot: Bot,
    chat_id: int,
    message_id: int,
    post_id: int,
    request_timeout: int | None = None,
) -> None:
    """otherwise the cover photo could still go out instead of the video. Cosmetic (the status transition is the real
    guard), so it never raises. A keyboard that is already gone is a success."""
    await set_keyboard_quietly(
        bot,
        chat_id,
        message_id,
        post_id,
        None,
        request_timeout,
        failure_event="draft_keyboard_removal_failed",
    )


async def set_keyboard_quietly(
    bot: Bot,
    chat_id: int,
    message_id: int,
    post_id: int,
    reply_markup: InlineKeyboardMarkup | None,
    request_timeout: int | None,
    *,
    failure_event: str,
    unchanged_event: str | None = None,
) -> None:
    """one cosmetic `editMessageReplyMarkup`: «not modified» is a success (logged as `unchanged_event` when given),
    any other Telegram refusal a warning named `failure_event`. Never raises a Telegram error."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            request_timeout=request_timeout,
        )
    except TelegramAPIError as error:
        if classify_edit_error(error) is EditOutcome.NOT_MODIFIED:
            if unchanged_event is not None:
                logger.info(unchanged_event, post_id=post_id, message_id=message_id)
            return
        logger.warning(failure_event, post_id=post_id, chat_id=chat_id, message_id=message_id, exc_info=True)
