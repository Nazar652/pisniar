"""Celery app wiring.

`build_celery_app()` is a pure function parametrized on `redis_url`, on
purpose: `pisniar.bot.handlers` builds a lightweight producer-only client
from it to call `send_task()` by name, without importing `pisniar.worker.tasks`
(no task implementations, no worker-side DB engine) — the two processes stay
decoupled. `pisniar.worker.tasks` builds the one "real" instance that tasks
are registered against and that the Celery CLI (`celery -A pisniar.worker.tasks`)
loads.
"""

from __future__ import annotations

from celery import Celery

PING_TASK_NAME = "pisniar.worker.ping"


def build_celery_app(redis_url: str, *, name: str = "pisniar") -> Celery:
    app = Celery(name, broker=redis_url)
    app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        timezone="UTC",
        enable_utc=True,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        result_backend=None,
        broker_connection_retry_on_startup=True,
        # `pisniar.worker.tasks.configure_logging()` runs at *import* time —
        # before the Celery worker's own bootstrap. Celery's default
        # (`worker_hijack_root_logger=True`) tears down every root logger
        # handler and reconfigures logging itself during that later
        # bootstrap step (`celery/app/log.py`: `root.handlers = []`, then
        # its own setup), which would silently throw away the
        # `ProcessorFormatter` handler `configure_logging()` just installed
        # — no JSON, no secret redaction, for the entire `worker` process.
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
    )
    return app
