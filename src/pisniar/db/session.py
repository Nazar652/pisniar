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
        # A fresh engine per Celery task already means a fresh connection —
        # pre-ping would just add a round trip that never finds a stale one.
        return create_async_engine(database_url, poolclass=NullPool)
    # `pool_pre_ping=True`: without it, a pooled connection surviving a
    # Postgres restart looks fine to the pool until the first query on it
    # fails. In practice that means the *first* `/readyz` after Postgres
    # comes back up would 503 on an otherwise healthy database, and only the
    # second one would see 200 — a real "restart Postgres, bring it back"
    # scenario, not a hypothetical.
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # `expire_on_commit=False`: callers (e.g. `handlers.handle_diag`) read
    # attributes like `post.id` after the `session_scope()` commit closes the
    # transaction: with the default `True`, that first post-commit attribute
    # access would trigger an implicit refresh — a query against a session
    # whose transaction has already ended.
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
