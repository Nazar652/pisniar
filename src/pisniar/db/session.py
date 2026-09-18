"""Async engine / session construction.

`bot` and `api` share one long-lived engine with the default pool, created
once at process startup. `worker` (Celery) is different on purpose: each task
runs `asyncio.run()` in its own fresh event loop (see `pisniar.worker.runner`),
and asyncpg connections cannot cross event loops — so worker tasks build a
`NullPool` engine per task call and dispose of it when done. The one-connection-
per-task cost is accepted; if it ever needs to change, this is the only file
that has to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


def build_engine(database_url: str, *, use_null_pool: bool = False) -> AsyncEngine:
    if use_null_pool:
        return create_async_engine(database_url, poolclass=NullPool)
    return create_async_engine(database_url)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work: commit on success, rollback on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
