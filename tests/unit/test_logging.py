"""The secret-redaction processor: scrubs a known secret value out of every
string field in a structlog event dict, and is a no-op when no secret is
configured or the secret does not appear anywhere."""

from __future__ import annotations

import json
import logging

import pytest
import structlog.testing

from pisniar.logging import (
    REDACTED_PLACEHOLDER,
    configure_logging,
    get_logger,
    make_redact_secrets_processor,
)


def test_redacts_secret_value_wherever_it_appears() -> None:
    processor = make_redact_secrets_processor(["super-secret-token"])

    event_dict = processor(
        None,
        "info",
        {
            "event": "sending request",
            "url": "https://api.telegram.org/botsuper-secret-token/sendMessage",
            "detail": "token was super-secret-token, retrying",
            "count": 3,
        },
    )

    assert "super-secret-token" not in event_dict["url"]
    assert REDACTED_PLACEHOLDER in event_dict["url"]
    assert "super-secret-token" not in event_dict["detail"]
    assert event_dict["count"] == 3, "non-string values must pass through untouched"


def test_no_secrets_configured_is_a_no_op() -> None:
    processor = make_redact_secrets_processor([])

    event_dict = processor(None, "info", {"event": "hello", "token": "anything"})

    assert event_dict == {"event": "hello", "token": "anything"}


def test_empty_string_secret_is_ignored() -> None:
    processor = make_redact_secrets_processor(["", "real-secret"])

    event_dict = processor(None, "info", {"event": "real-secret leaked"})

    assert event_dict["event"] == f"{REDACTED_PLACEHOLDER} leaked"


def test_redaction_recurses_into_nested_dict_list_and_tuple() -> None:
    processor = make_redact_secrets_processor(["super-secret-token"])

    event_dict = processor(
        None,
        "info",
        {
            "event": "nested",
            "context": {"headers": {"Authorization": "Bearer super-secret-token"}},
            "attempts": ["ok", "failed: super-secret-token"],
            "pair": ("super-secret-token", "unrelated"),
        },
    )

    assert "super-secret-token" not in json.dumps(event_dict)


def test_redaction_stringifies_and_scrubs_unrecognized_values() -> None:
    """An `Exception` instance (the `error=exc` pattern step 2 will use with
    httpx) has no dedicated branch — it falls through to `str()` before
    redaction, since `JSONRenderer` would otherwise serialize it untouched
    via `repr()`."""
    processor = make_redact_secrets_processor(["super-secret-token"])

    event_dict = processor(
        None,
        "error",
        {"event": "request failed", "error": ValueError("token=super-secret-token")},
    )

    assert "super-secret-token" not in str(event_dict["error"])


def test_redaction_leaves_non_string_scalars_untouched() -> None:
    processor = make_redact_secrets_processor(["super-secret-token"])

    event_dict = processor(
        None, "info", {"event": "counted", "count": 3, "ok": True, "missing": None}
    )

    assert event_dict["count"] == 3
    assert event_dict["ok"] is True
    assert event_dict["missing"] is None


def test_get_logger_returns_a_working_bound_logger() -> None:
    """Structural check via `structlog.testing.capture_logs()` (structlog's
    own public testing API), not a private attribute like `bound._context`
    — this must keep working across structlog versions."""
    logger = get_logger("test")

    with structlog.testing.capture_logs() as captured_logs:
        logger.bind(post_id=1).info("hello")

    assert captured_logs == [{"event": "hello", "log_level": "info", "post_id": 1}]


def test_configure_logging_redacts_secrets_from_stdlib_logging_too(
    capsys: pytest.CaptureFixture[str],
    preserve_root_logging_state: None,
) -> None:
    """The whole point of `foreign_pre_chain`: a plain stdlib `logging` call
    (as aiogram/uvicorn/SQLAlchemy/Celery all make) must go through the same
    redaction as a `structlog.get_logger()` call, not bypass it.
    """
    configure_logging(log_format="json", log_level="INFO", secrets=["super-secret-token"])
    logging.getLogger("some.third.party.library").warning(
        "request failed with token=%s", "super-secret-token"
    )

    output = capsys.readouterr().out
    assert "super-secret-token" not in output
    assert REDACTED_PLACEHOLDER in output
    parsed = json.loads(output)
    assert parsed["level"] == "warning"
