"""FastAPI app: `/healthz` (liveness) and `/readyz` (readiness).

`/healthz` never touches Postgres or Redis — it must answer 200 even while
they are down, so the process is never killed by its own liveness probe.
`/readyz` checks both and returns 503 on failure without leaking the DSN,
host, port, or exception details in the response body (security review area:
no infrastructure fingerprinting for an unauthenticated caller).
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
        postgres_ok = await _check_postgres(session_factory)
        redis_ok = await _check_redis(redis_url)
        if postgres_ok and redis_ok:
            return {"status": "ok"}
        response.status_code = 503
        return {"status": "unavailable"}

    return app


async def _check_postgres(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readyz_postgres_unavailable", exc_info=True)
        return False
    return True


async def _check_redis(redis_url: str) -> bool:
    client = redis_asyncio.Redis.from_url(redis_url)
    try:
        await client.ping()
    except Exception:
        logger.warning("readyz_redis_unavailable", exc_info=True)
        return False
    finally:
        await client.aclose()
    return True
