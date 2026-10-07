"""Fixtures of the Mini App tests: a fixed clock and a client over the real application."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support import ADMIN_ID, BOT_TOKEN, NOW

from pisniar.config import Settings
from pisniar.webapp.app import create_webapp


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        admin_ids=str(ADMIN_ID),
        alert_chat_id=1,
        channel_id=-1,
        database_url="postgresql+asyncpg://u:p@postgres:5432/db",
        spotify_client_id="id",
        spotify_client_secret="secret",
        webapp_static_dir=Path("/static-for-tests"),
    )


@pytest.fixture
def clock() -> Callable[[], float]:
    return lambda: float(NOW)


@pytest.fixture
def webapp(settings: Settings, clock: Callable[[], float]) -> FastAPI:
    return create_webapp(settings, clock=clock)


@pytest.fixture
def webapp_routes(webapp: FastAPI) -> list[str]:
    return [route.path for route in webapp.routes if isinstance(route, APIRoute)]


@pytest.fixture
def client_for(settings: Settings, clock: Callable[[], float]) -> Callable[..., TestClient]:
    """Builds a client; `model_copy` skips the absolute-path validator, which rejects a
    Windows temp directory."""

    def build(static_dir: Path | None = None) -> TestClient:
        configured = settings
        if static_dir is not None:
            configured = settings.model_copy(update={"webapp_static_dir": static_dir})
        return TestClient(create_webapp(configured, clock=clock))

    return build


@pytest.fixture
def client(client_for: Callable[..., TestClient]) -> TestClient:
    return client_for()
