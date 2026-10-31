"""Structured logging: one channel, JSON or console, chosen by `log_format`.

`structlog` and stdlib `logging` (aiogram/uvicorn/SQLAlchemy/Celery) share one pipeline and redaction; correlation ids
travel via `structlog.contextvars`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable, MutableMapping
from typing import Any, Literal, cast

import sentry_sdk
import structlog
import structlog.typing

REDACTED_PLACEHOLDER = "***REDACTED***"

# secrets that only exist after startup, so `configure_logging()` could not have been given them — keyed by name, so a
# rotation replaces, not grows.
registered_runtime_secrets: dict[str, str] = {}


def register_runtime_secret(name: str, value: str) -> None:
    """add a secret obtained at runtime (the Spotify access token) to the redaction set — an httpx error carries the
    `Authorization` header."""
    if value:
        registered_runtime_secrets[name] = value


# narrower than `structlog.types.Processor`: always takes/returns an event dict, never a rendered `str`/`bytes`/`tuple`
# — see `test_logging.py`.
EventDictProcessor = Callable[[Any, str, MutableMapping[str, Any]], MutableMapping[str, Any]]


def redact_secrets_in_text(value: str, secrets: Iterable[str] = ()) -> str:
    """also used outside the log pipeline: `send_alert` and `post.last_error` pass through no processor."""
    redacted = value
    for secret in [*[secret for secret in secrets if secret], *registered_runtime_secrets.values()]:
        if secret in redacted:
            redacted = redacted.replace(secret, REDACTED_PLACEHOLDER)
    return redacted


def make_redact_secrets_processor(secrets: Iterable[str]) -> EventDictProcessor:
    configured_secrets = [secret for secret in secrets if secret]

    def all_secrets() -> list[str]:
        # snapshot first: this runs from service threads too (`ytmusicapi` logs from `to_thread`) while registration
        # mutates the dict.
        runtime_secrets = list(registered_runtime_secrets.values())
        return [*configured_secrets, *runtime_secrets]

    def redact_string(value: str) -> str:
        return redact_secrets_in_text(value, configured_secrets)

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
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        if not all_secrets():
            return event_dict
        for key, value in list(event_dict.items()):
            event_dict[key] = redact_value(value)
        return event_dict

    return redact_secrets


SENTRY_LEVELS = frozenset({"error", "critical"})
SENTRY_TAG_FIELDS = ("post_id", "track_id", "update_id", "celery_task_id", "user_id")
NON_CONTEXT_FIELDS = frozenset({"exc_info", "_record", "_from_structlog"})


def exception_of(exc_info: object) -> BaseException | tuple[Any, Any, Any] | None:
    """the exception behind `exc_info` in any of the three shapes structlog and stdlib hand over."""
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and exc_info[0] is not None:
        return cast(tuple[Any, Any, Any], exc_info)
    return None


def report_error_to_sentry(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """one capture per ERROR line: our own lines always, a third party's only with an exception. Placed before
    `format_exc_info`, which would consume the traceback; a no-op until `init_sentry` ran."""
    if event_dict.get("level") not in SENTRY_LEVELS:
        return event_dict

    foreign_record = event_dict.get("_record")
    exception = exception_of(event_dict.get("exc_info"))
    if foreign_record is not None and (foreign_record.name.startswith("sentry_sdk") or exception is None):
        return event_dict

    with sentry_sdk.new_scope() as scope:
        if foreign_record is None:
            scope.set_tag("log_event", str(event_dict.get("event")))
        for field in SENTRY_TAG_FIELDS:
            if event_dict.get(field) is not None:
                scope.set_tag(field, str(event_dict[field]))
        scope.set_context("log", {key: value for key, value in event_dict.items() if key not in NON_CONTEXT_FIELDS})

        if exception is not None:
            sentry_sdk.capture_exception(exception)
        else:
            sentry_sdk.capture_message(str(event_dict.get("event")), level="error")
    return event_dict


def configure_logging(
    *,
    log_format: Literal["json", "console"] = "json",
    log_level: str = "INFO",
    secrets: Iterable[str] = (),
) -> None:
    level = logging.getLevelNamesMapping()[log_level.upper()]

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        report_error_to_sentry,
        structlog.processors.format_exc_info,
        make_redact_secrets_processor(secrets),
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if log_format == "json" else structlog.dev.ConsoleRenderer()
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

    # foreign_pre_chain: runs shared_processors (redaction included) on stdlib `logging` records too, so both sources
    # share one format/pass.
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
    """typed wrapper: upstream `get_logger()` returns `Any`, but `configure_logging()` always installs
    `make_filtering_bound_logger()`.
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
