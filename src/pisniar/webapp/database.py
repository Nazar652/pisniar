"""Read access to Postgres for a request: one session per call, closed when it ends.

A database failure becomes a Ukrainian 503 with the details in the log, never a 401.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.logging import get_logger
from pisniar.webapp.errors import ApiProblem

logger = get_logger(__name__)

DATABASE_UNAVAILABLE_MESSAGE = "База даних зараз недоступна. Спробуйте за хвилину."


async def read_from_database[ResultT](
    request: Request, read: Callable[[AsyncSession], Awaitable[ResultT]]
) -> ResultT:
    """no locks are taken: the session only reads, and the engine's `lock_timeout` still applies."""
    session_factory = cast(
        "async_sessionmaker[AsyncSession] | None", request.app.state.session_factory
    )
    if session_factory is None:
        logger.error("webapp_database_not_configured")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE)

    try:
        async with session_factory() as session:
            return await read(session)
    except (SQLAlchemyError, OSError):
        logger.exception("webapp_database_read_failed")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE) from None
