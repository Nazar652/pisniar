"""Mini App entrypoint: one uvicorn server and a Telegram client for edits, no polling."""

from __future__ import annotations

import asyncio

import uvicorn

from pisniar.config import get_settings, redaction_secrets
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.logging import configure_logging, get_logger
from pisniar.telegram.client import build_bot
from pisniar.webapp.app import create_webapp

logger = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(
        log_format=settings.log_format,
        log_level=settings.log_level,
        secrets=redaction_secrets(settings),
    )
    if not (settings.webapp_static_dir / "index.html").is_file():
        logger.error("webapp_frontend_missing", static_dir=str(settings.webapp_static_dir))

    engine = build_engine(settings.database_url, lock_timeout_ms=LOCK_TIMEOUT_MS)
    bot = build_bot(settings)
    app = create_webapp(settings, session_factory=build_session_factory(engine), bot=bot)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.http_host, port=settings.webapp_port, log_config=None)
    )
    try:
        await server.serve()
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
