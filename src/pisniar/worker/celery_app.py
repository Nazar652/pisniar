"""Celery app wiring. `build_celery_app()` is pure/parametrized so
`bot.handlers` can build a producer-only client without importing
`worker.tasks` — the two processes stay decoupled.
"""

from __future__ import annotations

from celery import Celery

PING_TASK_NAME = "pisniar.worker.ping"
RESOLVE_POST_TASK_NAME = "pisniar.worker.resolve_post"
# Dispatched by name from `bot.handlers`, which must never import the task
# itself — that import edge is the boundary AGENTS.md keeps closed.
PUBLISH_POST_TASK_NAME = "pisniar.worker.publish_post"

# A full resolve is ~20s of throttled HTTP; these are the ceiling that stops
# one wedged socket from owning the single worker slot forever (F-15).
TASK_SOFT_TIME_LIMIT_SECONDS = 300
TASK_TIME_LIMIT_SECONDS = 360


def build_celery_app(redis_url: str, *, name: str = "pisniar") -> Celery:
    app = Celery(name, broker=redis_url)
    app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=TASK_TIME_LIMIT_SECONDS,
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
