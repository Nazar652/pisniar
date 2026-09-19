"""V16 self-check: the autouse `block_network_access` fixture actually
blocks a real (non-loopback) connection attempt."""

from __future__ import annotations

import socket

import pytest


def test_non_loopback_connect_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="unit test attempted a real network connection"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))


def test_loopback_connect_is_not_blocked() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))  # must not raise
    finally:
        client.close()
        listener.close()
