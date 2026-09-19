"""Structured logging: one channel, JSON or console, chosen by `log_format`.

`structlog` and stdlib `logging` (aiogram/uvicorn/SQLAlchemy/Celery) share one
pipeline and redaction; correlation ids travel via `structlog.contextvars`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable, MutableMapping
from typing import Any, Literal, cast

import structlog
import structlog.typing

REDACTED_PLACEHOLDER = "***REDACTED***"

# Secrets that only exist after startup, so `configure_logging()` could not
# have been given them — keyed by name, so a rotation replaces, not grows.
_runtime_secrets: dict[str, str] = {}


def register_runtime_secret(name: str, value: str) -> None:
    """Add a secret obtained at runtime (the Spotify access token) to the
    redaction set — an httpx error carries the `Authorization` header."""
    if value:
        _runtime_secrets[name] = value


def clear_runtime_secrets() -> None:
    _runtime_secrets.clear()


# Narrower than `structlog.types.Processor`: always takes/returns an event
# dict, never a rendered `str`/`bytes`/`tuple` — see `test_logging.py`.
EventDictProcessor = Callable[[Any, str, MutableMapping[str, Any]], MutableMapping[str, Any]]


def make_redact_secrets_processor(secrets: Iterable[str]) -> EventDictProcessor:
    """Replace `secrets` in event dict values, recursing into dict/list/tuple
    and `str()`-ing unrecognized types (e.g. `error=exc`) first. Leaves
    bool/int/float/None alone — a secret can't be a substring of a number.
    """
    configured_secrets = [secret for secret in secrets if secret]

    def all_secrets() -> list[str]:
        return [*configured_secrets, *_runtime_secrets.values()]

    def redact_string(value: str) -> str:
        redacted = value
        for secret in all_secrets():
            if secret in redacted:
                redacted = redacted.replace(secret, REDACTED_PLACEHOLDER)
        return redacted

    def redact_value(value: object) -> object:
        if isinstance(value, str):
            return redact_string(value)
        if isinstance(value, dict):
            return {key: redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(redact_value(item) for item in value)
        if isinstance(value, bool | int | float | type(None)):
            return value
        return redact_string(str(value))

    def redact_secrets(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        if not all_secrets():
            return event_dict
        for key, value in list(event_dict.items()):
            event_dict[key] = redact_value(value)
        return event_dict

    return redact_secrets


def configure_logging(
    *,
    log_format: Literal["json", "console"] = "json",
    log_level: str = "INFO",
    secrets: Iterable[str] = (),
) -> None:
    """Configure structlog + stdlib `logging` as one pipeline. `"json"`
    (docker/prod default) renders one object per line; `"console"` is for
    local development.
    """
    level = logging.getLevelNamesMapping()[log_level.upper()]

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
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # foreign_pre_chain: runs shared_processors (redaction included) on
    # stdlib `logging` records too, so both sources share one format/pass.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)


def get_logger(*args: Any, **kwargs: Any) -> structlog.typing.FilteringBoundLogger:
    """Typed wrapper: upstream `get_logger()` returns `Any`, but
    `configure_logging()` always installs `make_filtering_bound_logger()`.
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
