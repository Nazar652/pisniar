"""Alert notifier: sends operational alerts to a private admin chat.

A delivery failure here must never break the caller's business logic (a
failed alert about a failure must not itself raise) — every exception is
caught, logged, and swallowed.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from pisniar.logging import get_logger

logger = get_logger(__name__)


async def send_alert(bot: Bot, chat_id: int, text: str) -> bool:
    """Send `text` to `chat_id`. Returns whether it was delivered.

    Never raises: any Telegram API or network error is logged as a WARNING
    and swallowed, so alerting can never take down the caller.
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramAPIError:
        logger.warning("alert_delivery_failed", chat_id=chat_id, exc_info=True)
        return False
    except Exception:
        logger.warning("alert_delivery_failed_unexpected", chat_id=chat_id, exc_info=True)
        return False
    return True
