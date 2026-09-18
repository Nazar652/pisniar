"""The diagnostic `ping` task, run through Celery's eager mode (no real
broker) with Postgres and Telegram fully mocked out — this stays a unit
test, not an `integration` one."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.worker import tasks as worker_tasks


@pytest.fixture
def eager_celery_app() -> Iterator[None]:
    worker_tasks.celery_app.conf.task_always_eager = True
    worker_tasks.celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        worker_tasks.celery_app.conf.task_always_eager = False
        worker_tasks.celery_app.conf.task_eager_propagates = False


def test_ping_task_executes_synchronously_checks_db_and_sends_alert(
    eager_celery_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = AsyncMock()

    @asynccontextmanager
    async def fake_session_context() -> AsyncIterator[AsyncMock]:
        yield fake_session

    fake_session_factory = MagicMock(side_effect=fake_session_context)
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.session.close = AsyncMock()

    send_alert_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(worker_tasks, "build_engine", MagicMock(return_value=fake_engine))
    monkeypatch.setattr(
        worker_tasks, "build_session_factory", MagicMock(return_value=fake_session_factory)
    )
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=fake_bot))
    monkeypatch.setattr(worker_tasks, "send_alert", send_alert_mock)

    result = worker_tasks.ping.delay(post_id=123)

    assert result.successful(), result.result
    fake_session.execute.assert_awaited_once()
    fake_engine.dispose.assert_awaited_once()
    fake_bot.session.close.assert_awaited_once()

    send_alert_mock.assert_awaited_once()
    call_args = send_alert_mock.await_args
    assert call_args is not None
    _bot_arg, chat_id_arg, message_arg = call_args.args
    assert chat_id_arg == worker_tasks.settings.alert_chat_id
    assert "123" in message_arg


def test_ping_task_succeeds_even_when_the_alert_fails_to_deliver(
    eager_celery_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`send_alert` returning `False` (its documented failure mode — see
    `tests/unit/test_notifier.py`) must not fail the task itself: an alert
    about a diagnostic is not the diagnostic."""
    fake_session = AsyncMock()

    @asynccontextmanager
    async def fake_session_context() -> AsyncIterator[AsyncMock]:
        yield fake_session

    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.session.close = AsyncMock()

    monkeypatch.setattr(worker_tasks, "build_engine", MagicMock(return_value=fake_engine))
    monkeypatch.setattr(
        worker_tasks,
        "build_session_factory",
        MagicMock(return_value=MagicMock(side_effect=fake_session_context)),
    )
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=fake_bot))
    monkeypatch.setattr(worker_tasks, "send_alert", AsyncMock(return_value=False))

    result = worker_tasks.ping.delay(post_id=123)

    assert result.successful(), result.result
    fake_engine.dispose.assert_awaited_once()


def test_ping_task_without_post_id_still_executes(
    eager_celery_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = AsyncMock()

    @asynccontextmanager
    async def fake_session_context() -> AsyncIterator[AsyncMock]:
        yield fake_session

    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.session.close = AsyncMock()

    monkeypatch.setattr(worker_tasks, "build_engine", MagicMock(return_value=fake_engine))
    monkeypatch.setattr(
        worker_tasks,
        "build_session_factory",
        MagicMock(return_value=MagicMock(side_effect=fake_session_context)),
    )
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=fake_bot))
    monkeypatch.setattr(worker_tasks, "send_alert", AsyncMock(return_value=True))

    result = worker_tasks.ping.delay()

    assert result.successful(), result.result
