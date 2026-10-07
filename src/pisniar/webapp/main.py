"""Mini App entrypoint: one uvicorn server, no Telegram polling and no Celery client."""

from __future__ import annotations

import asyncio

import uvicorn

from pisniar.config import get_settings, redaction_secrets
from pisniar.logging import configure_logging, get_logger
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

    app = create_webapp(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.http_host, port=settings.webapp_port, log_config=None)
    )
    await server.serve()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
