"""`handle_tap`, the helper under both buttons: the event names and fields are the caller's,
and the order is database, keyboard, dispatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from pisniar.bot import handlers
from pisniar.db.states import PostStatus

POST_ID = 7
USER_ID = 111


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", logger)
    return logger


class FakeSession:
    def __init__(self, order: list[str], lock_error: bool) -> None:
        self.order = order
        self.lock_error = lock_error
        self.rollback = AsyncMock()

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None

    async def commit(self) -> None:
        self.order.append("commit")

    async def get(self, model: type, identifier: int, **options: Any) -> object:
        assert options == {"with_for_update": {"nowait": True}}
        if self.lock_error:
            raise DBAPIError("SELECT ... FOR UPDATE NOWAIT", {}, Exception("lock not available"))
        return object()


def make_spec(claim: Any) -> handlers.TapSpec:
    return handlers.TapSpec(
        tapped_event="some_button_tapped",
        refused_event="some_refused",
        keyboard_failure_event="some_keyboard_removal_failed",
        claim=claim,
        expected_status=PostStatus.PUBLISHING,
        dispatch_failed_error="some dispatch error",
        dispatched_answer="Started.",
        dispatched_event="some_task_dispatched",
    )


class Tap:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.order: list[str] = []
        self.lock_error = False
        self.dispatch_result = True
        self.callback = MagicMock()
        self.callback.answer = AsyncMock(side_effect=lambda *a, **k: self.order.append("answer"))
        self.bot = MagicMock()
        self.mark_failure = AsyncMock()
        self.session_factory: Any = lambda: FakeSession(self.order, self.lock_error)
        self.remove_keyboard = AsyncMock(side_effect=lambda *a: self.order.append("keyboard"))
        monkeypatch.setattr(handlers, "mark_dispatch_failure", self.mark_failure)
        monkeypatch.setattr(handlers, "remove_keyboard", self.remove_keyboard)

    async def start_task(self) -> bool:
        self.order.append("dispatch")
        return self.dispatch_result

    async def run(self, claim: Any) -> None:
        await handlers.handle_tap(
            self.callback,
            POST_ID,
            USER_ID,
            self.bot,
            self.session_factory,
            make_spec(claim),
            self.start_task,
        )


@pytest.fixture
def tap(monkeypatch: pytest.MonkeyPatch) -> Tap:
    return Tap(monkeypatch)


async def test_handle_tap_commits_then_removes_the_keyboard_then_dispatches(
    tap: Tap, fake_logger: MagicMock
) -> None:
    claim = MagicMock(return_value=(None, (555, 41)))

    await tap.run(claim)

    assert tap.order == ["commit", "keyboard", "dispatch", "answer"]
    tap.remove_keyboard.assert_awaited_once_with(tap.bot, (555, 41), "some_keyboard_removal_failed")
    tap.callback.answer.assert_awaited_once_with("Started.")
    fake_logger.info.assert_any_call("some_button_tapped", user_id=USER_ID, post_id=POST_ID)
    fake_logger.info.assert_any_call("some_task_dispatched", user_id=USER_ID, post_id=POST_ID)


async def test_handle_tap_keeps_the_keyboard_and_dispatches_nothing_after_a_refusal(
    tap: Tap, fake_logger: MagicMock
) -> None:
    await tap.run(MagicMock(return_value=("Refused.", None)))

    assert tap.order == ["commit", "answer"]
    tap.callback.answer.assert_awaited_once_with("Refused.", show_alert=True)


async def test_handle_tap_refuses_a_locked_post_with_the_busy_sentence(
    tap: Tap, fake_logger: MagicMock
) -> None:
    tap.lock_error = True
    claim = MagicMock()

    await tap.run(claim)

    claim.assert_not_called()
    tap.callback.answer.assert_awaited_once_with(
        handlers.POST_BUSY_ANSWER.format(post_id=POST_ID), show_alert=True
    )
    fake_logger.warning.assert_called_once_with(
        "some_refused", reason="post_locked", user_id=USER_ID, post_id=POST_ID
    )


async def test_handle_tap_fails_the_post_when_the_dispatch_fails(
    tap: Tap, fake_logger: MagicMock
) -> None:
    tap.dispatch_result = False

    await tap.run(MagicMock(return_value=(None, None)))

    tap.mark_failure.assert_awaited_once_with(
        tap.session_factory,
        POST_ID,
        expected_status=PostStatus.PUBLISHING,
        error="some dispatch error",
    )
    tap.callback.answer.assert_awaited_once_with(
        handlers.DISPATCH_FAILED_RETRY_ANSWER.format(post_id=POST_ID), show_alert=True
    )
    fake_logger.warning.assert_called_once_with(
        "some_refused", reason="dispatch_failed", user_id=USER_ID, post_id=POST_ID
    )
