"""Draft delivery, not `send_alert()`: a draft is the product, so a failure
propagates (-> `failed` + `/retry`). `parse_mode="HTML"` is per-call only; the
shared `Bot` stays `parse_mode=None`. Carries both triggers, publish and render.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from pisniar.db.models import Post
from pisniar.telegram.keyboards import draft_keyboard


async def deliver_draft(bot: Bot, post: Post, chat_id: int) -> None:
    """idempotent: a `post` with `draft_message_id` already set is a no-op —
    this is what makes Celery's at-least-once redelivery safe."""
    if post.draft_message_id is not None:
        return
    if post.caption is None:
        raise ValueError(f"post {post.id} has no caption to deliver")

    # a caption is full of platform links; without this Telegram appends a
    # preview card and the draft no longer looks like the future post.
    sent_message = await bot.send_message(
        chat_id=chat_id,
        text=post.caption,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=draft_keyboard(post.id),
    )
    post.draft_chat_id = chat_id
    post.draft_message_id = sent_message.message_id
