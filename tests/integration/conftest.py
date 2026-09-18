"""Integration test fixtures.

Two separate databases, on purpose:

- `pisniar_test` (`test_database_url`/`migrated_test_database_url`/
  `db_session`): shared across the constraint tests in `test_constraints.py`,
  migrated once per session and never torn down mid-run.
- `pisniar_test_migrations` (`migrations_test_database_url`): a disposable,
  per-test database dedicated to `test_migrations.py`'s destructive
  `upgrade`/`downgrade`/`upgrade` cycle and its own `alembic check`. If a
  mid-cycle step failed (downgrade succeeds, the re-upgrade doesn't) on the
  *shared* database, `pisniar_test`'s session-scoped fixture would already be
  cached and every other integration test in the same run would then fail
  with a cascading `UndefinedTable`, for a reason that has nothing to do with
  what each of those tests actually checks.

Neither database is `pisniar`, the one the live `bot`/`worker` containers in
the same compose stack use — these tests must never be able to pull the
schema out from under a running app.
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
    # `pytest-xdist` is not a dependency today, so this is latent, not
    # active — but a constant name would mean two workers under `pytest -n`
    # racing to create/drop the same database out from under each other.
    # `PYTEST_XDIST_WORKER` (e.g. "gw0") is set by xdist itself; "gw0" is
    # this fixture's own stand-in for "no xdist, act like worker 0".
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return f"pisniar_test_migrations_{worker_id}"


def with_database_name(database_url: str, database_name: str) -> str:
    """`database_url` with its database name replaced by `database_name`.

    Uses SQLAlchemy's own `make_url()`/`URL.set()` rather than manual string
    splitting (e.g. `rpartition("/")`), which would silently drop any query
    parameters a DSN might carry (`?sslmode=...`, etc.).
    """
    return make_url(database_url).set(database=database_name).render_as_string(hide_password=False)


def to_asyncpg_maintenance_dsn(database_url: str) -> str:
    # asyncpg does not understand SQLAlchemy's "+asyncpg" driver suffix, and
    # a `CREATE`/`DROP DATABASE` needs a connection to some *other* existing
    # database first — "postgres" always exists on a fresh server.
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
    after — see the module docstring for why `test_migrations.py` gets its
    own instead of sharing `pisniar_test`.
    """
    settings = get_settings()
    maintenance_dsn = to_asyncpg_maintenance_dsn(settings.database_url)
    database_name = migrations_test_database_name()

    connection = await asyncpg.connect(maintenance_dsn)
    try:
        # `IF EXISTS ... WITH (FORCE)` first: a previous run that crashed
        # mid-test could have left this database behind with the prior
        # test's connections still attached.
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
