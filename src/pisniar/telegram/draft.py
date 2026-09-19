"""Draft delivery — NOT `send_alert()` (contract p.10): a draft is the
product, so a delivery failure must propagate (-> `failed` + `/retry`),
never be swallowed. `parse_mode="HTML"` is passed per-call here only —
the shared `Bot` stays `parse_mode=None` everywhere else (D-2 in contract).
"""

from __future__ import annotations

from aiogram import Bot

from pisniar.db.models import Post


async def deliver_draft(bot: Bot, post: Post, chat_id: int) -> None:
    """Idempotent: a `post` with `draft_message_id` already set is a no-op —
    this is what makes Celery's at-least-once redelivery safe (D-28/M32)."""
    if post.draft_message_id is not None:
        return
    if post.caption is None:
        raise ValueError(f"post {post.id} has no caption to deliver")

    sent_message = await bot.send_message(chat_id=chat_id, text=post.caption, parse_mode="HTML")
    post.draft_chat_id = chat_id
    post.draft_message_id = sent_message.message_id
