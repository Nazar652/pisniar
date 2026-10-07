"""The Mini App menu button: one `set_chat_menu_button` per admin, a reset to the default
button when no address is configured, and no failure that stops the bot from starting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetChatMenuButton
from aiogram.types import MenuButtonDefault, MenuButtonWebApp

from pisniar.telegram.menu import configure_admin_menu_buttons

URL = "https://pisniar.example.ts.net"
ADMINS = [111, 222]


def calls_by_chat(bot: AsyncMock) -> dict[int, object]:
    return {
        call.kwargs["chat_id"]: call.kwargs["menu_button"]
        for call in bot.set_chat_menu_button.await_args_list
    }


async def test_every_admin_gets_the_posts_button_on_their_own_chat() -> None:
    bot = AsyncMock()

    await configure_admin_menu_buttons(bot, ADMINS, URL)

    buttons = calls_by_chat(bot)
    assert set(buttons) == set(ADMINS)
    for button in buttons.values():
        assert isinstance(button, MenuButtonWebApp)
        assert button.text == "Пости"
        assert button.web_app.url == f"{URL}/"


async def test_the_global_default_button_is_never_touched() -> None:
    bot = AsyncMock()

    await configure_admin_menu_buttons(bot, ADMINS, URL)

    assert all(
        call.kwargs.get("chat_id") is not None for call in bot.set_chat_menu_button.await_args_list
    )


async def test_without_an_address_each_admins_button_is_reset_to_the_default() -> None:
    bot = AsyncMock()

    await configure_admin_menu_buttons(bot, ADMINS, None)

    buttons = calls_by_chat(bot)
    assert set(buttons) == set(ADMINS)
    assert all(isinstance(button, MenuButtonDefault) for button in buttons.values())


async def test_an_admin_who_never_wrote_to_the_bot_does_not_stop_the_others() -> None:
    bot = AsyncMock()
    bot.set_chat_menu_button.side_effect = [
        TelegramBadRequest(
            method=SetChatMenuButton(chat_id=111), message="Bad Request: user not found"
        ),
        True,
    ]

    await configure_admin_menu_buttons(bot, ADMINS, URL)

    assert bot.set_chat_menu_button.await_count == 2


async def test_an_unexpected_failure_is_logged_and_never_raised() -> None:
    bot = AsyncMock()
    bot.set_chat_menu_button.side_effect = RuntimeError("network is down")

    await configure_admin_menu_buttons(bot, ADMINS, URL)

    assert bot.set_chat_menu_button.await_count == 2


async def test_running_twice_sets_the_same_buttons_again() -> None:
    bot = AsyncMock()

    await configure_admin_menu_buttons(bot, ADMINS, URL)
    await configure_admin_menu_buttons(bot, ADMINS, URL)

    assert bot.set_chat_menu_button.await_count == 4
