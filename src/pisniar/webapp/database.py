"""Read access to Postgres for a request: one session per call, closed when it ends.

A database failure becomes a Ukrainian 503 with the details in the log, never a 401.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import Request
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.logging import get_logger
from pisniar.webapp.errors import ApiProblem

logger = get_logger(__name__)

DATABASE_UNAVAILABLE_MESSAGE = "База даних зараз недоступна. Спробуйте за хвилину."
POST_BUSY_MESSAGE = "Пост зараз змінюється іншою дією. Спробуйте за кілька секунд."

# `lock_not_available` (NOWAIT refused or `lock_timeout` hit) and `query_canceled` (`statement_timeout`, a cancel)
LOCK_FAILURE_SQLSTATES = frozenset({"55P03", "57014"})


async def read_from_database[ResultT](request: Request, read: Callable[[AsyncSession], Awaitable[ResultT]]) -> ResultT:
    """no locks are taken: the session only reads, and the engine's `lock_timeout` still applies."""
    session_factory = cast("async_sessionmaker[AsyncSession] | None", request.app.state.session_factory)
    if session_factory is None:
        logger.error("webapp_database_not_configured")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE)

    try:
        async with session_factory() as session:
            return await read(session)
    except (SQLAlchemyError, OSError):
        logger.exception("webapp_database_read_failed")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE) from None


def is_lock_failure(error: DBAPIError) -> bool:
    return getattr(error.orig, "sqlstate", None) in LOCK_FAILURE_SQLSTATES


async def write_to_database[ResultT](request: Request, write: Callable[[AsyncSession], Awaitable[ResultT]]) -> ResultT:
    """one transaction: committed when `write` returns, rolled back when it raises. A refused or
    timed-out lock is a 409 the admin can retry; every other database failure is a 503."""
    session_factory = cast("async_sessionmaker[AsyncSession] | None", request.app.state.session_factory)
    if session_factory is None:
        logger.error("webapp_database_not_configured")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE)

    try:
        async with session_factory() as session:
            result = await write(session)
            await session.commit()
            return result
    except DBAPIError as error:
        if is_lock_failure(error):
            logger.warning("webapp_database_lock_refused", exc_info=True)
            raise ApiProblem(409, "post_busy", POST_BUSY_MESSAGE) from None
        logger.exception("webapp_database_write_failed")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE) from None
    except (SQLAlchemyError, OSError):
        logger.exception("webapp_database_write_failed")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE) from None
