"""`worker_hijack_root_logger=False`: without it, Celery's own bootstrap (`celery.app.log.Logging`) wipes the root
handler `configure_logging()` installed at import time — taking JSON output and redaction down with it.
"""

from __future__ import annotations

import logging

from celery.app.log import Logging

from pisniar.logging import configure_logging
from pisniar.worker.celery_app import (
    DOWNLOAD_POST_AUDIO_TASK_NAME,
    FETCH_PLAIN_LYRICS_TASK_NAME,
    PING_TASK_NAME,
    PREPARE_POST_AUDIO_TASK_NAME,
    PUBLISH_POST_TASK_NAME,
    RENDER_POST_VIDEO_TASK_NAME,
    RESOLVE_POST_TASK_NAME,
    TASK_SOFT_TIME_LIMIT_SECONDS,
    TASK_TIME_LIMIT_SECONDS,
    build_celery_app,
)


def test_worker_hijack_root_logger_is_disabled() -> None:
    app = build_celery_app("redis://localhost:6379/0")

    assert app.conf.worker_hijack_root_logger is False
    assert app.conf.worker_redirect_stdouts is False


def test_celery_logging_bootstrap_does_not_wipe_our_handler(
    preserve_root_logging_state: None,
) -> None:
    configure_logging(log_format="json", log_level="INFO", secrets=[])
    root_logger = logging.getLogger()
    our_handler = root_logger.handlers[0]

    app = build_celery_app("redis://localhost:6379/0")
    # reset Celery's class-level bootstrap flag; setattr (not `Logging.
    # _setup = `) since it's an unstubbed internal, unresolved under ty.
    setattr(Logging, "_setup", False)  # noqa: B010
    try:
        Logging(app).setup_logging_subsystem(loglevel="INFO")
    finally:
        setattr(Logging, "_setup", False)  # noqa: B010

    assert root_logger.handlers == [our_handler]


def test_only_the_render_is_routed_to_its_own_queue() -> None:
    app = build_celery_app("redis://localhost:6379/0")
    routes = app.conf.task_routes

    assert routes == {RENDER_POST_VIDEO_TASK_NAME: {"queue": "render"}}
    names = (
        PING_TASK_NAME,
        RESOLVE_POST_TASK_NAME,
        PUBLISH_POST_TASK_NAME,
        DOWNLOAD_POST_AUDIO_TASK_NAME,
        PREPARE_POST_AUDIO_TASK_NAME,
        FETCH_PLAIN_LYRICS_TASK_NAME,
    )
    queue_of = app.amqp.router.route
    assert queue_of({}, RENDER_POST_VIDEO_TASK_NAME)["queue"].name == "render"
    for name in names:
        assert queue_of({}, name)["queue"].name == "celery", name


def test_tasks_have_both_a_soft_and_a_hard_time_limit() -> None:
    """`ytmusicapi` has no timeout of its own, so without these a wedged TCP
    connection owns the only worker slot until a manual restart."""
    app = build_celery_app("redis://localhost:6379/0")

    assert app.conf.task_soft_time_limit == TASK_SOFT_TIME_LIMIT_SECONDS
    assert app.conf.task_time_limit == TASK_TIME_LIMIT_SECONDS
    assert TASK_SOFT_TIME_LIMIT_SECONDS < TASK_TIME_LIMIT_SECONDS
