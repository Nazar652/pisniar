"""Construction of the aiogram `Bot` client used by `bot`, `worker` and `webapp`.

Each process is an independent Telegram API client — there is no `bot ↔
worker` edge; `worker` and `webapp` build their own `Bot`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiohttp import ClientConnectorError

from pisniar.config import Settings
from pisniar.logging import get_logger

logger = get_logger(__name__)

CONNECT_RETRY_BACKOFF_SECONDS = (1.0, 3.0)


def is_unsent_connect_error(error: BaseException) -> bool:
    """Whether the connector failed, which proves no request was sent."""
    return isinstance(error, TelegramNetworkError) and isinstance(
        error.__cause__, ClientConnectorError
    )


class RetryUnsentRequestMiddleware(BaseRequestMiddleware):
    """Repeats a call whose connection was never established, then re-raises the last error.

    Timeouts and disconnects are not repeated: the request may have been delivered.
    """

    def __init__(
        self,
        backoff_seconds: tuple[float, ...] = CONNECT_RETRY_BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if isinstance(method, GetUpdates):
            return await make_request(bot, method)

        for attempt, delay in enumerate(self.backoff_seconds, start=1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as error:
                if not is_unsent_connect_error(error):
                    raise
                logger.warning(
                    "telegram_request_retried",
                    method=method.__api_method__,
                    attempt=attempt,
                    cause=type(error.__cause__).__name__,
                )
                await self.sleep(delay)

        return await make_request(bot, method)


def build_bot(settings: Settings) -> Bot:
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=None),
    )
    bot.session.middleware(RetryUnsentRequestMiddleware())

    return bot
