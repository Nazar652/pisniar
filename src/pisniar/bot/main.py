"""Bot process entrypoint: aiogram long-polling + the health/readiness API
running in the same process (FastAPI lives inside `bot`, no fifth container
just for `/healthz`).

Graceful shutdown: aiogram's `start_polling()` already installs its own
SIGTERM/SIGINT handler and returns once polling stops; we piggyback on that
rather than registering a second, competing signal handler for the same
signal (`asyncio.loop.add_signal_handler` replaces, it does not chain). When
either the polling task or the HTTP server task finishes first, we tell the
other one to stop too, so `docker compose stop` / a hot-reload restart never
leaves a half-open connection.
"""

from __future__ import annotations

import asyncio

import uvicorn
from aiogram import Dispatcher

from pisniar.api.app import create_app
from pisniar.bot.handlers import router as commands_router
from pisniar.bot.middlewares import AdminAllowlistMiddleware
from pisniar.config import get_settings
from pisniar.db.session import build_engine, build_session_factory
from pisniar.logging import configure_logging, get_logger
from pisniar.telegram.client import build_bot
from pisniar.worker.celery_app import build_celery_app

logger = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(
        environment=settings.environment,
        log_level=settings.log_level,
        secrets=[settings.bot_token.get_secret_value()],
    )

    bot = build_bot(settings)
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    celery_client = build_celery_app(settings.redis_url)

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(AdminAllowlistMiddleware(frozenset(settings.admin_ids)))
    dispatcher.include_router(commands_router)

    app = create_app(session_factory=session_factory, redis_url=settings.redis_url)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    )

    async def run_polling() -> None:
        await dispatcher.start_polling(
            bot, session_factory=session_factory, celery_client=celery_client
        )

    polling_task = asyncio.create_task(run_polling(), name="bot-polling")
    http_task = asyncio.create_task(server.serve(), name="bot-http")

    try:
        done, pending = await asyncio.wait(
            {polling_task, http_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if polling_task in done:
            logger.info("polling_stopped_stopping_http")
            server.should_exit = True
        else:
            logger.info("http_stopped_stopping_polling")
            await dispatcher.stop_polling()

        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
