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

# Contention on a post/track row only ever means another resolve of the same
# row; failing fast beats holding a worker slot or a bot handler.
LOCK_TIMEOUT_MS = 5000


def build_engine(
    database_url: str, *, use_null_pool: bool = False, lock_timeout_ms: int | None = None
) -> AsyncEngine:
    # A connection-level GUC, not `SET LOCAL`: it must survive the rollback
    # a failed commit performs, which is exactly when locks matter.
    connect_args = (
        {"server_settings": {"lock_timeout": str(lock_timeout_ms)}}
        if lock_timeout_ms is not None
        else {}
    )

    if use_null_pool:
        # Fresh engine per task already means a fresh connection.
        return create_async_engine(database_url, poolclass=NullPool, connect_args=connect_args)
    # pool_pre_ping: without it, the first /readyz after a Postgres
    # restart 503s on a pooled-but-dead connection; only the second is 200.
    return create_async_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: callers read e.g. post.id after
    # session_scope()'s commit closes the transaction.
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
