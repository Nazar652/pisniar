"""Async engine / session construction. `bot`/`api` share one long-lived
engine; `worker` builds a fresh `NullPool` engine per Celery task, since
asyncpg connections can't cross the fresh event loop each task runs in.
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
        # Fresh engine per task already means a fresh connection.
        return create_async_engine(database_url, poolclass=NullPool)
    # pool_pre_ping: without it, the first /readyz after a Postgres
    # restart 503s on a pooled-but-dead connection; only the second is 200.
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: callers read e.g. post.id after
    # session_scope()'s commit closes the transaction.
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
