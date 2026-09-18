"""Construction of the aiogram `Bot` client shared by `bot` and `worker`.

Both processes are independent Telegram API clients — there is no `bot ↔
worker` edge. `worker` builds its own `Bot` instance (see
`pisniar.telegram.notifier`) purely to send alerts; it never talks to the
`bot` process directly.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from pisniar.config import Settings


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=None),
    )
