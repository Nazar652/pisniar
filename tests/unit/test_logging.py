"""The secret-redaction processor: scrubs a known secret value out of every
string field in a structlog event dict, and is a no-op when no secret is
configured or the secret does not appear anywhere."""

from __future__ import annotations

import json
import logging

import pytest
import structlog.testing

import pisniar.logging as pisniar_logging
from pisniar.logging import (
    REDACTED_PLACEHOLDER,
    configure_logging,
    get_logger,
    make_redact_secrets_processor,
    redact_secrets_in_text,
    register_runtime_secret,
)


@pytest.fixture
def isolated_runtime_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pisniar_logging, "registered_runtime_secrets", {})


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
    """an `Exception` (the `error=exc` pattern) has no branch of its own: it goes
    through `str()`, or `JSONRenderer` would serialize it unredacted via `repr()`."""
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
    """through structlog's public `capture_logs()`, not a private attribute like
    `bound._context`, so it keeps working across structlog versions."""
    logger = get_logger("test")

    with structlog.testing.capture_logs() as captured_logs:
        logger.bind(post_id=1).info("hello")

    assert captured_logs == [{"event": "hello", "log_level": "info", "post_id": 1}]


def test_configure_logging_redacts_secrets_from_stdlib_logging_too(
    capsys: pytest.CaptureFixture[str],
    preserve_root_logging_state: None,
) -> None:
    """the point of `foreign_pre_chain`: stdlib `logging` calls (aiogram, uvicorn,
    SQLAlchemy, Celery) get the same redaction as structlog ones."""
    configure_logging(log_format="json", log_level="INFO", secrets=["super-secret-token"])
    logging.getLogger("some.third.party.library").warning(
        "request failed with token=%s", "super-secret-token"
    )

    output = capsys.readouterr().out
    assert "super-secret-token" not in output
    assert REDACTED_PLACEHOLDER in output
    parsed = json.loads(output)
    assert parsed["level"] == "warning"


def test_a_secret_registered_after_startup_is_redacted_too(
    isolated_runtime_secrets: None,
) -> None:
    """the Spotify access token only exists once the resolver runs, long after
    `configure_logging()` was handed the static secret list."""
    processor = make_redact_secrets_processor(["configured-secret"])
    register_runtime_secret("spotify_access_token", "runtime-token-xyz")

    event = processor(None, "info", {"event": "boom", "error": "Bearer runtime-token-xyz"})

    assert "runtime-token-xyz" not in event["error"]
    assert REDACTED_PLACEHOLDER in event["error"]


def test_runtime_secret_registration_replaces_the_previous_value(
    isolated_runtime_secrets: None,
) -> None:
    """keyed by name so hourly token rotation cannot grow the set forever."""
    processor = make_redact_secrets_processor([])
    register_runtime_secret("spotify_access_token", "old-token")
    register_runtime_secret("spotify_access_token", "new-token")

    event = processor(None, "info", {"event": "boom", "error": "old-token new-token"})

    assert event["error"] == f"old-token {REDACTED_PLACEHOLDER}"


def test_redact_secrets_in_text_scrubs_configured_and_runtime_secrets(
    isolated_runtime_secrets: None,
) -> None:
    """`send_alert` and `post.last_error` are sinks no log processor ever sees, so
    the redaction has to exist as a plain function too."""
    register_runtime_secret("spotify_token", "runtime-token-value")

    redacted = redact_secrets_in_text(
        "asyncpg: password=db-password failed, Authorization: runtime-token-value",
        ["db-password"],
    )

    assert "db-password" not in redacted
    assert "runtime-token-value" not in redacted
    assert redacted.count(REDACTED_PLACEHOLDER) == 2


def test_redact_secrets_in_text_leaves_an_innocent_message_alone() -> None:
    assert redact_secrets_in_text("нічого таємного", ["db-password"]) == "нічого таємного"
