"""The Mini App server and Sentry: the CSP and the one meta tag follow the frontend DSN, and a real request failing
through the real routes leaves none of its credentials in the event.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from support import ADMIN_ID, BOT_TOKEN, NOW, make_init_data
from test_webapp_app import EXPECTED_CSP, PUBLIC_ROUTES, route_table

import pisniar.logging as pisniar_logging
import pisniar.sentry as pisniar_sentry
from pisniar.config import Settings, redaction_secrets
from pisniar.logging import configure_logging
from pisniar.sentry import FILTERED, init_sentry
from pisniar.webapp import main as webapp_main
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import MediaClaims, issue_media_token

FRONTEND_DSN = "https://publickey@o1.ingest.sentry.io/2"
INDEX_HTML = "<!doctype html><html><head><title>x</title></head><body></body></html>"
BEARER_SENTINEL = "BEARER-sentinel-value"
START_SENTINEL = "START-sentinel-value"


@pytest.fixture
def served_directory(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    return tmp_path


def client_with(settings: Settings, directory: Path, **overrides: Any) -> TestClient:
    configured = settings.model_copy(update={"webapp_static_dir": directory, **overrides})
    return TestClient(create_webapp(configured))


def test_without_a_frontend_dsn_the_page_and_the_csp_are_untouched(settings: Settings, served_directory: Path) -> None:
    response = client_with(settings, served_directory).get("/")

    assert response.text == INDEX_HTML
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP


def test_a_valid_frontend_dsn_adds_exactly_its_ingest_host_to_connect_src(
    settings: Settings, served_directory: Path
) -> None:
    response = client_with(settings, served_directory, sentry_frontend_dsn=SecretStr(FRONTEND_DSN)).get("/")

    expected = EXPECTED_CSP.replace("connect-src 'self';", "connect-src 'self' https://o1.ingest.sentry.io;")
    assert response.headers["Content-Security-Policy"] == expected
    assert "unsafe-" not in expected
    assert "publickey" not in expected


def test_the_page_carries_one_meta_tag_with_the_dsn_environment_and_release(
    settings: Settings, served_directory: Path
) -> None:
    response = client_with(
        settings,
        served_directory,
        sentry_frontend_dsn=SecretStr(FRONTEND_DSN),
        sentry_environment="production",
        sentry_release="abc123",
    ).get("/")

    tag = f'<meta name="sentry-dsn" content="{FRONTEND_DSN}" data-environment="production" data-release="abc123">'
    assert response.text == INDEX_HTML.replace("</head>", f"{tag}</head>")
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"


def test_the_tag_omits_the_release_when_there_is_none(settings: Settings, served_directory: Path) -> None:
    response = client_with(settings, served_directory, sentry_frontend_dsn=SecretStr(FRONTEND_DSN)).get("/")

    assert "data-release" not in response.text
    assert 'data-environment="dev"' in response.text


def test_a_value_cannot_leave_its_attribute(settings: Settings, served_directory: Path) -> None:
    response = client_with(
        settings,
        served_directory,
        sentry_frontend_dsn=SecretStr('https://k"><script>x@o1.ingest.sentry.io/2'),
        sentry_release='x"><script>',
    ).get("/")

    assert "<script>" not in response.text
    assert response.text.count('<meta name="sentry-dsn"') == 1
    assert "&quot;&gt;&lt;script&gt;" in response.text


def test_an_invalid_frontend_dsn_leaves_the_page_and_the_csp_as_they_were(
    settings: Settings, served_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(pisniar_sentry, "logger", fake_logger)

    response = client_with(settings, served_directory, sentry_frontend_dsn=SecretStr("http://key@host/1")).get("/")

    assert response.text == INDEX_HTML
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    fake_logger.error.assert_called_once_with("sentry_frontend_dsn_invalid")


def test_a_frontend_dsn_adds_no_route_and_no_public_one(settings: Settings, served_directory: Path) -> None:
    configured = settings.model_copy(
        update={"webapp_static_dir": served_directory, "sentry_frontend_dsn": SecretStr(FRONTEND_DSN)}
    )
    app = create_webapp(configured)

    routes = {entry[:2] for entry in route_table(app)}
    assert routes & PUBLIC_ROUTES == PUBLIC_ROUTES
    assert routes == {entry[:2] for entry in route_table(create_webapp(settings))}
    assert all(isinstance(route, APIRoute) for route in app.routes)


def test_the_index_is_still_503_before_the_frontend_is_built_with_a_dsn(settings: Settings, tmp_path: Path) -> None:
    response = client_with(settings, tmp_path, sentry_frontend_dsn=SecretStr(FRONTEND_DSN)).get("/")

    assert response.status_code == 503
    assert FRONTEND_DSN not in response.text


@pytest.fixture
def reporting(
    settings: Settings,
    sent_sentry_events: list[Any],
    preserve_root_logging_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    monkeypatch.setattr(pisniar_logging, "registered_runtime_secrets", {})
    # a route logger first used under this configuration would stay bound to it for later tests
    monkeypatch.setattr("pisniar.webapp.api.logger", MagicMock())
    monkeypatch.setattr("pisniar.webapp.audio.logger", MagicMock())
    configured = settings.model_copy(
        update={
            "sentry_dsn": SecretStr("https://backendkey@o1.ingest.sentry.io/1"),
            "database_url": "postgresql+asyncpg://user:SENTINEL-database-password@postgres:5432/db",
            "spotify_client_secret": SecretStr("SENTINEL-spotify-client-secret"),
        }
    )
    configure_logging(log_format="json", log_level="INFO", secrets=redaction_secrets(configured))
    init_sentry(configured)
    return sent_sentry_events


def frames_of_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    return event["exception"]["values"][-1]["stacktrace"]["frames"]


def frame_of_module(event: dict[str, Any], module: str) -> dict[str, Any]:
    return next(frame for frame in frames_of_event(event) if frame.get("module") == module)


def log_like_uvicorn(error: BaseException) -> None:
    logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=error)


def test_a_failing_media_request_leaves_its_token_and_header_out_of_the_event(
    settings: Settings, reporting: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claims = MediaClaims(user_id=ADMIN_ID, post_id=7, track_id=3, video_id="n4bPzdIs_nc")
    media_token = issue_media_token(claims, BOT_TOKEN, NOW, 900)

    def unreadable_volume(*arguments: object) -> None:
        raise RuntimeError("the media volume is unreadable")

    monkeypatch.setattr("pisniar.webapp.audio.locate_audio_file", unreadable_volume)
    app = create_webapp(settings.model_copy(update={"media_root": tmp_path}), clock=lambda: float(NOW))
    client = TestClient(app, raise_server_exceptions=True)

    with pytest.raises(RuntimeError) as raised:
        client.get(f"/api/posts/7/audio?token={media_token}", headers={"Authorization": f"Bearer {BEARER_SENTINEL}"})
    log_like_uvicorn(raised.value)

    (event,) = reporting
    serialized = json.dumps(event, default=str)
    for signed_part in media_token.split("."):
        assert signed_part not in serialized
    assert BEARER_SENTINEL not in serialized
    own_frame = frame_of_module(event, "pisniar.webapp.audio")
    assert own_frame["vars"]["post_id"] == "7"
    assert own_frame["vars"]["token"] == FILTERED
    assert own_frame["vars"]["request"] == FILTERED
    assert own_frame["vars"]["context"] == FILTERED
    assert [frame for frame in frames_of_event(event) if not frame["module"].startswith("pisniar.")]
    assert all("vars" not in frame for frame in frames_of_event(event) if not frame["module"].startswith("pisniar."))


def test_a_failing_sign_in_leaves_the_init_data_out_of_the_event(
    settings: Settings, reporting: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    init_data = make_init_data(extra={"start_param": START_SENTINEL})

    def broken_validation(*arguments: object) -> None:
        raise RuntimeError("validation crashed")

    monkeypatch.setattr("pisniar.webapp.api.validate_init_data", broken_validation)
    client = TestClient(create_webapp(settings, clock=lambda: float(NOW)), raise_server_exceptions=True)

    with pytest.raises(RuntimeError) as raised:
        client.post("/api/session", json={"init_data": init_data})
    log_like_uvicorn(raised.value)

    (event,) = reporting
    serialized = json.dumps(event, default=str)
    hash_value = next(part.split("=")[1] for part in init_data.split("&") if part.startswith("hash="))
    assert hash_value not in serialized
    assert START_SENTINEL not in serialized
    assert BOT_TOKEN not in serialized
    own_frame = frame_of_module(event, "pisniar.webapp.api")
    assert own_frame["vars"]["context"] == FILTERED
    assert own_frame["vars"]["request"] == FILTERED
    assert FILTERED in own_frame["vars"]["body"]


async def test_error_reporting_starts_right_after_logging_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    fake_bot = MagicMock()
    fake_bot.session.close = AsyncMock()
    fake_http_client = MagicMock()
    fake_http_client.aclose = AsyncMock()
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    fake_server = MagicMock()
    fake_server.serve = AsyncMock()
    monkeypatch.setattr(webapp_main, "configure_logging", lambda **keywords: order.append("logging"))
    monkeypatch.setattr(webapp_main, "init_sentry", lambda settings: order.append("sentry"))
    monkeypatch.setattr(webapp_main, "install_access_log_filter", lambda: order.append("access_log"))
    monkeypatch.setattr(webapp_main, "build_engine", MagicMock(return_value=fake_engine))
    monkeypatch.setattr(webapp_main, "build_session_factory", MagicMock())
    monkeypatch.setattr(webapp_main, "build_bot", MagicMock(return_value=fake_bot))
    monkeypatch.setattr(webapp_main, "build_celery_app", MagicMock())
    monkeypatch.setattr(webapp_main, "build_http_client", MagicMock(return_value=fake_http_client))
    monkeypatch.setattr(webapp_main, "create_webapp", MagicMock())
    monkeypatch.setattr(webapp_main.uvicorn, "Config", MagicMock())
    monkeypatch.setattr(webapp_main.uvicorn, "Server", MagicMock(return_value=fake_server))

    await webapp_main.run()

    assert order[:3] == ["logging", "sentry", "access_log"]
