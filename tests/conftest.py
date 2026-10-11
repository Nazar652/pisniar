"""Shared test setup: deterministic fake `Settings` values, since some modules call `get_settings()` at import time.
`os.environ` outranks `.env`, so these never leak real secrets on the host; inside Docker `.env` is already the process
env, so `setdefault` is a no-op there.
"""

import logging
import os
import socket
from collections.abc import Collection, Iterator
from typing import Any

import pytest
import structlog
from celery import Celery

pytest_plugins = ["pytester"]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("BOT_TOKEN", "000000000:test-token-not-a-real-secret")
os.environ.setdefault("ADMIN_IDS", "111111111,222222222")
os.environ.setdefault("ALERT_CHAT_ID", "111111111")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar")
os.environ.setdefault("REDIS_URL", "redis://redis:6379/0")
os.environ.setdefault("HTTP_HOST", "0.0.0.0")
os.environ.setdefault("HTTP_PORT", "8080")
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-spotify-client-id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test-spotify-client-secret-not-real")
os.environ.setdefault("ITUNES_COUNTRY", "UA")


@pytest.fixture
def preserve_root_logging_state() -> Iterator[None]:
    """`configure_logging()` replaces root handlers and structlog's processor list; a test calling it must restore both,
    or loggers cached earlier keep the old list and a later `capture_logs()` misses them (order-dependent under xdist).
    """
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_structlog_config = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**original_structlog_config)
        root_logger.handlers = original_handlers
        root_logger.setLevel(original_level)


class OutsideAccessError(RuntimeError):
    pass


class OutsideAccessGuard:
    def __init__(self) -> None:
        self.attempts: list[str] = []


def install_outside_access_guard(monkeypatch: pytest.MonkeyPatch, allowed_hosts: Collection[str]) -> OutsideAccessGuard:
    """refuses and records connects, name lookups, datagrams and task sends beyond `allowed_hosts`; no URL is logged.
    gap: an IP-literal asyncio connect on the Windows host (Proactor `ConnectEx`) never reaches `socket.connect`."""
    guard = OutsideAccessGuard()
    allowed = frozenset(allowed_hosts)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_sendmsg: Any = getattr(socket.socket, "sendmsg", None)
    original_getaddrinfo = socket.getaddrinfo

    def refuse(what: str) -> OutsideAccessError:
        guard.attempts.append(what)
        return OutsideAccessError(f"test reached outside its process: {what}; patch it instead")

    def destination_of(address: tuple[Any, ...] | str) -> tuple[Any, Any]:
        return (address[0], address[1]) if isinstance(address, tuple) else (address, None)

    def guarded_connect(self: socket.socket, address: tuple[Any, ...] | str) -> object:
        host, port = destination_of(address)
        if host in allowed:
            return original_connect(self, address)
        raise refuse(f"connect to {host}:{port}")

    def guarded_connect_ex(self: socket.socket, address: tuple[Any, ...] | str) -> int:
        host, port = destination_of(address)
        if host in allowed:
            return original_connect_ex(self, address)
        raise refuse(f"connect to {host}:{port}")

    def guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
        host, port = destination_of(args[-1]) if args else (None, None)
        if args and host not in allowed:
            raise refuse(f"datagram to {host}:{port}")
        return original_sendto(self, data, *args)

    def guarded_sendmsg(
        self: socket.socket,
        buffers: Any,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        address = kwargs.get("address", args[2] if len(args) > 2 else None)
        if address is not None:
            host, port = destination_of(address)
            if host not in allowed:
                raise refuse(f"datagram to {host}:{port}")
        return original_sendmsg(self, buffers, *args, **kwargs)

    def guarded_getaddrinfo(
        host: bytes | str | None,
        port: bytes | str | int | None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name is None or name == "" or name in allowed:
            return original_getaddrinfo(host, port, *args, **kwargs)
        raise refuse(f"name lookup of {name}")

    def guarded_send_task(
        self: Celery,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        raise refuse(f"task {name}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    if original_sendmsg is not None:
        monkeypatch.setattr(socket.socket, "sendmsg", guarded_sendmsg)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(Celery, "send_task", guarded_send_task)
    return guard


@pytest.fixture
def outside_access_allowed_hosts() -> frozenset[str]:
    """loopback only: the Windows Proactor loop needs a local socket pair, and on the host loopback also reaches the
    published Redis (127.0.0.1:56379) and Postgres, so only a direct kombu, redis or database client would pass."""
    return LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def outside_access_guard(outside_access_allowed_hosts: frozenset[str]) -> Iterator[OutsideAccessGuard]:
    """a recorded attempt fails the test at teardown, even when the code under test swallowed the error. it owns its
    `MonkeyPatch`, so a test's `monkeypatch.undo()` cannot remove the guard."""
    patcher = pytest.MonkeyPatch()
    try:
        guard = install_outside_access_guard(patcher, outside_access_allowed_hosts)
        yield guard
        if guard.attempts:
            raise pytest.fail.Exception("the test reached outside its process: " + "; ".join(guard.attempts))
    finally:
        patcher.undo()
