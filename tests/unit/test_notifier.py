"""`send_alert`: a delivery failure must never raise into the caller's
business logic — this is the contract `pisniar.worker.tasks` and
`pisniar.bot.handlers` both depend on — and an alert carrying a URL must
not drag a full-screen link preview into the operational channel."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pisniar.telegram.notifier import send_alert


def make_telegram_api_error() -> TelegramAPIError:
    return TelegramAPIError(method=SendMessage(chat_id=1, text="x"), message="boom")


async def test_send_alert_returns_true_on_success() -> None:
    bot = AsyncMock()

    delivered = await send_alert(bot, chat_id=123, text="hello")

    assert delivered is True
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 123
    assert sent["text"] == "hello"


async def test_send_alert_disables_the_link_preview() -> None:
    bot = AsyncMock()

    await send_alert(bot, chat_id=123, text="duplicate: https://open.spotify.com/track/x")

    link_preview_options = bot.send_message.await_args.kwargs["link_preview_options"]
    assert link_preview_options.is_disabled is True


async def test_send_alert_swallows_telegram_api_error() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = make_telegram_api_error()

    delivered = await send_alert(bot, chat_id=123, text="hello")

    assert delivered is False


async def test_send_alert_swallows_unexpected_exception() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("network is on fire")

    delivered = await send_alert(bot, chat_id=123, text="hello")

    assert delivered is False


@pytest.mark.parametrize("side_effect", [make_telegram_api_error(), RuntimeError("boom")])
async def test_send_alert_never_raises(side_effect: Exception) -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = side_effect

    # the point of the test: no `pytest.raises` needed — falling through to
    # this line at all is the assertion.
    await send_alert(bot, chat_id=123, text="hello")


async def test_an_alert_can_answer_a_message_and_survives_its_deletion() -> None:
    """a notice sits under the preview it explains; a preview deleted meanwhile
    must not cost the notice."""
    bot = AsyncMock()

    await send_alert(bot, chat_id=123, text="hello", reply_to_message_id=77)

    reply_parameters = bot.send_message.await_args.kwargs["reply_parameters"]
    assert reply_parameters.message_id == 77
    assert reply_parameters.allow_sending_without_reply is True


async def test_a_plain_alert_answers_nothing() -> None:
    bot = AsyncMock()

    await send_alert(bot, chat_id=123, text="hello")

    assert bot.send_message.await_args.kwargs["reply_parameters"] is None


async def test_send_alert_passes_the_keyboard_through() -> None:
    bot = AsyncMock()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="publish:1")]]
    )

    await send_alert(bot, chat_id=123, text="hello", reply_markup=keyboard)

    assert bot.send_message.await_args.kwargs["reply_markup"] is keyboard


async def test_send_alert_sends_no_keyboard_by_default() -> None:
    bot = AsyncMock()

    await send_alert(bot, chat_id=123, text="hello")

    assert bot.send_message.await_args.kwargs["reply_markup"] is None


async def test_a_rejected_keyboard_still_costs_nothing_but_the_alert() -> None:
    """a 400 on the keyboard is swallowed like any delivery failure."""
    bot = AsyncMock()
    bot.send_message.side_effect = make_telegram_api_error()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="publish:1")]]
    )

    assert await send_alert(bot, chat_id=-5, text="hello", reply_markup=keyboard) is False
