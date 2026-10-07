"""Bot entrypoint: aiogram long-polling + the health/readiness API in one process.
`handle_signals=False` makes uvicorn the sole SIGTERM/SIGINT owner (it would silently
override aiogram's handler otherwise), so this module propagates shutdown itself.
"""

from __future__ import annotations

import asyncio
import contextlib

import uvicorn
from aiogram import Dispatcher

from pisniar.api.app import create_app
from pisniar.bot.handlers import router as commands_router
from pisniar.bot.middlewares import AdminAllowlistMiddleware
from pisniar.channel import warn_if_alerts_share_the_channel
from pisniar.config import get_settings, redaction_secrets
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.logging import configure_logging, get_logger
from pisniar.telegram.client import build_bot
from pisniar.telegram.menu import configure_admin_menu_buttons
from pisniar.worker.celery_app import build_celery_app

logger = get_logger(__name__)


def build_dispatcher(admin_ids: frozenset[int]) -> Dispatcher:
    """own function so a test can assert the allowlist landed as an *outer*
    middleware on `dispatcher.update`, not e.g. on `dispatcher.message`."""
    # disable_fsm: the bot keeps no FSM state; otherwise FSMContextMiddleware
    # runs before the allowlist and touches storage per non-admin update.
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(AdminAllowlistMiddleware(admin_ids))
    dispatcher.include_router(commands_router)
    return dispatcher


async def run() -> None:
    settings = get_settings()
    configure_logging(
        log_format=settings.log_format,
        log_level=settings.log_level,
        secrets=redaction_secrets(settings),
    )
    warn_if_alerts_share_the_channel(settings.channel_id, settings.alert_chat_id)

    bot = build_bot(settings)
    await configure_admin_menu_buttons(bot, settings.admin_ids, settings.webapp_public_url)

    # the bot needs `lock_timeout` too: a handler blocked on a row lock
    # otherwise holds its QueuePool slot indefinitely.
    engine = build_engine(settings.database_url, lock_timeout_ms=LOCK_TIMEOUT_MS)
    session_factory = build_session_factory(engine)
    celery_client = build_celery_app(settings.redis_url)

    dispatcher = build_dispatcher(frozenset(settings.admin_ids))

    app = create_app(session_factory=session_factory, redis_url=settings.redis_url)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    )

    async def run_polling() -> None:
        await dispatcher.start_polling(
            bot,
            session_factory=session_factory,
            celery_client=celery_client,
            handle_signals=False,
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
            # RuntimeError if polling never started; don't mask the real
            # root cause, which the loop below surfaces instead.
            with contextlib.suppress(RuntimeError):
                await dispatcher.stop_polling()

        pending_results = await asyncio.gather(*pending, return_exceptions=True)
        for task, result in zip(pending, pending_results, strict=True):
            if isinstance(result, BaseException):
                # return_exceptions=True must not mean silently discarded.
                logger.warning(
                    "pending_task_failed_during_shutdown",
                    task=task.get_name(),
                    error=repr(result),
                )

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
