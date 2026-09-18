"""`build_dispatcher()`: the admin allowlist middleware must be registered
as an *outer* middleware on `dispatcher.update` — anything else (e.g.
`dispatcher.message.middleware`) leaves every non-`message` update type
unchecked, and would not be caught by `tests/unit/test_middlewares.py`
alone, since that file only exercises `AdminAllowlistMiddleware` directly
without touching how `bot/main.py` actually wires it up.
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
    # Module-scoped, not one per test: `pisniar.bot.handlers.router` is a
    # module-level singleton (the idiomatic aiogram pattern), and aiogram's
    # `Router` can only ever be attached to one `Dispatcher` for its
    # lifetime — a second `build_dispatcher()` call in the same process
    # raises `RuntimeError: Router is already attached`. Every test here
    # only inspects the dispatcher, never mutates or consumes it, so sharing
    # one instance is safe.
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
    """The regression this guards against: swapping the outer-middleware
    registration for a per-observer one (e.g. `dispatcher.message.middleware`)
    would leave `callback_query`/`inline_query`/`my_chat_member`/... updates
    unchecked while looking identical for `message` updates."""
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
