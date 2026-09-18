"""Celery app wiring. `build_celery_app()` is pure/parametrized so
`bot.handlers` can build a producer-only client without importing
`worker.tasks` — the two processes stay decoupled.
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
        # Without this, Celery's own bootstrap wipes the root logger
        # handler configure_logging() installed at import time.
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
    )
    return app
