"""Video preview delivery, the mirror of `draft.py` and the only safeguard before
publication, which cannot be revoked: the admin sees the exact file first.
`editMessageMedia` cannot turn a text draft into media, hence a new message.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from pisniar.db.models import Post
from pisniar.logging import get_logger
from pisniar.telegram.keyboards import preview_keyboard
from pisniar.telegram.video import RenderedVideo, send_rendered_video

logger = get_logger(__name__)


async def deliver_preview(bot: Bot, post: Post, chat_id: int, video: RenderedVideo) -> None:
    """idempotent on `preview_message_id`, like `deliver_draft`. A failure
    propagates: the preview is the product."""
    if post.preview_message_id is not None:
        return
    if post.caption is None:
        raise ValueError(f"post {post.id} has no caption to preview")

    sent_message = await send_rendered_video(
        bot, chat_id, post.caption, video, reply_markup=preview_keyboard(post.id)
    )
    post.preview_chat_id = chat_id
    post.preview_message_id = sent_message.message_id


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
