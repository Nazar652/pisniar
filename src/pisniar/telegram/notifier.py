"""Operational alerts to a private admin chat. A delivery failure never
breaks the caller's business logic: every exception is logged and swallowed.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from pisniar.logging import get_logger

logger = get_logger(__name__)


async def send_alert(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        # An alert regularly carries a URL; a preview card would push the
        # next alert off screen and make the channel unscannable.
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramAPIError:
        logger.warning("alert_delivery_failed", chat_id=chat_id, exc_info=True)
        return False
    except Exception:
        logger.warning("alert_delivery_failed_unexpected", chat_id=chat_id, exc_info=True)
        return False
    return True
