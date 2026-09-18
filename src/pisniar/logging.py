"""Structured logging: one channel, JSON or console, chosen by `log_format`.

Every log line — whether it comes from `structlog.get_logger()` or from a
plain stdlib `logging.getLogger()` call inside aiogram, uvicorn, SQLAlchemy,
or Celery — goes through the same `structlog.stdlib.ProcessorFormatter`
pipeline and the same redaction processor before it reaches stdout. This is
the point of the `foreign_pre_chain=shared_processors` wiring below: without
it, third-party stdlib log records would bypass `make_redact_secrets_processor`
entirely and the bot token could leak through, say, an aiohttp or uvicorn
access log line.

Correlation ids (`post_id`, `update_id`, `celery_task_id`) are bound via
`structlog.contextvars` so every log line emitted while handling one update,
one Celery task, or one post travels with the same id without threading it
through every function call.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable, MutableMapping
from typing import Any, Literal, cast

import structlog
import structlog.typing

REDACTED_PLACEHOLDER = "***REDACTED***"

# Narrower than `structlog.types.Processor` (which also allows a processor to
# return a final rendered `str | bytes | bytearray | tuple[Any, ...]`, for
# terminal renderers): this processor always both takes and returns an event
# dict, and callers benefit from that precision — see `tests/unit/test_logging.py`.
EventDictProcessor = Callable[[Any, str, MutableMapping[str, Any]], MutableMapping[str, Any]]


def make_redact_secrets_processor(secrets: Iterable[str]) -> EventDictProcessor:
    """Build a structlog processor that replaces `secrets` in string values.

    Recurses into `dict`/`list`/`tuple` values (not just top-level string
    fields) — an event dict logged with `error=exc` or a nested `context={...}`
    payload must not smuggle a secret past this processor just because it
    isn't a bare string at the top level. Values of an unrecognized type
    (e.g. an `Exception` instance passed as `error=exc`) are redacted via
    their `str()` too: `JSONRenderer` falls back to `repr()` for anything it
    doesn't know how to serialize natively, so a secret embedded in, say, an
    exception's message would otherwise reach the final output untouched.
    `bool`/`int`/`float`/`None` are left as themselves — stringifying those
    would break their JSON typing for no redaction benefit, since a secret
    (always a string) cannot be a substring of a number or a boolean.
    """
    known_secrets = [secret for secret in secrets if secret]

    def redact_string(value: str) -> str:
        redacted = value
        for secret in known_secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, REDACTED_PLACEHOLDER)
        return redacted

    def redact_value(value: object) -> object:
        if not known_secrets:
            return value
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
        if not known_secrets:
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
    """Configure structlog and stdlib `logging` as one unified pipeline.

    `log_format == "json"` renders one JSON object per line (machine-readable,
    the docker/prod default); `"console"` renders a human-friendly format for
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

    # `foreign_pre_chain` runs the very same `shared_processors` — redaction
    # included — on log records that came from plain stdlib `logging` calls
    # (aiogram, uvicorn, SQLAlchemy, Celery), so both sources end up on one
    # channel with one format and one redaction pass.
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
