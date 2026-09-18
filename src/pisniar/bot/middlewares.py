"""Admin allowlist — the single barrier between this bot and all of Telegram.

Registered as an *outer* middleware on `dp.update`, so it runs for every
update type before any handler is even looked up. A non-admin update is
dropped silently (no reply at all, per the approved assumption) and logged
at WARNING. Get the registration order wrong and the bot is open to the
whole internet — see the security review notes in the task contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast, override

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from pisniar.logging import bind_update_id, get_logger

logger = get_logger(__name__)


class AdminAllowlistMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = cast(Update, event)
        bind_update_id(update.update_id)

        user = data.get("event_from_user")
        user_id = user.id if user is not None else None

        if user_id is None or user_id not in self.admin_ids:
            logger.warning("update_rejected_non_admin", user_id=user_id, update_id=update.update_id)
            return None

        return await handler(event, data)
