"""Draft delivery — NOT `send_alert()` (contract p.10): a draft is the
product, so a delivery failure must propagate (-> `failed` + `/retry`),
never be swallowed. `parse_mode="HTML"` is passed per-call here only —
the shared `Bot` stays `parse_mode=None` everywhere else (D-2 in contract).
The draft also carries both triggers — publish (D-46) and «Зробити відео»
(D-77) — which is why this step-2 module is the one steps 3 and 4A touch.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from pisniar.db.models import Post
from pisniar.telegram.keyboards import draft_keyboard


async def deliver_draft(bot: Bot, post: Post, chat_id: int) -> None:
    """Idempotent: a `post` with `draft_message_id` already set is a no-op —
    this is what makes Celery's at-least-once redelivery safe (D-28/M32)."""
    if post.draft_message_id is not None:
        return
    if post.caption is None:
        raise ValueError(f"post {post.id} has no caption to deliver")

    # A caption is full of platform links; without this Telegram appends a
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
