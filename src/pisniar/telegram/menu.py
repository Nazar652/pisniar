"""The Mini App menu button, set for each admin's private chat on its own.

The global default button is never touched, so everyone else keeps the standard one.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram import Bot
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

from pisniar.logging import get_logger
from pisniar.telegram.keyboards import POST_LIST_BUTTON_TEXT, webapp_url

logger = get_logger(__name__)


async def configure_admin_menu_buttons(
    bot: Bot, admin_ids: Iterable[int], webapp_public_url: str | None
) -> None:
    """idempotent, run on every start; without a URL it resets each admin's button. A failure
    for one admin (say, one who never wrote to the bot) is logged and never stops the rest."""
    menu_button = (
        MenuButtonDefault()
        if webapp_public_url is None
        else MenuButtonWebApp(
            text=POST_LIST_BUTTON_TEXT, web_app=WebAppInfo(url=webapp_url(webapp_public_url))
        )
    )

    for admin_id in admin_ids:
        try:
            await bot.set_chat_menu_button(chat_id=admin_id, menu_button=menu_button)
        except Exception:
            logger.warning("admin_menu_button_failed", admin_id=admin_id, exc_info=True)
            continue

        logger.info(
            "admin_menu_button_set", admin_id=admin_id, webapp=webapp_public_url is not None
        )
