"""`warn_if_alerts_share_the_channel`: a warning, never a refusal. The two
ids are deliberately equal while the channel under test is the alert chat;
on the real channel the same equality publishes alert text to subscribers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pisniar import channel
from pisniar.channel import CHANNEL_LINK_URL, CHANNEL_USERNAME, warn_if_alerts_share_the_channel


@pytest.fixture
def channel_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """not `capture_logs()`: `worker.tasks` calls this at import time, and the cached
    structlog proxy then captures nothing for the rest of the session."""
    fake_logger = MagicMock()
    monkeypatch.setattr(channel, "logger", fake_logger)
    return fake_logger


def test_a_shared_chat_warns_and_reports_it(channel_logger: MagicMock) -> None:
    shared = warn_if_alerts_share_the_channel(-1001234567890, -1001234567890)

    assert shared is True
    assert channel_logger.warning.call_args.args[0] == "alert_chat_is_the_publish_channel"


def test_separate_chats_are_silent(channel_logger: MagicMock) -> None:
    shared = warn_if_alerts_share_the_channel(-1001234567890, 111111111)

    assert shared is False
    channel_logger.warning.assert_not_called()


def test_the_caption_username_is_derived_from_the_caption_link() -> None:
    """one literal, not two: `CHANNEL_USERNAME` is what `telegram.publish`
    compares `get_chat()` against, and a second literal could drift."""
    assert CHANNEL_LINK_URL.endswith(f"/{CHANNEL_USERNAME}")
