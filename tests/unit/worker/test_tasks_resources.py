"""The resource context managers of the worker tasks: the `Bot` is closed and the engine is
disposed on every way out, and the engine arguments are exactly the caller's.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.db.session import LOCK_TIMEOUT_MS
from pisniar.worker import tasks as worker_tasks


class BodyError(Exception):
    pass


@pytest.fixture
def fake_bot(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=bot))
    return bot


@pytest.fixture
def engine_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    build_engine = MagicMock(return_value=engine)
    session_factory = MagicMock()
    build_session_factory = MagicMock(return_value=session_factory)
    monkeypatch.setattr(worker_tasks, "build_engine", build_engine)
    monkeypatch.setattr(worker_tasks, "build_session_factory", build_session_factory)
    return engine, build_engine, build_session_factory


async def test_bot_client_yields_the_bot_and_closes_its_session_after_the_block(
    fake_bot: MagicMock,
) -> None:
    async with worker_tasks.bot_client() as bot:
        assert bot is fake_bot
        fake_bot.session.close.assert_not_awaited()

    fake_bot.session.close.assert_awaited_once()


async def test_bot_client_closes_its_session_when_the_block_raises(fake_bot: MagicMock) -> None:
    with pytest.raises(BodyError):
        async with worker_tasks.bot_client():
            raise BodyError

    fake_bot.session.close.assert_awaited_once()


async def test_task_session_factory_builds_a_null_pool_engine_with_the_given_lock_timeout(
    engine_calls: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    engine, build_engine, build_session_factory = engine_calls

    async with worker_tasks.task_session_factory(lock_timeout_ms=LOCK_TIMEOUT_MS) as factory:
        assert factory is build_session_factory.return_value
        engine.dispose.assert_not_awaited()

    build_engine.assert_called_once_with(
        worker_tasks.settings.database_url, use_null_pool=True, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    build_session_factory.assert_called_once_with(engine)
    engine.dispose.assert_awaited_once()


async def test_task_session_factory_sets_no_lock_timeout_unless_asked(
    engine_calls: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    _, build_engine, _ = engine_calls

    async with worker_tasks.task_session_factory():
        pass

    assert build_engine.call_args.kwargs["lock_timeout_ms"] is None


async def test_task_session_factory_disposes_the_engine_when_the_block_raises(
    engine_calls: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    engine, _, _ = engine_calls

    with pytest.raises(BodyError):
        async with worker_tasks.task_session_factory():
            raise BodyError

    engine.dispose.assert_awaited_once()
