"""`build_dispatcher()`: the admin allowlist must land on
`dispatcher.update.outer_middleware`, not e.g. `dispatcher.message.middleware`
— `test_middlewares.py` only exercises the middleware directly, not this
wiring.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher

from pisniar.bot import main as bot_main
from pisniar.bot.main import build_dispatcher
from pisniar.bot.middlewares import AdminAllowlistMiddleware

ADMIN_ID = 111111111


@pytest.fixture(scope="module")
def dispatcher() -> Iterator[Dispatcher]:
    # module-scoped: the handlers router is a singleton aiogram only lets
    # attach once. Tests only inspect the dispatcher, so sharing is safe.
    yield build_dispatcher(frozenset({ADMIN_ID}))


def test_admin_allowlist_is_registered_as_an_outer_update_middleware(
    dispatcher: Dispatcher,
) -> None:
    outer_update_middlewares = list(dispatcher.update.outer_middleware)
    assert any(
        isinstance(middleware, AdminAllowlistMiddleware) for middleware in outer_update_middlewares
    )


def test_admin_allowlist_is_not_registered_on_a_single_observer_instead(
    dispatcher: Dispatcher,
) -> None:
    """a per-observer registration would leave `callback_query` and the rest
    unchecked while the `message` tests stay green."""
    message_inner_middlewares = list(dispatcher.message.middleware)
    assert not any(
        isinstance(middleware, AdminAllowlistMiddleware) for middleware in message_inner_middlewares
    )


def test_admin_allowlist_carries_the_configured_admin_ids(dispatcher: Dispatcher) -> None:
    (middleware,) = [
        entry
        for entry in dispatcher.update.outer_middleware
        if isinstance(entry, AdminAllowlistMiddleware)
    ]
    assert middleware.admin_ids == frozenset({ADMIN_ID})


async def test_the_menu_buttons_are_set_before_polling_starts_and_never_block_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a failing menu call is swallowed inside `configure_admin_menu_buttons`; here only
    the order is pinned, because polling must not start on an unconfigured bot."""
    order: list[str] = []

    async def record_menu(bot: object, admin_ids: object, webapp_public_url: object) -> None:
        order.append("menu")

    async def record_polling(*arguments: object, **keyword_arguments: object) -> None:
        order.append("polling")

    async def serve_briefly() -> None:
        await asyncio.sleep(0.05)

    fake_dispatcher = MagicMock()
    fake_dispatcher.start_polling = record_polling
    fake_server = MagicMock()
    fake_server.serve = serve_briefly
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()

    monkeypatch.setattr(bot_main, "configure_logging", MagicMock())
    monkeypatch.setattr(bot_main, "warn_if_alerts_share_the_channel", MagicMock())
    monkeypatch.setattr(bot_main, "build_bot", MagicMock())
    monkeypatch.setattr(bot_main, "configure_admin_menu_buttons", record_menu)
    monkeypatch.setattr(bot_main, "build_engine", MagicMock(return_value=fake_engine))
    monkeypatch.setattr(bot_main, "build_session_factory", MagicMock())
    monkeypatch.setattr(bot_main, "build_celery_app", MagicMock())
    monkeypatch.setattr(bot_main, "build_dispatcher", MagicMock(return_value=fake_dispatcher))
    monkeypatch.setattr(bot_main, "create_app", MagicMock())
    monkeypatch.setattr(bot_main.uvicorn, "Server", MagicMock(return_value=fake_server))
    monkeypatch.setattr(bot_main.uvicorn, "Config", MagicMock())

    await bot_main.run()

    assert order == ["menu", "polling"]
