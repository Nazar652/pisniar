"""Integration test fixtures. Two databases: `pisniar_test` (shared by the integration tests, recreated and
migrated once per session), `pisniar_test_migrations` (disposable, per-test) for `test_migrations.py`'s
destructive cycle — never `pisniar`, which the live `bot`/`worker` containers use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging.config
import os
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.config import get_settings
from pisniar.db.session import build_engine, build_session_factory
from pisniar.resolve.cache import SourceCache

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_NAME = "pisniar_test"


def migrations_test_database_name() -> str:
    # latent (no xdist dep today): a constant name would race under
    # `pytest -n`. "gw0" stands in for "no xdist" when unset.
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return f"pisniar_test_migrations_{worker_id}"


def with_database_name(database_url: str, database_name: str) -> str:
    """via `make_url()`: string splitting would drop the query params."""
    return make_url(database_url).set(database=database_name).render_as_string(hide_password=False)


def to_asyncpg_maintenance_dsn(database_url: str) -> str:
    # asyncpg needs a plain DSN, and CREATE/DROP DATABASE needs some *other* database first — "postgres" always exists.
    maintenance_url = make_url(database_url).set(drivername="postgresql", database="postgres")
    return maintenance_url.render_as_string(hide_password=False)


TEST_LOCK_TIMEOUT_MS = 400


@pytest.fixture(scope="session")
def test_lock_timeout_ms() -> int:
    """the `lock_timeout` of the engines whose tests wait for a lock on purpose; production keeps 5000."""
    return TEST_LOCK_TIMEOUT_MS


@pytest.fixture(scope="session")
def database_host_names() -> frozenset[str]:
    """the database host and its addresses, resolved once before any guard patches the lookup."""
    host = make_url(get_settings().database_url).host
    names: set[str] = set()
    if host:
        names.add(host)
        with contextlib.suppress(OSError):
            names.update(str(entry[4][0]) for entry in socket.getaddrinfo(host, None))
    return frozenset(names)


@pytest.fixture
def outside_access_allowed_hosts(
    outside_access_allowed_hosts: frozenset[str],
    database_host_names: frozenset[str],
) -> frozenset[str]:
    """loopback and the database, nothing else."""
    return outside_access_allowed_hosts | database_host_names


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_database_url() -> str:
    settings = get_settings()
    target_url = with_database_name(settings.database_url, TEST_DATABASE_NAME)

    connection = await asyncpg.connect(to_asyncpg_maintenance_dsn(settings.database_url))
    try:
        # recreated every session: committed rows of earlier runs would break a migration
        # that backfills. The name is the module constant, never the application database.
        await connection.execute(f"DROP DATABASE IF EXISTS {TEST_DATABASE_NAME} WITH (FORCE)")
        await connection.execute(f"CREATE DATABASE {TEST_DATABASE_NAME}")
    finally:
        await connection.close()

    return target_url


@pytest_asyncio.fixture
async def migrations_test_database_url() -> AsyncIterator[str]:
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
def run_alembic() -> Callable[..., Awaitable[None]]:
    """runs an `alembic.command` function in a worker thread, because `env.py` calls `asyncio.run`; a failure raises."""

    async def run(alembic_command: Callable[..., None], *arguments: str, database_url: str) -> None:
        def execute() -> None:
            # fileConfig would disable pytest's loggers; `prepend_sys_path = .` is undone with the copy of `sys.path`.
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("DATABASE_URL", database_url)
                patch.setattr(logging.config, "fileConfig", lambda *args, **kwargs: None)
                patch.setattr(sys, "path", list(sys.path))
                alembic_command(Config(str(REPOSITORY_ROOT / "alembic.ini")), *arguments)

        await asyncio.to_thread(execute)

    return run


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated_test_database_url(test_database_url: str, run_alembic: Callable[..., Awaitable[None]]) -> str:
    await run_alembic(command.upgrade, "head", database_url=test_database_url)
    return test_database_url


@pytest_asyncio.fixture
async def db_session(migrated_test_database_url: str) -> AsyncIterator[AsyncSession]:
    """one session per test, rolled back after — even on an expected
    `IntegrityError`, which only aborts the statement."""
    engine = build_engine(migrated_test_database_url)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def source_cache(migrated_test_database_url: str) -> AsyncIterator[SourceCache]:
    """a real `SourceCache` on the test database — it opens and commits its
    own sessions, so it deliberately shares nothing with `db_session`."""
    engine = build_engine(migrated_test_database_url)
    yield SourceCache(session_factory=build_session_factory(engine))
    await engine.dispose()
