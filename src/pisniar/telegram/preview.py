"""Video preview delivery — the mirror of `draft.py` for step 4A, and the
single safeguard the step has: publication cannot be revoked (D-65), so the
admin has to see *the file that will go into the channel* before it does
(AS11). `editMessageMedia` cannot turn a text draft into a media message,
so the preview is a new message with its own chat/message reference.
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
    """Idempotent: a `post` with `preview_message_id` already set is a no-op —
    the same Celery-redelivery guard `deliver_draft` uses (D-28/M32). A
    delivery failure propagates: the preview is the product of this step."""
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
    """The text draft must stop offering publication once the preview exists,
    or the cover photo can still go out instead of the video. Cosmetic on its
    own — the status transition is the real guard — so it never raises."""
    if post.draft_chat_id is None or post.draft_message_id is None:
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=post.draft_chat_id, message_id=post.draft_message_id, reply_markup=None
        )
    except TelegramAPIError:
        # Already removed on the button tap; this pass covers the routes
        # (a `/retry`, a redelivery) that never went through one.
        logger.warning("preview_draft_keyboard_removal_failed", post_id=post.id, exc_info=True)
