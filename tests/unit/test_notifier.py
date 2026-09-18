"""`send_alert`: a delivery failure must never raise into the caller's
business logic — this is the contract `pisniar.worker.tasks` and
`pisniar.bot.handlers` both depend on."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage

from pisniar.telegram.notifier import send_alert


def make_telegram_api_error() -> TelegramAPIError:
    return TelegramAPIError(method=SendMessage(chat_id=1, text="x"), message="boom")


async def test_send_alert_returns_true_on_success() -> None:
    bot = AsyncMock()

    delivered = await send_alert(bot, chat_id=123, text="hello")

    assert delivered is True
    bot.send_message.assert_awaited_once_with(chat_id=123, text="hello")


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

    # The point of the test: no `pytest.raises` needed — falling through to
    # this line at all is the assertion.
    await send_alert(bot, chat_id=123, text="hello")
