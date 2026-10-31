"""Error reporting through the real SDK with an in-memory transport: what is sent, and above all what is not.
Every credential class is planted in a frame and in a log field and must be absent from the whole event.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest
import sentry_sdk
import urllib3
from pydantic import SecretStr
from sentry_sdk.utils import AnnotatedValue, strip_string
from starlette.requests import Request

import pisniar.logging as pisniar_logging
import pisniar.sentry as pisniar_sentry
from pisniar.config import Settings, redaction_secrets
from pisniar.logging import configure_logging, get_logger, register_runtime_secret
from pisniar.sentry import (
    FILTERED,
    MAX_EVENT_TEXT_LENGTH,
    frontend_ingest_origin,
    init_sentry,
    scrub_event,
)
from pisniar.webapp.auth import issue_session_token

BOT_TOKEN = "123456:SENTINEL-bot-token-value"
SPOTIFY_SECRET = "SENTINEL-spotify-client-secret"
SPOTIFY_ACCESS_TOKEN = "SENTINEL-spotify-access-token"
DATABASE_PASSWORD = "SENTINEL-database-password"
REDIS_PASSWORD = "SENTINEL-redis-password"
PROXY_URL = "http://proxyuser:SENTINEL-proxy-password@proxy-host:8080"
BACKEND_DSN = "https://SENTINELbackendkey@o1.ingest.sentry.io/1"
SESSION_TOKEN = issue_session_token(111, BOT_TOKEN, 1_800_000_000, 3600)
FIELD_SENTINEL = "FIELD-sentinel-value"
LIBRARY_SENTINEL = b"Bearer LIBRARY-sentinel-value"
INIT_DATA = "query_id=AAH-SENTINEL&user=%7B%22id%22%3A111%7D&auth_date=1800000000&hash=SENTINELhash0123456789"

MODULE_SOURCE = """
def fail(**exposed):
    visible_count = 42
    raise RuntimeError("operation failed")
