"""`deliver_draft`: idempotent (draft_message_id set -> no-op), sends with
explicit `parse_mode="HTML"` and no link preview, and — unlike `send_alert` —
never swallows a delivery failure (contract p.10: недоставка -> ретрай ->
failed)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage

from pisniar.db.models import Post
from pisniar.telegram.draft import deliver_draft
from pisniar.telegram.keyboards import CallbackAction, parse_callback


def make_post(*, caption: str | None = "caption text", draft_message_id: int | None = None) -> Post:
    post = Post(id=1, track_id=1, created_by_user_id=1, caption=caption)
    post.draft_message_id = draft_message_id
    return post


async def test_deliver_draft_sends_html_and_sets_draft_reference() -> None:
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 42
    post = make_post()

    await deliver_draft(bot, post, chat_id=999)

    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 999
    assert sent["text"] == "caption text"
    assert sent["parse_mode"] == "HTML"
    assert post.draft_chat_id == 999
    assert post.draft_message_id == 42


async def test_deliver_draft_attaches_both_triggers() -> None:
    """The draft is the whole trigger for steps 3 and 4A (D-46 + D-77):
    without these buttons a delivered draft goes nowhere by any route."""
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 42
    post = make_post()

    await deliver_draft(bot, post, chat_id=999)

    reply_markup = bot.send_message.await_args.kwargs["reply_markup"]
    parsed = [
        parse_callback(button.callback_data)
        for row in reply_markup.inline_keyboard
        for button in row
    ]
    assert [(one.action, one.post_id) for one in parsed if one is not None] == [
        (CallbackAction.PUBLISH, post.id),
        (CallbackAction.RENDER, post.id),
    ]


async def test_deliver_draft_disables_the_link_preview() -> None:
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 42
    post = make_post(caption='<a href="https://open.spotify.com/track/x">Spotify</a>')

    await deliver_draft(bot, post, chat_id=999)

    link_preview_options = bot.send_message.await_args.kwargs["link_preview_options"]
    assert link_preview_options.is_disabled is True


async def test_deliver_draft_is_idempotent_when_already_delivered() -> None:
    bot = AsyncMock()
    post = make_post(draft_message_id=42)

    await deliver_draft(bot, post, chat_id=999)

    bot.send_message.assert_not_awaited()


async def test_deliver_draft_raises_without_a_caption() -> None:
    bot = AsyncMock()
    post = make_post(caption=None)

    with pytest.raises(ValueError, match="no caption"):
        await deliver_draft(bot, post, chat_id=999)


async def test_deliver_draft_does_not_swallow_telegram_errors() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramAPIError(
        method=SendMessage(chat_id=999, text="x"), message="boom"
    )
    post = make_post()

    with pytest.raises(TelegramAPIError):
        await deliver_draft(bot, post, chat_id=999)

    assert post.draft_message_id is None
