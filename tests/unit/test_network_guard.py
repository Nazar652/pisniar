"""Self-check: the root autouse `outside_access_guard` refuses and records outside access, saying only the host, the
port or the task name, and a swallowed refusal still fails the test.
"""

from __future__ import annotations

import ast
import asyncio
import socket
import textwrap
from pathlib import Path
from typing import Any

import aiohttp.resolver
import pytest

from pisniar.worker.celery_app import build_celery_app

ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
ALLOWLIST_FIXTURE = "outside_access_allowed_hosts"
OUTSIDE_ADDRESS = ("203.0.113.7", 5432)
SECRET = "hunter2"


def test_non_loopback_connect_is_blocked_and_recorded(outside_access_guard: Any) -> None:
    with socket.socket() as client, pytest.raises(RuntimeError, match="reached outside") as refusal:
        client.connect(OUTSIDE_ADDRESS)

    assert type(refusal.value).__name__ == "OutsideAccessError"
    assert outside_access_guard.attempts == ["connect to 203.0.113.7:5432"]
    outside_access_guard.attempts.clear()


def test_connect_ex_is_blocked_and_recorded_like_connect(outside_access_guard: Any) -> None:
    with socket.socket() as client, pytest.raises(RuntimeError, match="reached outside"):
        client.connect_ex(OUTSIDE_ADDRESS)

    assert outside_access_guard.attempts == ["connect to 203.0.113.7:5432"]
    outside_access_guard.attempts.clear()


def test_loopback_connect_is_not_blocked(outside_access_guard: Any) -> None:
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

    assert outside_access_guard.attempts == []


def test_a_datagram_beyond_loopback_is_refused_and_one_to_loopback_is_not(outside_access_guard: Any) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(b"ping", receiver.getsockname())
        with pytest.raises(RuntimeError, match="reached outside"):
            sender.sendto(b"ping", ("203.0.113.7", 53))
    receiver.close()

    assert outside_access_guard.attempts == ["datagram to 203.0.113.7:53"]
    outside_access_guard.attempts.clear()


def test_a_name_lookup_is_refused_unless_it_is_loopback_or_empty(outside_access_guard: Any) -> None:
    with pytest.raises(RuntimeError, match="reached outside"):
        socket.getaddrinfo("redis.example.invalid", 6379)

    assert outside_access_guard.attempts == ["name lookup of redis.example.invalid"]
    outside_access_guard.attempts.clear()
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo(None, 80)
    assert outside_access_guard.attempts == []


def test_celery_send_task_is_refused_by_name_without_the_broker_url(outside_access_guard: Any) -> None:
    app = build_celery_app(f"redis://:{SECRET}@127.0.0.1:1/0")

    with pytest.raises(RuntimeError, match="reached outside") as refusal:
        app.send_task("pisniar.some_task", kwargs={"post_id": 7})

    assert outside_access_guard.attempts == ["task pisniar.some_task"]
    assert SECRET not in str(refusal.value)
    assert SECRET not in "".join(outside_access_guard.attempts)
    outside_access_guard.attempts.clear()


def test_a_swallowed_refusal_still_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(ROOT_CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(
        textwrap.dedent(
            """
            import socket

            def test_swallows_the_refusal():
                try:
                    socket.getaddrinfo("redis.example.invalid", 6379)
                except Exception:
                    pass
            """
        )
    )

    result = pytester.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*the test reached outside its process: name lookup of redis.example.invalid*"])


def test_a_tests_monkeypatch_undo_leaves_the_guard_active(
    monkeypatch: pytest.MonkeyPatch, outside_access_guard: Any
) -> None:
    monkeypatch.undo()

    with pytest.raises(RuntimeError, match="reached outside"):
        socket.getaddrinfo("redis.example.invalid", 6379)

    assert outside_access_guard.attempts == ["name lookup of redis.example.invalid"]
    outside_access_guard.attempts.clear()


def names_the_allowlist_fixture(node: ast.AST) -> bool:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name == ALLOWLIST_FIXTURE
    if isinstance(node, ast.Constant):
        return node.value == ALLOWLIST_FIXTURE
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return node.id == ALLOWLIST_FIXTURE
    return isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and node.attr == ALLOWLIST_FIXTURE


def test_only_the_root_and_the_integration_conftest_define_the_allowlist() -> None:
    # a definition, a string (`name=`, parametrize, `getfixturevalue`) or an assignment elsewhere could widen the
    # allowlist unnoticed by the integration self-checks; a plain parameter named like the fixture is only a use.
    tests_root = ROOT_CONFTEST.parent
    naming: set[str] = set()
    for path in tests_root.rglob("*.py"):
        if any(names_the_allowlist_fixture(node) for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))):
            naming.add(path.relative_to(tests_root).as_posix())

    assert naming - {Path(__file__).resolve().relative_to(tests_root).as_posix()} == {
        "conftest.py",
        "integration/conftest.py",
    }


async def test_no_async_resolver_or_other_loop_goes_around_the_patched_socket_calls() -> None:
    # an async DNS resolver or uvloop would resolve and connect outside the functions the guard patches.
    assert aiohttp.resolver.DefaultResolver is aiohttp.resolver.ThreadedResolver
    assert type(asyncio.get_running_loop()).__module__.startswith("asyncio.")
