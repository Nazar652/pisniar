"""Error reporting to Sentry: the SDK setup, the barrier every event passes, and the Mini App's ingest origin.

Local variables are on, so `scrub_event` is what keeps credentials out; it fails closed (a raise drops the event).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import partial
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from pydantic import SecretStr
from sentry_sdk.integrations.atexit import AtexitIntegration
from sentry_sdk.integrations.dedupe import DedupeIntegration
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.types import Event, Hint

from pisniar.config import Settings, bare_https_origin, redaction_secrets
from pisniar.logging import get_logger, redact_secrets_in_text

logger = get_logger(__name__)

MAX_EVENT_TEXT_LENGTH = 8192
FILTERED = "[Filtered]"
IN_APP_MODULE_PREFIX = "pisniar."
# modules that handle credentials: their locals are never sent
VARIABLE_FREE_MODULES = frozenset({"pisniar.webapp.auth", "pisniar.config", "pisniar.resolve.sources.spotify"})
# a key under one of these is filtered by its name at any depth
NAME_FILTERED_SUBTREES = frozenset({"vars", "log"})
FILTERED_NAME = re.compile(
    "token|secret|passw|auth|init_?data|signature|credential|cookie|dsn|proxy|api_?key|bearer|derived_key"
    "|request|scope|headers|settings|context|query",
    re.IGNORECASE,
)
FILTERED_VALUES = tuple(
    re.compile(pattern)
    for pattern in (
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        r"Bearer\s+\S+",
        r"[?&]token=[^&\s'\"]+",
        r"tgWebAppData=[^\s'\"]*",
        r"\b(?:query_id|auth_date|hash|signature|chat_instance|chat_type|start_param|user)=[^\s'\"&]*",
    )
)


def scrub_text(text: str, secrets: Iterable[str]) -> str:
    """the SDK cuts by UTF-8 bytes to between limit-3 and limit, "..." included; a cut could leave part of a secret that
    no exact match sees, so anything that long goes whole."""
    if len(text.encode("utf-8", "replace")) >= MAX_EVENT_TEXT_LENGTH - len("..."):
        return FILTERED
    scrubbed = redact_secrets_in_text(text, secrets)
    for pattern in FILTERED_VALUES:
        scrubbed = pattern.sub(FILTERED, scrubbed)
    return scrubbed


def scrub_key(key: Any, secrets: Iterable[str]) -> Any:
    """a key can carry a value too (a local `{token: ...}` repr'd as a dict keyed by it)."""
    return scrub_text(key, secrets) if isinstance(key, str) else key


def scrub_node(node: Any, secrets: Iterable[str], *, filter_by_name: bool) -> Any:
    if isinstance(node, str):
        return scrub_text(node, secrets)
    if isinstance(node, dict):
        return {
            scrub_key(key, secrets): FILTERED
            if filter_by_name and FILTERED_NAME.search(str(key))
            else scrub_node(item, secrets, filter_by_name=filter_by_name or key in NAME_FILTERED_SUBTREES)
            for key, item in node.items()
        }
    if isinstance(node, list | tuple):
        return [scrub_node(item, secrets, filter_by_name=filter_by_name) for item in node]
    return node


def frames_of(event: Event) -> list[dict[str, Any]]:
    sections = [event.get("exception"), event.get("threads")]
    values = [value for section in sections if section for value in section.get("values", [])]
    return [frame for value in values for frame in (value.get("stacktrace") or {}).get("frames", [])]


def drop_variables_outside_own_code(event: Event) -> None:
    for frame in frames_of(event):
        module = frame.get("module") or ""
        if not module.startswith(IN_APP_MODULE_PREFIX) or module in VARIABLE_FREE_MODULES:
            frame.pop("vars", None)


def scrub_event(event: Event, hint: Hint, *, secrets: Iterable[str] = ()) -> Event:
    """`before_send`: no `try` on purpose, because the SDK drops an event whose callback raised."""
    drop_variables_outside_own_code(event)
    return scrub_node(event, list(secrets), filter_by_name=False)


def init_sentry(settings: Settings) -> None:
    """no DSN, no SDK; a malformed one (`BadDsn` or a bad port, both `ValueError`) is logged without its value."""
    if settings.sentry_dsn is None:
        return

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.get_secret_value(),
            environment=settings.sentry_environment,
            release=settings.sentry_release,
            default_integrations=False,
            integrations=[AtexitIntegration(), ExcepthookIntegration(), DedupeIntegration()],
            include_local_variables=True,
            max_value_length=MAX_EVENT_TEXT_LENGTH,
            max_breadcrumbs=0,
            auto_session_tracking=False,
            send_client_reports=False,
            # empty, not unset: unset reads HTTPS_PROXY, which holds the proxy credentials; events go direct
            https_proxy="",
            http_proxy="",
            before_send=partial(scrub_event, secrets=redaction_secrets(settings)),
        )
    except ValueError:
        logger.error("sentry_dsn_invalid")


def frontend_ingest_origin(frontend_dsn: SecretStr | None) -> str | None:
    """the origin the browser SDK posts to, for the CSP; `None` for no DSN or one that is not a public https DSN."""
    if frontend_dsn is None:
        return None

    try:
        parts = urlsplit(frontend_dsn.get_secret_value())
        if parts.scheme == "https" and parts.username and parts.hostname and parts.port is None:
            return bare_https_origin(f"https://{parts.hostname}")
    except ValueError:
        pass

    logger.error("sentry_frontend_dsn_invalid")
    return None
