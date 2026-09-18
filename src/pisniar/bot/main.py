"""Bot process entrypoint: aiogram long-polling + the health/readiness API
running in the same process (FastAPI lives inside `bot`, no fifth container
just for `/healthz`).

Graceful shutdown: uvicorn's `Server.serve()` unconditionally installs its
own SIGTERM/SIGINT handler via the low-level `signal.signal()` API the
moment it starts running — this silently overwrites whatever aiogram's
`Dispatcher.start_polling()` registered via `loop.add_signal_handler()`
(both ultimately compete for the same one process-wide C-level handler slot,
and which one "wins" depends on exactly when each library's registration
code happens to run relative to the other's `await` points — not a
reliable thing to depend on). So `start_polling(..., handle_signals=False)`
below makes uvicorn the sole, unambiguous owner of OS signal handling.
Propagating that shutdown to aiogram is this module's own job: whichever of
`polling_task` / `http_task` finishes first (uvicorn's `should_exit` flips on
SIGTERM) tells the other one to stop, so `docker compose stop` / a
hot-reload restart never leaves a half-open connection.
"""

from __future__ import annotations

import asyncio
import contextlib

import uvicorn
from aiogram import Dispatcher

from pisniar.api.app import create_app
from pisniar.bot.handlers import router as commands_router
from pisniar.bot.middlewares import AdminAllowlistMiddleware
from pisniar.config import get_settings, redaction_secrets
from pisniar.db.session import build_engine, build_session_factory
from pisniar.logging import configure_logging, get_logger
from pisniar.telegram.client import build_bot
from pisniar.worker.celery_app import build_celery_app

logger = get_logger(__name__)


def build_dispatcher(admin_ids: frozenset[int]) -> Dispatcher:
    """Construct the `Dispatcher` with the admin allowlist wired in as an
    *outer* middleware on `dispatcher.update` — the barrier that runs for
    every update type before any handler is even looked up.

    Kept as its own function, separate from `run()`, specifically so a test
    can assert the middleware ended up on `dispatcher.update.outer_middleware`
    and not, say, `dispatcher.message.middleware` (which would leave every
    non-`message` update type — `callback_query`, `inline_query`,
    `my_chat_member`, ...— unchecked, while every test that only exercises
    `message` updates stays green).
    """
    # `disable_fsm=True`: step 1 has no FSM-based conversation state, and
    # without this the built-in `FSMContextMiddleware` still runs before the
    # admin allowlist middleware and creates a `MemoryStorage` entry for
    # every non-admin update too.
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

    bot = build_bot(settings)
    engine = build_engine(settings.database_url)
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
            # `stop_polling()` raises `RuntimeError` if polling never
            # actually started (e.g. it crashed before acquiring its
            # internal lock) — suppressed so that failure doesn't mask the
            # real root cause, which the loop below surfaces instead.
            with contextlib.suppress(RuntimeError):
                await dispatcher.stop_polling()

        pending_results = await asyncio.gather(*pending, return_exceptions=True)
        for task, result in zip(pending, pending_results, strict=True):
            if isinstance(result, BaseException):
                # `return_exceptions=True` is there so one task's failure
                # during shutdown can't stop us from waiting for the other —
                # not to silently discard it. On SIGTERM this is the only
                # place the *second* task's own shutdown error would ever
                # surface.
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
