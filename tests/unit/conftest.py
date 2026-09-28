"""No unit test may reach the network: `socket.socket.connect` is patched to
raise for any non-loopback address. Loopback stays open because Windows'
`ProactorEventLoop` uses a real local socket pair for its self-pipe.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class NetworkAccessDisabledInUnitTestsError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def block_network_access(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: tuple[Any, ...] | str) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK_HOSTS:
            return original_connect(self, address)
        raise NetworkAccessDisabledInUnitTestsError(
            f"unit test attempted a real network connection to {address!r}; mock it instead"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