"""


def build_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "bot_token": BOT_TOKEN,
        "admin_ids": "111",
        "alert_chat_id": 1,
        "channel_id": -1,
        "database_url": f"postgresql+asyncpg://u:{DATABASE_PASSWORD}@postgres:5432/db",
        "redis_url": f"redis://:{REDIS_PASSWORD}@redis:6379/0",
        "spotify_client_id": "id",
        "spotify_client_secret": SPOTIFY_SECRET,
        "https_proxy": PROXY_URL,
        "sentry_dsn": BACKEND_DSN,
        "sentry_environment": "staging",
        "sentry_release": "abc123",
    }
    return Settings(_env_file=None, **{**values, **overrides})


@pytest.fixture
def reporting(
    sent_sentry_events: list[Any],
    preserve_root_logging_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    """the whole pipeline as production wires it: logging with its redaction, then the SDK."""
    monkeypatch.setattr(pisniar_logging, "registered_runtime_secrets", {})
    register_runtime_secret("spotify_access_token", SPOTIFY_ACCESS_TOKEN)
    settings = build_settings()
    configure_logging(log_format="json", log_level="INFO", secrets=redaction_secrets(settings))
    init_sentry(settings)
    return sent_sentry_events


@pytest.fixture
def load_module(tmp_path: Path) -> Callable[[str], ModuleType]:
    """a real module file under the given dotted name: the SDK reads `module` from the frame's `__name__`."""

    def load(name: str) -> ModuleType:
        path = tmp_path / f"{name.replace('.', '_')}.py"
        path.write_text(MODULE_SOURCE, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


def report_failure(module: ModuleType, *, logged: object = None, **exposed: Any) -> None:
    """the failure is logged the way our code does it: `exc_info=True`, with a field of the caller's choosing."""
    try:
        module.fail(**exposed)
    except RuntimeError:
        get_logger("pisniar.test").error("operation_failed", exc_info=True, detail=logged, post_id=5)


def capture_failure(module: ModuleType, **exposed: Any) -> None:
    """straight to the SDK, so only its own options and `before_send` stand between the frame and the transport."""
    try:
        module.fail(**exposed)
    except RuntimeError:
        sentry_sdk.capture_exception()


def last_frame_of(event: dict[str, Any]) -> dict[str, Any]:
    return event["exception"]["values"][-1]["stacktrace"]["frames"][-1]


def dumped(events: list[Any]) -> str:
    return json.dumps(events, default=str)


def test_without_a_dsn_nothing_is_initialised(sent_sentry_events: list[Any]) -> None:
    init_sentry(build_settings(sentry_dsn=None))

    assert sentry_sdk.get_client().is_active() is False
    sentry_sdk.capture_message("nobody listens")
    assert sent_sentry_events == []


def test_the_sdk_collects_nothing_beyond_the_error_itself(reporting: list[Any]) -> None:
    options = sentry_sdk.get_client().options

    logging.getLogger("third.party").error("a plain stdlib error")
    sentry_sdk.get_client().flush()

    assert reporting == []
    assert set(sentry_sdk.get_client().integrations) == {"atexit", "dedupe", "excepthook"}
    assert options["default_integrations"] is False
    assert options["include_local_variables"] is True
    assert options["max_breadcrumbs"] == 0
    assert options["max_value_length"] == MAX_EVENT_TEXT_LENGTH
    assert options["traces_sample_rate"] is None
    assert options["profiles_sample_rate"] is None
    assert not options["send_default_pii"]
    assert options["send_client_reports"] is False


def test_an_event_carries_the_stack_the_tags_and_no_request_or_user(
    reporting: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    report_failure(load_module("pisniar.worker.fake"), shown="plain")

    (event,) = reporting
    assert event["environment"] == "staging"
    assert event["release"] == "abc123"
    assert event["tags"] == {"log_event": "operation_failed", "post_id": "5"}
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert "request" not in event
    assert "user" not in event
    assert event["breadcrumbs"] in ({}, {"values": []})
    assert event["contexts"]["log"]["post_id"] == 5
    assert "exc_info" not in event["contexts"]["log"]


def test_a_frame_of_our_code_keeps_its_harmless_variables(
    reporting: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    report_failure(load_module("pisniar.webapp.fake"), shown="plain-visible")

    frame = last_frame_of(reporting[0])
    assert frame["module"] == "pisniar.webapp.fake"
    assert frame["vars"]["visible_count"] == "42"
    assert frame["vars"]["exposed"] == {"shown": "'plain-visible'"}


@pytest.mark.parametrize("module_name", ["somelibrary.client", "__main__", "pisniar.webapp.auth", "pisniar.config"])
def test_frames_of_libraries_and_of_credential_modules_lose_all_variables(
    reporting: list[Any], load_module: Callable[[str], ModuleType], module_name: str
) -> None:
    report_failure(load_module(module_name), shown="plain-visible")

    frame = last_frame_of(reporting[0])
    assert frame["module"] == module_name
    assert "vars" not in frame
    assert "plain-visible" not in json.dumps(reporting[0]["exception"])


def test_the_spotify_adapter_module_loses_its_variables(
    reporting: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    report_failure(load_module("pisniar.resolve.sources.spotify"), shown="plain-visible")

    assert "vars" not in last_frame_of(reporting[0])


CREDENTIALS = {
    "bot_token": (BOT_TOKEN, BOT_TOKEN),
    "spotify_secret": (SPOTIFY_SECRET, SPOTIFY_SECRET),
    "spotify_access_token": (SPOTIFY_ACCESS_TOKEN, SPOTIFY_ACCESS_TOKEN),
    "database_password": (DATABASE_PASSWORD, DATABASE_PASSWORD),
    "redis_password": (REDIS_PASSWORD, REDIS_PASSWORD),
    "proxy_password": ("SENTINEL-proxy-password", "SENTINEL-proxy-password"),
    "proxy_url": (PROXY_URL, "SENTINEL-proxy-password"),
    "backend_dsn": (BACKEND_DSN, "SENTINELbackendkey"),
    "backend_dsn_in_text": (f"dsn is {BACKEND_DSN}", "SENTINELbackendkey"),
    "session_token": (SESSION_TOKEN, SESSION_TOKEN.split(".")[1]),
    "session_token_in_text": (f"stored {SESSION_TOKEN}", SESSION_TOKEN.split(".")[0]),
    "bearer_header": ("Bearer SENTINEL-bearer-value", "SENTINEL-bearer-value"),
    "token_in_url": ("/api/posts/1/audio?token=SENTINEL-query-token", "SENTINEL-query-token"),
    "init_data": (INIT_DATA, "SENTINELhash0123456789"),
    "telegram_fragment": ("https://host/#tgWebAppData=SENTINEL-fragment&v=9", "SENTINEL-fragment"),
}


@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_a_credential_in_a_neutral_local_and_in_a_log_field_never_reaches_the_event(
    reporting: list[Any], load_module: Callable[[str], ModuleType], name: str
) -> None:
    value, sentinel = CREDENTIALS[name]

    report_failure(load_module("pisniar.webapp.fake"), logged=[value], blob=value, nested=[{"deep": (value,)}])

    (event,) = reporting
    assert event["exception"]["values"][-1]["stacktrace"]["frames"][-1]["vars"]["exposed"]["blob"] != repr(value)
    assert sentinel not in dumped(reporting)
    assert value not in dumped(reporting)


@pytest.mark.parametrize(
    "key",
    [
        *["token", "session_token", "password", "secret", "authorization", "init_data", "initData", "signature"],
        *["cookie", "dsn", "proxy_url", "api_key", "bearer", "derived_key", "request", "scope", "headers", "settings"],
        *["context", "query_string", "scope_body"],
    ],
)
def test_a_variable_with_a_credential_name_is_filtered_at_any_depth(
    reporting: list[Any], load_module: Callable[[str], ModuleType], key: str
) -> None:
    report_failure(
        load_module("pisniar.webapp.fake"), **{key: "NAMESENTINEL", "shown": {"inner": {key: "DEEPSENTINEL"}}}
    )

    (event,) = reporting
    frame_variables = last_frame_of(event)["vars"]["exposed"]
    assert frame_variables[key] == FILTERED
    assert frame_variables["shown"] == {"inner": {key: FILTERED}}
    assert "NAMESENTINEL" not in dumped(reporting)
    assert "DEEPSENTINEL" not in dumped(reporting)


def test_a_log_field_with_a_credential_name_is_filtered(
    reporting: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    try:
        load_module("pisniar.webapp.fake").fail()
    except RuntimeError:
        get_logger("pisniar.test").error("failed", exc_info=True, token=FIELD_SENTINEL, note="kept")

    (event,) = reporting
    assert event["contexts"]["log"]["token"] == FILTERED
    assert event["contexts"]["log"]["note"] == "kept"
    assert FIELD_SENTINEL not in dumped(reporting)


def test_a_credential_in_the_exception_message_is_redacted(reporting: list[Any]) -> None:
    try:
        raise ConnectionError(f"AUTH failed for redis://:{REDIS_PASSWORD}@redis:6379/0")
    except ConnectionError:
        get_logger("pisniar.test").error("redis_down", exc_info=True)

    assert REDIS_PASSWORD not in dumped(reporting)
    assert reporting[0]["exception"]["values"][-1]["value"].count(pisniar_logging.REDACTED_PLACEHOLDER) == 1


def test_a_third_party_traceback_logged_by_uvicorn_is_reported_without_library_variables(reporting: list[Any]) -> None:
    def library_failure(raw_scope: dict[str, Any]) -> None:
        raise RuntimeError("asgi failure")

    try:
        library_failure({"headers": [(b"authorization", LIBRARY_SENTINEL)], "query_string": LIBRARY_SENTINEL})
    except RuntimeError as error:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=error)

    (event,) = reporting
    assert "log_event" not in event.get("tags", {})
    assert "vars" not in last_frame_of(event)
    assert LIBRARY_SENTINEL.decode() not in dumped(reporting)


def test_a_string_cut_at_the_limit_is_filtered_whole(
    reporting: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    """the cut leaves the first half of a secret that no exact match can recognise."""
    secret = "HALFSECRET-SENTINEL-TAIL"
    padding = "A" * (MAX_EVENT_TEXT_LENGTH - 3 - len("HALFSECRET"))

    report_failure(load_module("pisniar.webapp.fake"), long_text=padding + secret)

    assert last_frame_of(reporting[0])["vars"]["exposed"]["long_text"] == FILTERED
    assert "HALFSECRET" not in dumped(reporting)


def test_the_sdk_cuts_a_long_string_to_exactly_the_configured_length(
    sent_sentry_events: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    """the observed truncation that `scrub_text`'s threshold relies on, with no scrubber in between."""
    init_sentry(build_settings())
    sentry_sdk.get_client().options["before_send"] = None

    capture_failure(load_module("pisniar.webapp.fake"), long_text="B" * (MAX_EVENT_TEXT_LENGTH * 2))

    cut = last_frame_of(sent_sentry_events[0])["vars"]["exposed"]["long_text"]
    assert len(cut) == MAX_EVENT_TEXT_LENGTH
    assert cut.endswith("...")


def test_a_request_object_in_a_local_is_expanded_by_the_sdk_but_leaves_nothing(
    sent_sentry_events: list[Any], load_module: Callable[[str], ModuleType]
) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/posts/1/audio",
        "raw_path": b"/api/posts/1/audio?token=REQSENTINEL",
        "query_string": b"token=REQSENTINEL",
        "headers": [(b"authorization", b"Bearer REQSENTINEL")],
    }
    init_sentry(build_settings())
    sentry_sdk.get_client().options["before_send"] = None
    capture_failure(load_module("pisniar.webapp.fake"), current=Request(scope))
    unscrubbed = dumped(sent_sentry_events)
    sent_sentry_events.clear()

    init_sentry(build_settings())
    capture_failure(load_module("pisniar.webapp.fake"), current=Request(scope))

    assert "REQSENTINEL" in unscrubbed
    assert "REQSENTINEL" not in dumped(sent_sentry_events)


def test_a_failing_scrubber_sends_nothing(
    reporting: list[Any], load_module: Callable[[str], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(event: dict[str, Any]) -> None:
        raise ValueError("scrubber broke")

    monkeypatch.setattr(pisniar_sentry, "drop_variables_outside_own_code", broken)

    report_failure(load_module("pisniar.webapp.fake"), blob=BOT_TOKEN)

    assert reporting == []


@pytest.mark.parametrize(
    "dsn", ["not-a-dsn-SENTINEL", "https://key@host:abc/1", "https://key@[::1/1", "ftp://key@host/1"]
)
def test_an_invalid_dsn_is_logged_without_its_value_and_the_service_runs_on(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the real `sentry_sdk.init`: with a transport supplied it never parses the DSN. A bad port is a plain
    `ValueError`, not a `BadDsn`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(pisniar_sentry, "logger", fake_logger)

    init_sentry(build_settings(sentry_dsn=dsn))

    fake_logger.error.assert_called_once_with("sentry_dsn_invalid")
    assert sentry_sdk.get_client().is_active() is False


def test_events_go_direct_even_with_https_proxy_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """the SDK would read the variable and then send the proxy no credentials: Sentry goes direct, like Telegram."""
    monkeypatch.setenv("HTTPS_PROXY", PROXY_URL)
    monkeypatch.setenv("HTTP_PROXY", PROXY_URL)
    try:
        sentry_sdk.init(dsn="https://key@o1.ingest.sentry.io/1", default_integrations=False)
        unconfigured = vars(sentry_sdk.get_client().transport)["_pool"]
        sentry_sdk.get_client().close(timeout=0)

        init_sentry(build_settings())
        direct = vars(sentry_sdk.get_client().transport)["_pool"]
    finally:
        sentry_sdk.get_client().close(timeout=0)
        sentry_sdk.get_global_scope().set_client(None)

    assert isinstance(unconfigured, urllib3.ProxyManager)
    assert not isinstance(direct, urllib3.ProxyManager)


def test_an_unreachable_sentry_changes_no_outcome(
    preserve_root_logging_state: None, load_module: Callable[[str], ModuleType]
) -> None:
    """no transport override: the DSN points at a closed loopback port, so the background send fails quietly."""
    settings = build_settings(sentry_dsn="https://key@127.0.0.1:9/1")
    configure_logging(log_format="json", log_level="INFO", secrets=redaction_secrets(settings))
    init_sentry(settings)
    try:
        report_failure(load_module("pisniar.webapp.fake"), shown="x")
    finally:
        sentry_sdk.get_client().close(timeout=0)
        sentry_sdk.get_global_scope().set_client(None)


def test_scrub_event_walks_every_string_of_the_event() -> None:
    event: Any = {
        "message": f"Bearer {SESSION_TOKEN}",
        "tags": {"url": "https://x/?a=1&token=SENTINEL-tag"},
        "extra": {"nested": ["ok", f"fragment tgWebAppData={INIT_DATA}"]},
    }

    scrubbed: Any = scrub_event(event, {}, secrets=[BOT_TOKEN])

    assert "SENTINEL" not in dumped([scrubbed])
    assert scrubbed["extra"]["nested"][0] == "ok"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("signed eyJ1aWQiOjF9.c2ln-_Zm9v end", "signed "),
        ("Authorization: Bearer abc.def-ghi", "Authorization: "),
        ("GET /x?token=abc&y=1", "&y=1"),
        ("GET /x?y=1&token=abc", "?y=1"),
        ("tgWebAppData=query_id%3DAAH&tgWebAppVersion=9", ""),
        ("fields hash=deadbeef and auth_date=17 end", "end"),
        ("signature=abc chat_instance=1 chat_type=sender start_param=x user=%7B%7D", ""),
    ],
)
def test_the_value_patterns_remove_the_credential_shapes(text: str, kept: str) -> None:
    scrubbed = scrub_event({"message": text}, {})["message"]

    assert FILTERED in scrubbed
    assert scrubbed.count("abc") == 0 and "deadbeef" not in scrubbed and "eyJ" not in scrubbed
    assert kept in scrubbed


def test_names_are_filtered_only_inside_variables_and_the_log_context() -> None:
    event: Any = {
        "tags": {"token": "tag-kept"},
        "contexts": {"log": {"proxy": "x"}, "runtime": {"token": "runtime-kept"}},
        "exception": {
            "values": [
                {"stacktrace": {"frames": [{"module": "pisniar.api.fake", "vars": {"token": "x", "shown": "kept"}}]}}
            ]
        },
    }

    scrubbed = scrub_event(event, {})

    assert scrubbed["tags"] == {"token": "tag-kept"}
    assert scrubbed["contexts"] == {"log": {"proxy": FILTERED}, "runtime": {"token": "runtime-kept"}}
    assert scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"] == {"token": FILTERED, "shown": "kept"}


def test_a_frame_without_a_module_loses_its_variables() -> None:
    event: Any = {"exception": {"values": [{"stacktrace": {"frames": [{"vars": {"shown": "x"}}]}}]}}

    assert "vars" not in scrub_event(event, {})["exception"]["values"][0]["stacktrace"]["frames"][0]


def test_the_threads_section_is_scrubbed_like_the_exception() -> None:
    event: Any = {"threads": {"values": [{"stacktrace": {"frames": [{"module": "somelib", "vars": {"a": "b"}}]}}]}}

    assert "vars" not in scrub_event(event, {})["threads"]["values"][0]["stacktrace"]["frames"][0]


def test_the_runtime_registered_secret_is_removed_from_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pisniar_logging, "registered_runtime_secrets", {})
    register_runtime_secret("late", "SENTINEL-late-secret")

    scrubbed = scrub_event({"message": "using SENTINEL-late-secret now"}, {})

    assert "SENTINEL-late-secret" not in scrubbed["message"]


@pytest.mark.parametrize(
    ("dsn", "origin"),
    [
        (None, None),
        ("https://publickey@o1.ingest.sentry.io/2", "https://o1.ingest.sentry.io"),
        ("https://publickey@o1.ingest.de.sentry.io/2", "https://o1.ingest.de.sentry.io"),
        ("http://publickey@o1.ingest.sentry.io/2", None),
        ("https://o1.ingest.sentry.io/2", None),
        ("https://publickey@o1.ingest.sentry.io:8443/2", None),
        ("https://publickey@/2", None),
        ("https://publickey@bad host/2", None),
        ("garbage", None),
        ("https://publickey@o1.ingest.sentry.io:abc/2", None),
        ("https://publickey@[::1/2", None),
    ],
)
def test_the_ingest_origin_comes_only_from_a_public_https_dsn(
    dsn: str | None, origin: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(pisniar_sentry, "logger", fake_logger)

    assert frontend_ingest_origin(None if dsn is None else SecretStr(dsn)) == origin

    assert fake_logger.error.called is (dsn is not None and origin is None)
    for call in fake_logger.error.call_args_list:
        assert call.args == ("sentry_frontend_dsn_invalid",) and not call.kwargs


def cut_by_the_sdk(text: str) -> str:
    """what the SDK itself leaves of a long string, with no scrubber in the way."""
    cut = strip_string(text, MAX_EVENT_TEXT_LENGTH)
    return str(cut.value) if isinstance(cut, AnnotatedValue) else cut


def scrubbed_message(text: str) -> str:
    scrubbed: Any = scrub_event({"message": text}, {})
    return scrubbed["message"]


@pytest.mark.parametrize("letter", ["a", "ж", "😀"], ids=["ascii", "cyrillic_two_bytes", "emoji_four_bytes"])
def test_a_string_the_sdk_cut_by_bytes_is_filtered_whole(letter: str) -> None:
    """Cyrillic is two bytes a character: the cut text is far shorter than the limit in characters."""
    cut = cut_by_the_sdk(letter * MAX_EVENT_TEXT_LENGTH + "HALFSECRET")

    assert cut.endswith("...")
    assert scrubbed_message(cut) == FILTERED


def test_cyrillic_text_well_below_the_byte_limit_is_kept() -> None:
    text = "ж" * (MAX_EVENT_TEXT_LENGTH // 2 - 100)

    assert cut_by_the_sdk(text) == text
    assert scrubbed_message(text) == text


@pytest.mark.parametrize("characters", [MAX_EVENT_TEXT_LENGTH // 2 - 1, MAX_EVENT_TEXT_LENGTH // 2 + 9])
def test_cyrillic_text_at_the_byte_limit_is_filtered(characters: int) -> None:
    assert scrubbed_message("ж" * characters) == FILTERED


def test_a_credential_used_as_a_dictionary_key_is_scrubbed() -> None:
    event: Any = {"extra": {f"Bearer {SESSION_TOKEN}": "kept", "plain": {BOT_TOKEN: 1}}}

    scrubbed: Any = scrub_event(event, {}, secrets=[BOT_TOKEN])

    assert SESSION_TOKEN not in dumped([scrubbed])
    assert BOT_TOKEN not in dumped([scrubbed])
    assert scrubbed["extra"]["plain"] == {pisniar_logging.REDACTED_PLACEHOLDER: 1}


def test_a_key_still_follows_the_name_rule_by_its_original_text() -> None:
    event: Any = {"vars": {"token": "x"}}

    scrubbed: Any = scrub_event(event, {})

    assert scrubbed["vars"] == {"token": FILTERED}
