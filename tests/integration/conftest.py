"""Integration test fixtures: a dedicated `pisniar_test` database.

Deliberately not the `pisniar` database the live `bot`/`worker` containers
in the same compose stack are using — these tests run destructive
migrations (`downgrade base`) that would otherwise pull the schema out from
under the running app.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.config import get_settings
from pisniar.db.session import build_engine, build_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_NAME = "pisniar_test"


def to_test_database_url(database_url: str) -> str:
    base, _, _current_name = database_url.rpartition("/")
    return f"{base}/{TEST_DATABASE_NAME}"


def to_asyncpg_maintenance_dsn(database_url: str) -> str:
    # asyncpg does not understand SQLAlchemy's "+asyncpg" driver suffix, and
    # a `CREATE DATABASE` needs a connection to some *other* existing
    # database first — "postgres" always exists on a fresh server.
    plain = database_url.replace("postgresql+asyncpg://", "postgresql://")
    base, _, _current_name = plain.rpartition("/")
    return f"{base}/postgres"


@pytest_asyncio.fixture(scope="session")
async def test_database_url() -> str:
    settings = get_settings()
    target_url = to_test_database_url(settings.database_url)

    connection = await asyncpg.connect(to_asyncpg_maintenance_dsn(settings.database_url))
    try:
        already_exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DATABASE_NAME
        )
        if not already_exists:
            await connection.execute(f"CREATE DATABASE {TEST_DATABASE_NAME}")
    finally:
        await connection.close()

    return target_url


@pytest.fixture(scope="session")
def run_alembic() -> Callable[..., subprocess.CompletedProcess[str]]:
    """A thin wrapper around the real `alembic` CLI — the same one V4/V11
    invoke — pointed at `database_url` via the `DATABASE_URL` env var, which
    `alembic/env.py` already knows how to read."""

    def run(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["alembic", *args],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
            check=False,
        )

    return run


@pytest.fixture(scope="session")
def migrated_test_database_url(
    test_database_url: str, run_alembic: Callable[..., subprocess.CompletedProcess[str]]
) -> str:
    result = run_alembic("upgrade", "head", database_url=test_database_url)
    assert result.returncode == 0, result.stdout + result.stderr
    return test_database_url


@pytest_asyncio.fixture
async def db_session(migrated_test_database_url: str) -> AsyncIterator[AsyncSession]:
    """One session per test, rolled back at the end regardless of outcome —
    including when the test's own assertion is that an operation raised
    `IntegrityError`, since that only aborts the statement, not the fixture's
    cleanup."""
    engine = build_engine(migrated_test_database_url)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()
