"""`songlink`: `__NEXT_DATA__` parsing, the 404 vs parse-error distinction, and
429/Retry-After backoff, all via `httpx.MockTransport`, no real network.
"""

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
    """Page structure drift must not masquerade as "not found"."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html></html>"))

    async with make_client(transport) as client:
        with pytest.raises(songlink.SonglinkParseError):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")


def build_page(page_props: str) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        f'{{"props":{{"pageProps":{page_props}}}}}'
        "</script>"
    )


async def test_an_in_page_quota_error_raises_instead_of_caching_emptiness() -> None:
    """Measured live: a quota refusal is HTTP 200 with a `pageProps.error` and a
    stub `pageData`, a transport failure shaped like a positive payload."""
    quota_page = build_page(
        '{"error":{"statusCode":429},"cookies":{},"pageData":{"facebookPixelId":null}}'
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=quota_page))

    async with make_client(transport) as client:
        with pytest.raises(songlink.SonglinkParseError, match="in-page error: 429"):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")


async def test_a_page_without_an_entity_raises_instead_of_a_found_result() -> None:
    """Same class without the `error` key: only HTTP 404 is a verified miss,
    so an unconfirmed empty shape must not be cached as one."""
    empty_page = build_page('{"cookies":{},"pageData":{"facebookPixelId":null}}')
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=empty_page))

    async with make_client(transport) as client:
        with pytest.raises(songlink.SonglinkParseError, match="no entityData"):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")


def test_a_cached_entityless_payload_raises_rather_than_resolving_to_nothing() -> None:
    """A cached row may predate the guard and is served for its TTL; failing
    loudly beats handing the worker an all-`None` result."""
    with pytest.raises(songlink.SonglinkParseError, match="no entityData"):
        songlink._result_from_payload({"found": True, "page_data": {"facebookPixelId": None}})


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


async def test_a_body_over_the_limit_is_refused_while_streaming() -> None:
    """song.link is the one response read as text; its chunked CDN reply has no
    `Content-Length`, and where it has one it counts compressed bytes."""
    oversized = b"x" * (songlink.MAX_RESPONSE_BYTES + 1024)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=oversized))

    async with make_client(transport) as client:
        with pytest.raises(songlink.ResponseTooLargeError):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")


async def test_a_redirect_off_odesli_is_never_requested() -> None:
    """The host check must run before the hop is sent: with `follow_redirects=True`
    httpx would contact the internal address inside `send()`."""
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "song.link":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta"})
        return httpx.Response(200, text=FIXTURE_HTML)

    async with make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(songlink.SonglinkParseError) as raised:
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert "169.254.169.254" in str(raised.value)
    assert requested_hosts == ["song.link"]


async def test_a_redirect_downgraded_to_http_is_refused() -> None:
    """An allowed host over plain http is still a hop we never take."""
    requested_schemes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_schemes.append(request.url.scheme)
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://song.link/s/x"})
        return httpx.Response(200, text=FIXTURE_HTML)

    async with make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(songlink.SonglinkParseError, match="non-https"):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert requested_schemes == ["https"]


async def test_a_redirect_within_odesli_is_allowed() -> None:
    """The `?url=` form lands on `odesli.co`, and song.link 308s to its own
    relative paths — both measured live, both must keep working."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "song.link":
            return httpx.Response(302, headers={"location": "https://odesli.co/s/x"})
        return httpx.Response(200, text=FIXTURE_HTML)

    async with make_client(httpx.MockTransport(handler)) as client:
        payload = await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert payload["found"] is True


async def test_the_measured_live_chain_still_resolves() -> None:
    """Measured 2026-09-20: `308` to a relative path, then `302` to an
    absolute one, then `200` — the shape the manual walk has to reproduce."""
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if len(requested_paths) == 1:
            return httpx.Response(308, headers={"location": "/https:/open.spotify.com/track/x"})
        if len(requested_paths) == 2:
            return httpx.Response(302, headers={"location": "https://song.link/s/x"})
        return httpx.Response(200, text=FIXTURE_HTML)

    async with make_client(httpx.MockTransport(handler)) as client:
        payload = await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert payload["found"] is True
    assert requested_paths[1:] == ["/https:/open.spotify.com/track/x", "/s/x"]


async def test_an_endless_redirect_chain_stops_at_the_hop_limit() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(302, headers={"location": f"https://song.link/s/{request_count}"})

    async with make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(songlink.SonglinkParseError, match="redirected more than"):
            await songlink._fetch_and_parse(client, "https://open.spotify.com/track/x")

    assert request_count == songlink.MAX_REDIRECTS + 1


def test_a_hop_httpx_could_not_build_is_refused() -> None:
    """`next_request` sits beside `has_redirect_location` in httpx, so this
    guard is the one that keeps the walk from assuming a target it lacks."""
    with pytest.raises(songlink.SonglinkParseError, match="without a Location"):
        songlink._validated_redirect_target(httpx.Response(302))
