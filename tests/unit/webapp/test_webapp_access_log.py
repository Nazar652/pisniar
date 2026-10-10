"""The access-log filter: a unit check of the record rewrite, and a real uvicorn server whose
captured logs must hold no audio token once the filter is installed.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from support import ADMIN_ID, BOT_TOKEN, NOW

from pisniar.config import Settings
from pisniar.webapp.access_log import (
    ACCESS_LOGGER_NAME,
    QueryStrippingFilter,
    install_access_log_filter,
)
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import MediaClaims, issue_media_token

VIDEO_ID = "n4bPzdIs_nc"


def access_record(*arguments: object) -> logging.LogRecord:
    return logging.LogRecord(ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', arguments, None)


@pytest.fixture
def clean_access_logger() -> Iterator[logging.Logger]:
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    before = list(access_logger.filters)
    yield access_logger
    access_logger.filters[:] = before


def test_the_filter_replaces_the_target_with_its_path() -> None:
    record = access_record("1.2.3.4:5", "GET", "/api/posts/7/audio?token=SECRET", "1.1", 206)

    assert QueryStrippingFilter().filter(record) is True
    assert record.getMessage() == '1.2.3.4:5 - "GET /api/posts/7/audio HTTP/1.1" 206'


def test_the_filter_leaves_a_target_without_a_query_alone() -> None:
    record = access_record("1.2.3.4:5", "GET", "/assets/app.js", "1.1", 200)

    QueryStrippingFilter().filter(record)

    assert record.getMessage() == '1.2.3.4:5 - "GET /assets/app.js HTTP/1.1" 200'


def test_the_filter_does_not_touch_a_record_of_another_shape() -> None:
    record = access_record("only", "three", "?x=1")

    assert QueryStrippingFilter().filter(record) is True
    assert record.args == ("only", "three", "?x=1")


def test_install_adds_the_filter_once(clean_access_logger: logging.Logger) -> None:
    install_access_log_filter()
    install_access_log_filter()

    installed = [item for item in clean_access_logger.filters if isinstance(item, QueryStrippingFilter)]
    assert len(installed) == 1


@pytest.fixture
def live_server(
    settings: Settings,
    clock: Callable[[], float],
    tmp_path: Path,
) -> Iterator[str]:
    audio = tmp_path / "audio" / "3" / f"{VIDEO_ID}.webm"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"x" * 64)
    app = create_webapp(settings.model_copy(update={"media_root": tmp_path}), clock=clock)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    yield f"http://127.0.0.1:{listener.getsockname()[1]}"

    server.should_exit = True
    thread.join(timeout=10)
    listener.close()


def wait_for_access_line(caplog: pytest.LogCaptureFixture) -> None:
    """the server logs from its own thread, possibly after the client has its response."""
    deadline = time.monotonic() + 5
    while "/api/posts/7/audio" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.02)


def test_a_real_request_with_a_token_leaves_the_token_in_no_log_once_the_filter_is_installed(
    live_server: str,
    caplog: pytest.LogCaptureFixture,
    clean_access_logger: logging.Logger,
) -> None:
    claims = MediaClaims(user_id=ADMIN_ID, post_id=7, track_id=3, video_id=VIDEO_ID)
    token = issue_media_token(claims, BOT_TOKEN, NOW, 900)
    url = f"{live_server}/api/posts/7/audio?token={token}"
    # the client's own request log is not the server's; the last call sets the handler level
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.INFO)

    httpx.get(url, headers={"Range": "bytes=0-9"})
    wait_for_access_line(caplog)
    assert token in caplog.text, "without the filter the token is in the access log"

    caplog.clear()
    install_access_log_filter()
    response = httpx.get(url, headers={"Range": "bytes=0-9"})
    wait_for_access_line(caplog)

    assert response.status_code == 206
    assert "/api/posts/7/audio" in caplog.text
    assert token not in caplog.text
    assert "token=" not in caplog.text
