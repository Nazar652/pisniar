"""Celery app wiring. `build_celery_app()` is pure/parametrized so
`bot.handlers` can build a producer-only client without importing
`worker.tasks` — the two processes stay decoupled.
"""

from __future__ import annotations

from celery import Celery

PING_TASK_NAME = "pisniar.worker.ping"
RESOLVE_POST_TASK_NAME = "pisniar.worker.resolve_post"
PUBLISH_POST_TASK_NAME = "pisniar.worker.publish_post"

# the render button starts the download, which chains the render; the Mini App
# prepares the audio only, with no render behind it.
DOWNLOAD_POST_AUDIO_TASK_NAME = "pisniar.worker.download_post_audio"
PREPARE_POST_AUDIO_TASK_NAME = "pisniar.worker.prepare_post_audio"
RENDER_POST_VIDEO_TASK_NAME = "pisniar.worker.render_post_video"

# keeps one wedged socket from owning the worker slot. `bot.handlers` reads these
# as the stale-resolve window, so media tasks take their limits from `Settings`.
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
        # without this, Celery's own bootstrap wipes the root logger
        # handler configure_logging() installed at import time.
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
    )
    return app
