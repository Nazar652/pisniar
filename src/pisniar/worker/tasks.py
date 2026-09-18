"""Celery task implementations — the Celery CLI entrypoint.

Run as `celery -A pisniar.worker.tasks worker ...`. Importing this module
reads and validates the full environment (`pisniar.config.Settings`), unlike
`pisniar.worker.celery_app`, which stays safe to import anywhere (including
from `pisniar.bot`, which never imports this module).
"""

from __future__ import annotations

from typing import Any

from celery.signals import task_postrun, task_prerun
from sqlalchemy import text

from pisniar.config import get_settings, redaction_secrets
from pisniar.db.session import build_engine, build_session_factory
from pisniar.logging import (
    bind_celery_task_id,
    bind_post_id,
    clear_context,
    configure_logging,
    get_logger,
)
from pisniar.telegram.client import build_bot
from pisniar.telegram.notifier import send_alert
from pisniar.worker.celery_app import PING_TASK_NAME, build_celery_app
from pisniar.worker.runner import run_async

settings = get_settings()
configure_logging(
    log_format=settings.log_format,
    log_level=settings.log_level,
    secrets=redaction_secrets(settings),
)

celery_app = build_celery_app(settings.redis_url)

logger = get_logger(__name__)


# Clears correlation ids around every task, not just `ping` — a worker
# process is long-lived and reuses its own contextvars across tasks, so
# without this a later task could inherit a stale `post_id` from whichever
# task ran before it on the same worker thread.
@task_prerun.connect
def _clear_context_before_task(**kwargs: Any) -> None:
    clear_context()


@task_postrun.connect
def _clear_context_after_task(**kwargs: Any) -> None:
    clear_context()


@celery_app.task(name=PING_TASK_NAME, bind=True)
def ping(self: Any, post_id: int | None = None) -> None:
    """Diagnostic task: proves the worker executes, can reach Postgres, and
    can send a Telegram alert independently of the bot process."""
    bind_celery_task_id(self.request.id)
    if post_id is not None:
        bind_post_id(post_id)
    run_async(_ping(post_id))


async def _ping(post_id: int | None) -> None:
    logger.info("ping_task_started", post_id=post_id)

    engine = build_engine(settings.database_url, use_null_pool=True)
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))

        bot = build_bot(settings)
        try:
            delivered = await send_alert(
                bot, settings.alert_chat_id, f"diag ping executed, post_id={post_id}"
            )
        finally:
            await bot.session.close()
    finally:
        await engine.dispose()

    logger.info("ping_task_finished", post_id=post_id, alert_delivered=delivered)
