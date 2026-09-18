"""Structured logging: JSON in docker, console in dev.

Correlation ids (`post_id`, `update_id`, `celery_task_id`) are bound via
`structlog.contextvars` so every log line emitted while handling one update,
one Celery task, or one post travels with the same id without threading it
through every function call.

A redaction processor scrubs any known secret value (the raw bot token) out
of every event dict, as a defense-in-depth backstop on top of `SecretStr`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable, MutableMapping
from typing import Any, cast

import structlog
import structlog.typing

REDACTED_PLACEHOLDER = "***REDACTED***"

# Narrower than `structlog.types.Processor` (which also allows a processor to
# return a final rendered `str | bytes | bytearray | tuple[Any, ...]`, for
# terminal renderers): this processor always both takes and returns an event
# dict, and callers benefit from that precision — see `tests/unit/test_logging.py`.
EventDictProcessor = Callable[[Any, str, MutableMapping[str, Any]], MutableMapping[str, Any]]


def make_redact_secrets_processor(secrets: Iterable[str]) -> EventDictProcessor:
    """Build a structlog processor that replaces `secrets` in string values."""
    known_secrets = [secret for secret in secrets if secret]

    def redact_secrets(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        if not known_secrets:
            return event_dict
        for key, value in list(event_dict.items()):
            if isinstance(value, str):
                redacted = value
                for secret in known_secrets:
                    if secret and secret in redacted:
                        redacted = redacted.replace(secret, REDACTED_PLACEHOLDER)
                event_dict[key] = redacted
        return event_dict

    return redact_secrets


def configure_logging(
    *, environment: str, log_level: str = "INFO", secrets: Iterable[str] = ()
) -> None:
    """Configure structlog for the whole process.

    `environment == "docker"` renders JSON (machine-readable, one object per
    line); anything else renders a human-friendly console format for local
    development.
    """
    level = logging.getLevelNamesMapping()[log_level.upper()]

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        make_redact_secrets_processor(secrets),
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if environment == "docker"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(*args: Any, **kwargs: Any) -> structlog.typing.FilteringBoundLogger:
    """Typed wrapper around `structlog.get_logger()`.

    Upstream, `get_logger()` returns `Any` — the actual logger class depends
    on whatever `wrapper_class` `configure_logging()` installed at runtime,
    which structlog cannot know statically. `configure_logging()` always
    installs `make_filtering_bound_logger()`, so the cast here reflects a
    guarantee this module itself makes, not a blind assertion.
    """
    return cast(structlog.typing.FilteringBoundLogger, structlog.get_logger(*args, **kwargs))


def bind_post_id(post_id: int) -> None:
    structlog.contextvars.bind_contextvars(post_id=post_id)


def bind_update_id(update_id: int) -> None:
    structlog.contextvars.bind_contextvars(update_id=update_id)


def bind_celery_task_id(celery_task_id: str) -> None:
    structlog.contextvars.bind_contextvars(celery_task_id=celery_task_id)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
