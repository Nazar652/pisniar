"""Self-check: the integration allowlist holds only loopback and the database; the broker and Telegram are out."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiohttp.resolver
import pytest
from sqlalchemy.engine import make_url

from pisniar.config import get_settings
from pisniar.worker.celery_app import build_celery_app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("host", ["redis", "api.telegram.org"])
def test_a_lookup_of_the_broker_or_telegram_is_refused_and_recorded(outside_access_guard: Any, host: str) -> None:
    with pytest.raises(RuntimeError, match="reached outside"):
        socket.getaddrinfo(host, 443)

    assert outside_access_guard.attempts == [f"name lookup of {host}"]
    outside_access_guard.attempts.clear()


def test_a_task_send_is_refused_and_recorded(outside_access_guard: Any) -> None:
    app = build_celery_app("redis://redis:6379/0")

    with pytest.raises(RuntimeError, match="reached outside"):
        app.send_task("pisniar.some_task")

    assert outside_access_guard.attempts == ["task pisniar.some_task"]
    outside_access_guard.attempts.clear()


def test_a_datagram_message_beyond_the_allowlist_is_refused_and_one_to_loopback_is_not(
    outside_access_guard: Any,
) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendmsg([b"ping"], [], 0, receiver.getsockname())
        with pytest.raises(RuntimeError, match="reached outside"):
            sender.sendmsg([b"ping"], [], 0, ("203.0.113.7", 53))
    receiver.close()

    assert outside_access_guard.attempts == ["datagram to 203.0.113.7:53"]
    outside_access_guard.attempts.clear()


def test_the_database_host_and_its_address_are_allowed(outside_access_guard: Any) -> None:
    url = make_url(get_settings().database_url)
    assert url.host
    address = str(socket.getaddrinfo(url.host, url.port or 5432)[0][4][0])

    with socket.create_connection((address, url.port or 5432), timeout=5):
        pass

    assert outside_access_guard.attempts == []


async def test_no_async_resolver_or_other_loop_goes_around_the_patched_socket_calls() -> None:
    # an async DNS resolver or uvloop would resolve and connect outside the functions the guard patches.
    assert aiohttp.resolver.DefaultResolver is aiohttp.resolver.ThreadedResolver
    assert type(asyncio.get_running_loop()).__module__.startswith("asyncio.")
