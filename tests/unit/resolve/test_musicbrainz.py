"""`musicbrainz`: disabled without a contact, and url-rels -> platform_links
mapping, via `httpx.MockTransport` and a trimmed real shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import musicbrainz

RECORDING: dict[str, object] = {
    "id": "recording-123",
    "relations": [
        {"type": "free streaming", "url": {"resource": "https://open.spotify.com/track/x"}},
        {"type": "free streaming", "url": {"resource": "https://www.deezer.com/track/x"}},
        {"type": "purchase for download", "url": {"resource": "https://example.com/x"}},
    ],
}
RECORDING_FOUND_BODY: dict[str, object] = {"recordings": [RECORDING]}


async def test_fetch_by_isrc_is_disabled_without_a_contact(fake_cache: SourceCache) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=RECORDING_FOUND_BODY))

    async with httpx.AsyncClient(transport=transport) as client:
        result = await musicbrainz.fetch_by_isrc(client, fake_cache, "GBAYE9200070", None)

    assert result.found is False


async def test_fetch_by_isrc_sends_contact_in_user_agent(fake_cache: SourceCache) -> None:
    captured_user_agent = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_user_agent
        captured_user_agent = request.headers.get("User-Agent")
        return httpx.Response(200, json=RECORDING_FOUND_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await musicbrainz.fetch_by_isrc(client, fake_cache, "GBAYE9200070", "dev@example.com")

    assert captured_user_agent is not None
    assert "dev@example.com" in captured_user_agent


async def test_fetch_by_isrc_returns_not_found_on_404(fake_cache: SourceCache) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    async with httpx.AsyncClient(transport=transport) as client:
        result = await musicbrainz.fetch_by_isrc(
            client, fake_cache, "GBAYE9200070", "dev@example.com"
        )

    assert result.found is False


def test_result_from_payload_maps_known_hosts_to_platforms() -> None:
    result = musicbrainz._result_from_payload({"found": True, "data": RECORDING})

    assert result.found is True
    assert result.recording_id == "recording-123"
    assert result.platform_links == {
        "spotify": "https://open.spotify.com/track/x",
        "deezer": "https://www.deezer.com/track/x",
    }


def test_result_from_payload_ignores_unrecognized_hosts() -> None:
    result = musicbrainz._result_from_payload({"found": True, "data": RECORDING})

    assert "example.com" not in str(result.platform_links)


def test_a_broken_url_rel_is_skipped_instead_of_failing_the_whole_recording() -> None:
    """`httpx.URL()` raises on a control character, and one bad url-rel in a publicly
    editable database must not fail the good links next to it."""
    recording: dict[str, object] = {
        "id": "recording-456",
        "relations": [
            {"url": {"resource": "https://open.spotify.com/track/ok"}},
            {"url": {"resource": "https://exa	mple.com/x"}},
            {"url": {"resource": "http://xn--a.com/x"}},
            {"url": {"resource": "https://[::zz]/x"}},
            {"url": {"resource": "https://www.deezer.com/track/ok"}},
        ],
    }

    result = musicbrainz._result_from_payload({"found": True, "data": recording})

    assert result.platform_links == {
        "spotify": "https://open.spotify.com/track/ok",
        "deezer": "https://www.deezer.com/track/ok",
    }


def test_an_unparsable_url_rel_is_logged_without_its_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger_mock = MagicMock()
    monkeypatch.setattr(musicbrainz, "logger", logger_mock)

    assert musicbrainz._host_or_none("https://exa\tmple‮.com/x") is None

    logged_url = logger_mock.warning.call_args.kwargs["url"]
    assert "\t" not in logged_url
    assert "‮" not in logged_url
