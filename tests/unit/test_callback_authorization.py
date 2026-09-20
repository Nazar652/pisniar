"""D-46 claimed the allowlist covers an inline button "automatically",
because `event_from_user` is populated for a `callback_query` too. Publishing
is the most destructive operation the system has, so that claim is verified
here by feeding real updates through a real dispatcher rather than trusted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from pisniar.bot import middlewares
from pisniar.bot.middlewares import AdminAllowlistMiddleware

ADMIN_ID = 111111111
STRANGER_ID = 999999999


@pytest.fixture(autouse=True)
def rejection_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The module logger is a structlog lazy proxy that caches itself on
    first use, and a cached proxy silently defeats `capture_logs()` in every
    later test — so this file logs through a stand-in instead."""
    fake_logger = MagicMock()
    monkeypatch.setattr(middlewares, "logger", fake_logger)
    return fake_logger


@pytest.fixture
def bot() -> Bot:
    return Bot(token="000000000:test-token-not-a-real-secret")


@pytest.fixture
def reached() -> list[str]:
    return []


@pytest.fixture
def dispatcher(reached: list[str]) -> Iterator[Dispatcher]:
    """Mirrors `build_dispatcher()`'s wiring with a spy router in place of
    the real handlers — the production router is a singleton aiogram only
    lets attach once, and this test needs its own."""
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(AdminAllowlistMiddleware(frozenset({ADMIN_ID})))

    router = Router(name="spy")

    @router.callback_query()
    async def spy_callback(callback: CallbackQuery) -> None:
        reached.append(callback.data or "")

    @router.message()
    async def spy_message(message: Message) -> None:
        reached.append(message.text or "")

    dispatcher.include_router(router)
    yield dispatcher


def make_callback_update(user_id: int, update_id: int = 1) -> Update:
    user = User(id=user_id, is_bot=False, first_name="X")
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id="cb-1",
            from_user=user,
            chat_instance="chat-instance-1",
            data="publish:7",
            message=Message(
                message_id=5,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
            ),
        ),
    )


async def test_a_non_admin_button_tap_never_reaches_a_handler(
    dispatcher: Dispatcher, bot: Bot, reached: list[str], rejection_logger: MagicMock
) -> None:
    await dispatcher.feed_update(bot, make_callback_update(STRANGER_ID))

    assert reached == []
    assert rejection_logger.warning.call_args.args[0] == "update_rejected_non_admin"


async def test_an_admin_button_tap_does_reach_its_handler(
    dispatcher: Dispatcher, bot: Bot, reached: list[str]
) -> None:
    """The other half of the claim: the allowlist must not be blocking every
    callback, which would make the test above pass for the wrong reason."""
    await dispatcher.feed_update(bot, make_callback_update(ADMIN_ID))

    assert reached == ["publish:7"]
