"""FastAPI app: `/healthz` (liveness) and `/readyz` (readiness).

`/healthz` never touches Postgres/Redis, so it isn't killed by its own probe.
`/readyz` checks both, returns 503 without leaking DSN/host/exception details.
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio
from fastapi import FastAPI, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.logging import get_logger

logger = get_logger(__name__)


def create_app(*, session_factory: async_sessionmaker[AsyncSession], redis_url: str) -> FastAPI:
    app = FastAPI(title="pisniar", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        postgres_available = await check_postgres(session_factory)
        redis_available = await check_redis(redis_url)
        if postgres_available and redis_available:
            return {"status": "ok"}
        response.status_code = 503
        return {"status": "unavailable"}

    return app


async def check_postgres(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readyz_postgres_unavailable", exc_info=True)
        return False
    return True


async def check_redis(redis_url: str) -> bool:
    client = redis_asyncio.Redis.from_url(redis_url)
    try:
        await client.ping()
    except Exception:
        logger.warning("readyz_redis_unavailable", exc_info=True)
        return False
    finally:
        await client.aclose()
    return True
