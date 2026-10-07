"""`handle_link`'s three early returns answer the admin and stop. Each one
logs, or a message that arrived, was rejected and was answered would leave
no trace at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.bot import handlers
from pisniar.resolve.urls import MAX_NORMALIZED_URL_BYTES

ALBUM_URL = "https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j"
OVER_LONG_URL = "https://open.spotify.com/track/" + "a" * MAX_NORMALIZED_URL_BYTES
UNRECOGNIZED_TEXT = "hello there, not a link"


async def capture_rejection_fields(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """the module logger is replaced rather than captured through structlog:
    `capture_logs()` is defeated by an already-cached bound logger."""
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)

    message = AsyncMock()
    message.text = text
    message.from_user = SimpleNamespace(id=111)
    session_factory = MagicMock()
    celery_client = MagicMock()

    await handlers.handle_link(message, session_factory, celery_client)

    message.answer.assert_awaited_once()
    session_factory.assert_not_called()
    celery_client.send_task.assert_not_called()
    fake_logger.warning.assert_not_called()
    fake_logger.error.assert_not_called()
    return [
        log_call.kwargs
        for log_call in fake_logger.info.call_args_list
        if log_call.args[:1] == ("link_rejected",)
    ]


async def test_an_over_long_url_is_logged_with_its_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = await capture_rejection_fields(OVER_LONG_URL, monkeypatch)

    assert len(fields) == 1
    assert fields[0]["reason"] == "too_long"
    assert fields[0]["raw_url_bytes"] == len(OVER_LONG_URL.encode("utf-8"))
    assert fields[0]["limit_bytes"] == MAX_NORMALIZED_URL_BYTES


async def test_a_non_track_url_is_logged_with_its_kind_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = await capture_rejection_fields(ALBUM_URL, monkeypatch)

    assert len(fields) == 1
    assert fields[0]["reason"] == "not_track"
    assert fields[0]["not_track_reason"] == "album"
    assert "4yYXqedvSKuJlgI2IWsB0j" in fields[0]["normalized_url"]


async def test_unrecognized_text_is_logged_by_length_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = await capture_rejection_fields(UNRECOGNIZED_TEXT, monkeypatch)

    assert len(fields) == 1
    assert fields[0]["reason"] == "unknown"
    assert fields[0]["raw_text_length"] == len(UNRECOGNIZED_TEXT)
    # the admin's message body is not a URL and must not reach the logs.
    assert not any(UNRECOGNIZED_TEXT in str(value) for value in fields[0].values())
