"""`worker_hijack_root_logger=False` (+ `worker_redirect_stdouts=False`):
without it, Celery's own logging bootstrap (`celery.app.log.Logging`) wipes
every root logger handler during worker startup — including the
`ProcessorFormatter` handler `configure_logging()` installs at import time in
`pisniar.worker.tasks`, taking JSON output and secret redaction down with it
for the entire `worker` process. `pisniar.worker.tasks` module import runs
`configure_logging()` before Celery's own bootstrap has a chance to run, so
this is a real ordering hazard, not a hypothetical one.
"""

from __future__ import annotations

import logging

from celery.app.log import Logging

from pisniar.logging import configure_logging
from pisniar.worker.celery_app import build_celery_app


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
    # `Logging._setup` is a *class*-level flag Celery sets after the first
    # successful bootstrap in this process — reset it so this test exercises
    # the real path regardless of what ran before it in the same suite.
    # `setattr`/`getattr`, not a direct attribute reference: `_setup` is a
    # genuine but unstubbed Celery internal (not in `celery-types`), and a
    # direct `Logging._setup = ...` is an `unresolved-attribute` under `ty`.
    setattr(Logging, "_setup", False)  # noqa: B010
    try:
        Logging(app).setup_logging_subsystem(loglevel="INFO")
    finally:
        setattr(Logging, "_setup", False)  # noqa: B010

    assert root_logger.handlers == [our_handler]
