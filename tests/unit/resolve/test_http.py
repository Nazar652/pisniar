"""`http.build_http_client`: the shared client's redirect cap and response size
ceiling, plus the timeout every adapter inherits.
"""

from __future__ import annotations

import httpx
import pytest

from pisniar.resolve import http as resolve_http
from pisniar.resolve.http import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    TIMEOUT_SECONDS,
    ResponseTooLargeError,
    build_http_client,
    parse_retry_after_seconds,
)


async def test_client_carries_timeout_redirect_cap_and_size_hook() -> None:
    async with build_http_client() as client:
        assert client.timeout.connect == TIMEOUT_SECONDS
        assert client.max_redirects == MAX_REDIRECTS
        assert resolve_http.reject_oversized_response in client.event_hooks["response"]


async def test_oversized_advertised_body_is_refused() -> None:
    """the hook runs on headers, so a hostile `Content-Length` costs no
    memory — the body is never downloaded at all."""
    response = httpx.Response(
        200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, content=b""
    )

    with pytest.raises(ResponseTooLargeError):
        await resolve_http.reject_oversized_response(response)


async def test_normal_sized_body_passes_the_hook() -> None:
    response = httpx.Response(200, json={"ok": True})

    await resolve_http.reject_oversized_response(response)


async def test_missing_content_length_is_not_treated_as_oversized() -> None:
    response = httpx.Response(200, headers={"Transfer-Encoding": "chunked"})

    await resolve_http.reject_oversized_response(response)


def test_retry_after_parsing_falls_back_to_none_for_an_http_date() -> None:
    assert parse_retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after_seconds("12") == 12.0
