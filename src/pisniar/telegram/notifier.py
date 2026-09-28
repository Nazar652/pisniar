"""Operational alerts to a private admin chat. A delivery failure never
breaks the caller's business logic: every exception is logged and swallowed.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions, ReplyParameters

from pisniar.logging import get_logger

logger = get_logger(__name__)


async def send_alert(
    bot: Bot, chat_id: int, text: str, *, reply_to_message_id: int | None = None
) -> bool:
    try:
        # A deleted reply target must not cost the alert itself.
        reply_parameters = (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id is not None
            else None
        )

        # An alert regularly carries a URL; a preview card would push the
        # next alert off screen and make the channel unscannable.
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_parameters=reply_parameters,
        )
    except TelegramAPIError:
        logger.warning("alert_delivery_failed", chat_id=chat_id, exc_info=True)
        return False
    except Exception:
        logger.warning("alert_delivery_failed_unexpected", chat_id=chat_id, exc_info=True)
        return False
    return True
