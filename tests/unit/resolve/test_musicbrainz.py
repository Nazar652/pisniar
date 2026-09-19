"""`musicbrainz`: disabled without a contact (D-30/gate 2.0.4), url-rels ->
platform_links mapping — via `httpx.MockTransport`, trimmed real shape."""

from __future__ import annotations

import httpx

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
