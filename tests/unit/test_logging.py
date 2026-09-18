"""The secret-redaction processor: scrubs a known secret value out of every
string field in a structlog event dict, and is a no-op when no secret is
configured or the secret does not appear anywhere."""

from __future__ import annotations

from pisniar.logging import REDACTED_PLACEHOLDER, get_logger, make_redact_secrets_processor


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


def test_get_logger_returns_a_bindable_logger() -> None:
    logger = get_logger("test")

    # Structural check only: binding must not raise, regardless of whether
    # `configure_logging()` has run yet in this process.
    bound = logger.bind(post_id=1)
    assert bound is not None
