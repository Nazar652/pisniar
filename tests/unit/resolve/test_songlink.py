"""`songlink`: `__NEXT_DATA__` parsing, 404 vs parse-error distinction (R2),
429/Retry-After backoff — all via `httpx.MockTransport`, no real network."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pisniar.resolve.sources import songlink

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "songlink_track.html").read_text(
    encoding="utf-8"
)


def make_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_fetch_and_parse_returns_found_payload_for_a_real_page_shape() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=FIXTURE_HTML))

    async with make_client(transport) as client:
        payload = await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert payload["found"] is True
    page_data = payload["page_data"]
    assert isinstance(page_data, dict)
    assert page_data["entityData"]["isrc"] == "GBARL9300135"


async def test_fetch_and_parse_returns_not_found_on_404() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    async with make_client(transport) as client:
        payload = await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert payload == {"found": False}


async def test_fetch_and_parse_raises_parse_error_on_missing_next_data_script() -> None:
    """Page structure drift (R2) must not masquerade as "not found"."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html></html>"))

    async with make_client(transport) as client:
        with pytest.raises(songlink.SonglinkParseError):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")


async def test_fetch_and_parse_retries_after_429_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text=FIXTURE_HTML)

    transport = httpx.MockTransport(handler)
    async with make_client(transport) as client:
        payload = await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert attempts == 2
    assert payload["found"] is True


async def test_fetch_and_parse_gives_up_after_max_retries() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "0"})
    )

    async with make_client(transport) as client:
        with pytest.raises(songlink.SonglinkParseError, match="rate-limiting"):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")


def test_result_from_payload_extracts_spotify_entity_fields() -> None:
    payload = songlink._parse_page(FIXTURE_HTML)

    result = songlink._result_from_payload(payload)

    assert result.found is True
    assert result.provider == "spotify"
    assert result.isrc == "GBARL9300135"
    assert result.title == "Never Gonna Give You Up"
    assert result.artist_name == "Rick Astley"
    assert result.duration_ms == 213573
    assert result.album_id_spotify == "6N9PS4QXF1D0OWPk0Sxtb4"
    assert result.year == 1987


def test_result_from_payload_only_includes_links_with_a_url() -> None:
    payload = songlink._parse_page(FIXTURE_HTML)

    result = songlink._result_from_payload(payload)

    # "appleMusic"/"youtube"/"youtubeMusic" appear in the fixture without a
    # url (Odesli has no link for them) and must not show up here.
    assert result.platform_links == {
        "spotify": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "deezer": "https://www.deezer.com/track/781592622",
        "tidal": "https://listen.tidal.com/track/491206012",
        "amazonStore": "https://amazon.com/dp/B0DZTSF3YS",
    }


def test_result_from_payload_returns_not_found_result() -> None:
    result = songlink._result_from_payload({"found": False})

    assert result.found is False
    assert result.platform_links == {}


def test_album_id_only_set_for_spotify_provider() -> None:
    payload: dict[str, object] = {
        "found": True,
        "page_data": {
            "entityData": {"provider": "youtube", "albumId": "should-be-ignored"},
            "sections": [],
        },
    }

    result = songlink._result_from_payload(payload)

    assert result.album_id_spotify is None
