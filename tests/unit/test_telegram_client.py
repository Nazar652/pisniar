"""`build_bot()`: the shared client stays `parse_mode=None` globally. The same client sends alerts, whose text
routinely contains `<` from tracebacks and URLs; a global `parse_mode="HTML"` would make Telegram reject or interpret
them."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetUpdates, SendMessage
from aiohttp import (
    ClientConnectorDNSError,
    ClientConnectorError,
    ConnectionTimeoutError,
    ServerDisconnectedError,
)
from aiohttp.client_reqrep import ConnectionKey

from pisniar.config import get_settings
from pisniar.telegram.client import (
    RetryUnsentRequestMiddleware,
    build_bot,
    is_unsent_connect_error,
)

SEND = SendMessage(chat_id=1, text="x")


def network_error_from(cause: BaseException | None) -> TelegramNetworkError:
    error = TelegramNetworkError(method=SEND, message="HTTP Client says - boom")
    error.__cause__ = cause

    return error


CONNECTION_KEY = ConnectionKey("api.telegram.org", 443, True, True, None, None, None)


def connect_error() -> TelegramNetworkError:
    return network_error_from(ClientConnectorError(CONNECTION_KEY, OSError("unreachable")))


def make_middleware() -> tuple[RetryUnsentRequestMiddleware, AsyncMock]:
    sleep = AsyncMock()

    return RetryUnsentRequestMiddleware(backoff_seconds=(1.0, 3.0), sleep=sleep), sleep


@pytest.mark.parametrize(
    "error",
    [
        connect_error(),
        network_error_from(ClientConnectorDNSError(CONNECTION_KEY, OSError("no such host"))),
    ],
)
def test_is_unsent_connect_error_accepts_a_connect_failure(error: BaseException) -> None:
    assert is_unsent_connect_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        network_error_from(TimeoutError()),
        network_error_from(ConnectionTimeoutError()),
        network_error_from(ServerDisconnectedError()),
        network_error_from(None),
        TelegramBadRequest(method=SEND, message="Bad Request: chat not found"),
        ValueError("x"),
    ],
)
def test_is_unsent_connect_error_rejects_everything_else(error: BaseException) -> None:
    assert is_unsent_connect_error(error) is False


async def test_middleware_retries_a_connect_error_then_succeeds() -> None:
    middleware, sleep = make_middleware()
    make_request = AsyncMock(side_effect=[connect_error(), "ok"])

    result = await middleware(make_request, AsyncMock(), SEND)

    assert result == "ok"
    assert make_request.await_count == 2
    sleep.assert_awaited_once_with(1.0)


async def test_middleware_gives_up_after_three_attempts_and_raises_the_last_error() -> None:
    middleware, sleep = make_middleware()
    errors = [connect_error(), connect_error(), connect_error()]
    make_request = AsyncMock(side_effect=errors)

    with pytest.raises(TelegramNetworkError) as raised:
        await middleware(make_request, AsyncMock(), SEND)

    assert raised.value is errors[-1]
    assert make_request.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 3.0]


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: network_error_from(TimeoutError()),
        lambda: network_error_from(ConnectionTimeoutError()),
        lambda: network_error_from(ServerDisconnectedError()),
        lambda: TelegramBadRequest(method=SEND, message="Bad Request: chat not found"),
    ],
)
async def test_middleware_does_not_retry_a_possibly_delivered_request(
    error_factory: Callable[[], Exception],
) -> None:
    middleware, sleep = make_middleware()
    error = error_factory()
    make_request = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as raised:
        await middleware(make_request, AsyncMock(), SEND)

    assert raised.value is error
    assert make_request.await_count == 1
    sleep.assert_not_awaited()


async def test_middleware_leaves_long_polling_to_aiogram() -> None:
    middleware, sleep = make_middleware()
    make_request = AsyncMock(side_effect=connect_error())

    with pytest.raises(TelegramNetworkError):
        await middleware(make_request, AsyncMock(), GetUpdates())

    assert make_request.await_count == 1
    sleep.assert_not_awaited()


async def test_build_bot_registers_the_retry_middleware() -> None:
    bot = build_bot(get_settings())
    try:
        assert any(isinstance(m, RetryUnsentRequestMiddleware) for m in bot.session.middleware)
    finally:
        await bot.session.close()


async def test_shared_bot_has_no_global_parse_mode() -> None:
    bot = build_bot(get_settings())
    try:
        assert bot.default.parse_mode is None
    finally:
        await bot.session.close()
