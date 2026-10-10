"""Mini App entrypoint: one uvicorn server, a Telegram client for edits and a Celery producer
for the one render task, no polling.
"""

from __future__ import annotations

import asyncio

import uvicorn

from pisniar.config import get_settings, redaction_secrets
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.logging import configure_logging, get_logger
from pisniar.telegram.client import build_bot
from pisniar.webapp.access_log import install_access_log_filter
from pisniar.webapp.app import create_webapp
from pisniar.worker.celery_app import build_celery_app

logger = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(
        log_format=settings.log_format,
        log_level=settings.log_level,
        secrets=redaction_secrets(settings),
    )
    install_access_log_filter()
    if not (settings.webapp_static_dir / "index.html").is_file():
        logger.error("webapp_frontend_missing", static_dir=str(settings.webapp_static_dir))

    engine = build_engine(settings.database_url, lock_timeout_ms=LOCK_TIMEOUT_MS)
    bot = build_bot(settings)
    celery_client = build_celery_app(settings.redis_url)
    app = create_webapp(
        settings,
        session_factory=build_session_factory(engine),
        bot=bot,
        celery_client=celery_client,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=settings.http_host, port=settings.webapp_port, log_config=None))
    try:
        await server.serve()
    finally:
        celery_client.close()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
