"""Integration test fixtures. Two databases: `pisniar_test` (shared, migrated
once per session) for `test_constraints.py`, `pisniar_test_migrations`
(disposable, per-test) for `test_migrations.py`'s destructive cycle — never
`pisniar`, which the live `bot`/`worker` containers use.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.config import get_settings
from pisniar.db.session import build_engine, build_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_NAME = "pisniar_test"


def migrations_test_database_name() -> str:
    # Latent (no xdist dep today): a constant name would race under
    # `pytest -n`. "gw0" stands in for "no xdist" when unset.
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return f"pisniar_test_migrations_{worker_id}"


def with_database_name(database_url: str, database_name: str) -> str:
    """`database_url` with its database swapped, via `make_url()` (not
    string splitting, which would drop query params)."""
    return make_url(database_url).set(database=database_name).render_as_string(hide_password=False)


def to_asyncpg_maintenance_dsn(database_url: str) -> str:
    # asyncpg needs a plain DSN, and CREATE/DROP DATABASE needs some
    # *other* database first — "postgres" always exists.
    maintenance_url = make_url(database_url).set(drivername="postgresql", database="postgres")
    return maintenance_url.render_as_string(hide_password=False)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_database_url() -> str:
    settings = get_settings()
    target_url = with_database_name(settings.database_url, TEST_DATABASE_NAME)

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


@pytest_asyncio.fixture
async def migrations_test_database_url() -> AsyncIterator[str]:
    """A disposable database, created before the test and force-dropped
    after — see module docstring."""
    settings = get_settings()
    maintenance_dsn = to_asyncpg_maintenance_dsn(settings.database_url)
    database_name = migrations_test_database_name()

    connection = await asyncpg.connect(maintenance_dsn)
    try:
        # IF EXISTS...FORCE first: a crashed prior run may have left this
        # database behind with its connections still attached.
        await connection.execute(f"DROP DATABASE IF EXISTS {database_name} WITH (FORCE)")
        await connection.execute(f"CREATE DATABASE {database_name}")
    finally:
        await connection.close()

    try:
        yield with_database_name(settings.database_url, database_name)
    finally:
        connection = await asyncpg.connect(maintenance_dsn)
        try:
            await connection.execute(f"DROP DATABASE IF EXISTS {database_name} WITH (FORCE)")
        finally:
            await connection.close()


@pytest.fixture(scope="session")
def run_alembic() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Wraps the real `alembic` CLI (same one V4/V11 use), via `DATABASE_URL`."""

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
    """One session per test, rolled back after — even on an expected
    `IntegrityError`, which only aborts the statement."""
    engine = build_engine(migrated_test_database_url)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()
