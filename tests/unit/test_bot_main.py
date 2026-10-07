"""`build_dispatcher()`: the admin allowlist must land on
`dispatcher.update.outer_middleware`, not e.g. `dispatcher.message.middleware`
— `test_middlewares.py` only exercises the middleware directly, not this
wiring.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from aiogram import Dispatcher

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
