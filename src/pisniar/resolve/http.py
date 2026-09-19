"""Shared HTTP client construction and per-source throttling (contract
p.14): each adapter gets its own `Throttler` instance so a slow MusicBrainz
call never delays a Deezer call.
"""

from __future__ import annotations

import asyncio
import time

import httpx

TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResponseTooLargeError(httpx.HTTPError):
    """An advertised body larger than `MAX_RESPONSE_BYTES` — refused before
    it is read, so a hostile or broken source can't exhaust worker memory."""


async def _reject_oversized_response(response: httpx.Response) -> None:
    content_length = response.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > MAX_RESPONSE_BYTES
    ):
        raise ResponseTooLargeError(
            f"response body {content_length} bytes exceeds {MAX_RESPONSE_BYTES}"
        )


def build_http_client() -> httpx.AsyncClient:
    """The one client every source adapter shares for a single resolve."""
    return httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        max_redirects=MAX_REDIRECTS,
        event_hooks={"response": [_reject_oversized_response]},
    )


class Throttler:
    """Serializes calls to at least `min_interval_seconds` apart. A cache
    hit never calls `wait()`, so this only ever limits genuinely new
    requests (contract p.14: "Кеш робить повторні запити неможливими")."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.last_call_at: float | None = None
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.last_call_at is not None:
                remaining = self.min_interval_seconds - (now - self.last_call_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self.last_call_at = time.monotonic()


def parse_retry_after_seconds(header_value: str | None) -> float | None:
    """`Retry-After` is either a delta-seconds integer or an HTTP-date; only
    the common delta-seconds form is supported, the rest fall back to `None`."""
    if header_value is None:
        return None
    try:
        return float(header_value)
    except ValueError:
        return None
