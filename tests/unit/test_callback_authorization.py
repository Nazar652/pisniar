"""The allowlist is expected to cover inline buttons because `event_from_user` is
populated for a `callback_query` too. Publishing is the most destructive operation
there is, so that is verified with real updates through a real dispatcher.
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
    """A cached structlog proxy silently defeats `capture_logs()` in every later
    test, so this file logs through a stand-in."""
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
    """Mirrors `build_dispatcher()` with a spy router: the production router is a
    singleton aiogram lets attach only once."""
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


def make_callback_update(
    user_id: int, update_id: int = 1, callback_data: str = "publish:7"
) -> Update:
    user = User(id=user_id, is_bot=False, first_name="X")
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id="cb-1",
            from_user=user,
            chat_instance="chat-instance-1",
            data=callback_data,
            message=Message(
                message_id=5,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
            ),
        ),
    )


@pytest.mark.parametrize("callback_data", ["publish:7", "render:7"])
async def test_a_non_admin_button_tap_never_reaches_a_handler(
    dispatcher: Dispatcher,
    bot: Bot,
    reached: list[str],
    rejection_logger: MagicMock,
    callback_data: str,
) -> None:
    """Both buttons: «Зробити відео» spends minutes of CPU and hundreds of
    megabytes, so it is verified rather than assumed to inherit the check."""
    await dispatcher.feed_update(
        bot, make_callback_update(STRANGER_ID, callback_data=callback_data)
    )

    assert reached == []
    assert rejection_logger.warning.call_args.args[0] == "update_rejected_non_admin"


@pytest.mark.parametrize("callback_data", ["publish:7", "render:7"])
async def test_an_admin_button_tap_does_reach_its_handler(
    dispatcher: Dispatcher, bot: Bot, reached: list[str], callback_data: str
) -> None:
    """The other half of the claim: the allowlist must not be blocking every
    callback, which would make the test above pass for the wrong reason."""
    await dispatcher.feed_update(bot, make_callback_update(ADMIN_ID, callback_data=callback_data))

    assert reached == [callback_data]
