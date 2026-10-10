"""The health and readiness API: `/healthz` never touches a dependency, `/readyz`
answers 503 when Postgres or Redis is down, and the 503 body leaks nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from pisniar.api import app as api_app

DSN_FRAGMENT = "postgresql+asyncpg://secret-user:secret-password@db-host:5432/pisniar"
REDIS_URL = "redis://:secret-redis-password@redis-host:6379/0"


class FakeSession:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.statements: list[object] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)
        if self.error is not None:
            raise self.error


def make_session_factory(error: Exception | None = None) -> MagicMock:
    session = FakeSession(error)
    factory = MagicMock(return_value=session)
    factory.session = session
    return factory


def patch_redis(monkeypatch: pytest.MonkeyPatch, ping_error: Exception | None = None) -> MagicMock:
    client = MagicMock()
    client.ping = AsyncMock(side_effect=ping_error)
    client.aclose = AsyncMock()
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(api_app.redis_asyncio.Redis, "from_url", constructor)
    return client


def test_healthz_answers_ok_without_touching_postgres_or_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = make_session_factory()
    redis_client = patch_redis(monkeypatch)
    client = TestClient(api_app.create_app(session_factory=session_factory, redis_url=REDIS_URL))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    session_factory.assert_not_called()
    redis_client.ping.assert_not_awaited()


def test_readyz_answers_ok_when_both_dependencies_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = make_session_factory()
    redis_client = patch_redis(monkeypatch)
    client = TestClient(api_app.create_app(session_factory=session_factory, redis_url=REDIS_URL))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(session_factory.session.statements) == 1
    redis_client.ping.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()


def test_readyz_is_503_when_postgres_is_down_and_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = make_session_factory(ConnectionError(f"cannot connect to {DSN_FRAGMENT}"))
    patch_redis(monkeypatch)
    client = TestClient(api_app.create_app(session_factory=session_factory, redis_url=REDIS_URL))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    for secret in ("secret-user", "secret-password", "db-host", "cannot connect"):
        assert secret not in response.text


def test_readyz_is_503_when_redis_is_down_and_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = make_session_factory()
    redis_client = patch_redis(monkeypatch, ConnectionError(f"cannot reach {REDIS_URL}"))
    client = TestClient(api_app.create_app(session_factory=session_factory, redis_url=REDIS_URL))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    for secret in ("secret-redis-password", "redis-host", "cannot reach"):
        assert secret not in response.text
    redis_client.aclose.assert_awaited_once()


def test_both_checks_run_even_when_postgres_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """the Redis client is always closed, so one dead dependency leaks no connection."""
    session_factory = make_session_factory(ConnectionError("down"))
    redis_client = patch_redis(monkeypatch)
    client = TestClient(api_app.create_app(session_factory=session_factory, redis_url=REDIS_URL))

    client.get("/readyz")

    redis_client.ping.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()


def test_the_api_exposes_no_docs_or_schema() -> None:
    client = TestClient(api_app.create_app(session_factory=make_session_factory(), redis_url=REDIS_URL))

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
