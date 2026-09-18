"""`AdminAllowlistMiddleware`: the single barrier between this bot and all of
Telegram. A regression here — e.g. swapping the outer-middleware
registration for a per-observer one, so only `message` updates get checked —
must fail a test on its own, not slip through `make check` silently."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import structlog.testing
from aiogram.types import TelegramObject, Update, User

from pisniar.bot.middlewares import AdminAllowlistMiddleware

ADMIN_ID = 111111111
STRANGER_ID = 999999999


def make_update(update_id: int = 1) -> Update:
    return Update(update_id=update_id)


async def test_non_admin_update_is_dropped_silently_with_a_warning() -> None:
    middleware = AdminAllowlistMiddleware(frozenset({ADMIN_ID}))
    handler = AsyncMock()
    event: TelegramObject = make_update()
    data: dict[str, Any] = {"event_from_user": User(id=STRANGER_ID, is_bot=False, first_name="X")}

    with structlog.testing.capture_logs() as captured_logs:
        result = await middleware(handler, event, data)

    handler.assert_not_awaited()
    assert result is None
    warnings = [entry for entry in captured_logs if entry["log_level"] == "warning"]
    assert any(entry["event"] == "update_rejected_non_admin" for entry in warnings)
    assert any(entry.get("user_id") == STRANGER_ID for entry in warnings)


async def test_admin_update_reaches_the_handler() -> None:
    middleware = AdminAllowlistMiddleware(frozenset({ADMIN_ID}))
    handler = AsyncMock(return_value="handled")
    event: TelegramObject = make_update()
    data: dict[str, Any] = {"event_from_user": User(id=ADMIN_ID, is_bot=False, first_name="Admin")}

    result = await middleware(handler, event, data)

    handler.assert_awaited_once_with(event, data)
    assert result == "handled"


async def test_update_with_no_event_from_user_is_dropped() -> None:
    """`UserContextMiddleware` (registered ahead of this one) does not
    populate `event_from_user` for every update type (e.g. a poll answer
    with an anonymous voter) — no user must mean no access, not a crash."""
    middleware = AdminAllowlistMiddleware(frozenset({ADMIN_ID}))
    handler = AsyncMock()
    event: TelegramObject = make_update()

    result = await middleware(handler, event, {})

    handler.assert_not_awaited()
    assert result is None
