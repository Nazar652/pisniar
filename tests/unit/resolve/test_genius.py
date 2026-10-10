"""`genius`: the page parser on an untrusted HTML shape modelled on a real song page, the
search-hit checks (our track, not a translation, a path that cannot leave `genius.com`)
and what must never be cached. Every response is an `httpx.MockTransport`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import genius
from pisniar.resolve.sources.genius import GeniusApiError
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome

ARTISTS = ["Робаний Йот"]
TITLE = "Стеля"
PAGE_PATH = "/Robanyi-yot-ceiling-lyrics"

PAGE_HTML = """
<html><head><title>Стеля Lyrics</title><script>var lyrics = "not a lyric";</script></head>
<body><div class="Lyrics__Root">
<div data-lyrics-container="true" class="Lyrics__Container">
<div data-exclude-from-selection="true" class="Header"><button>1 Contributor</button>
<h2>Стеля (Ceiling) Lyrics</h2></div>[Текст пісні «Стеля»]<br/><br/>[Куплет 1: Робаний Йот]<br/>
Перший <a href="/annotation/1"><span>рядок</span></a><br/>Другий &amp; рядок<br/>
<div data-exclude-from-selection="true"><div>You might also like</div></div>
<i>Третій</i><br/><script>alert("x")</script><svg><text>icon</text></svg></div>
<div data-lyrics-container="true">[Приспів]<br/>Четвертий<br/></div>
</div><div>Footer text</div></body></html>
"""
EXPECTED_LINES = ("Перший рядок", "Другий & рядок", "Третій", "Четвертий")


def hit(**overrides: object) -> dict[str, Any]:
    result = {
        "path": PAGE_PATH,
        "title": "Стеля (Ceiling)",
        "artist_names": "Робаний Йот (Robanyi Yot)",
        "primary_artists": [{"name": "Робаний Йот (Robanyi Yot)"}],
        "lyrics_state": "complete",
        "instrumental": False,
        **overrides,
    }
    return {"result": result}


def search_body(*hits: dict[str, Any]) -> dict[str, Any]:
    return {"response": {"sections": [{"type": "song", "hits": list(hits)}]}}


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def site(
    search: dict[str, Any] | None = None, page: str = PAGE_HTML
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search/song":
            return httpx.Response(200, json=search if search is not None else search_body(hit()))
        return httpx.Response(200, text=page)

    return handler


def test_the_page_parser_keeps_lyric_text_and_drops_labels_ui_and_scripts() -> None:
    assert genius.lyrics_lines_from_html(PAGE_HTML) == EXPECTED_LINES


def test_a_page_without_a_lyrics_container_has_no_lines() -> None:
    assert genius.lyrics_lines_from_html("<html><body><p>Тільки текст</p></body></html>") == ()


def test_the_parser_ignores_an_unbalanced_close_tag() -> None:
    page = '</div></div><div data-lyrics-container="true">Рядок<br/></div>'

    assert genius.lyrics_lines_from_html(page) == ("Рядок",)


def test_the_parser_collects_only_while_inside_a_container() -> None:
    parser = genius.LyricsHtmlParser()
    parser.feed("<p>поза</p>")

    assert parser.collecting is False
    assert parser.chunks == []

    parser.feed('<div data-lyrics-container="true">всередині')
    assert parser.collecting is True

    parser.feed('<div data-exclude-from-selection="true">зайве')
    assert parser.collecting is False
    assert "зайве" not in parser.chunks


def test_a_self_closing_container_tag_does_not_leave_the_parser_inside() -> None:
    parser = genius.LyricsHtmlParser()
    parser.feed('<div data-lyrics-container="true"/>після')

    assert parser.collecting is False


@pytest.mark.parametrize(
    ("value", "stripped"),
    [
        ("Стеля (Ceiling)", "Стеля"),
        ("хейтспіч (hatespeech) [UKR]", "хейтспіч"),
        ("A (x) B [y]", "A B"),
        ("Без позначок", "Без позначок"),
    ],
)
def test_annotations_are_stripped_from_a_name(value: str, stripped: str) -> None:
    assert genius.strip_annotations(value) == stripped


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (("/Genius-ukrainian-translations-x-lyrics", "x", "y"), True),
        (("/a-lyrics", "Назва (Переклад)", "y"), True),
        (("/a-lyrics", "x", "Genius Traducciones"), True),
        (("/a-lyrics", "Стеля", "Робаний Йот"), False),
    ],
)
def test_translation_pages_are_recognised(fields: tuple[str, str, str], expected: bool) -> None:
    assert genius.looks_like_translation(*fields) is expected


def test_the_result_of_the_search_is_our_track() -> None:
    assert (
        genius.path_if_ours(
            hit()["result"], artist_query="Робаний Йот", title=TITLE, duration_ms=158_000
        )
        == PAGE_PATH
    )


def test_a_duo_page_is_ours_through_the_primary_artist() -> None:
    result = hit(
        artist_names="хейтспіч (hatespeech) [UKR] & СТРУКТУРА ЩАСТЯ (STRUCTURA SHYASTYA)",
        primary_artists=[
            {"name": "хейтспіч (hatespeech) [UKR]"},
            {"name": "СТРУКТУРА ЩАСТЯ (STRUCTURA SHYASTYA)"},
        ],
        title="День мій ворог (Day is My Enemy)",
    )["result"]

    assert (
        genius.path_if_ours(
            result, artist_query="хейтспіч", title="День мій ворог", duration_ms=195_941
        )
        == PAGE_PATH
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "https://evil.example/x-lyrics"},
        {"path": "/../../etc-lyrics"},
        {"path": "/a/b-lyrics"},
        {"path": "/x-lyrics?next=http://evil.example"},
        {"path": "/page-about"},
        {"path": "/x-lyrics\n"},
        {"path": None},
        {"lyrics_state": "unreleased"},
        {"instrumental": True},
        {"title": "Інша пісня"},
        {"artist_names": "Хтось інший", "primary_artists": [{"name": "Хтось інший"}]},
        {"path": "/Genius-ukrainian-translations-ceiling-lyrics"},
        {"title": "Стеля (Ukrainian Translation)"},
    ],
)
def test_a_hit_that_is_not_our_track_or_has_a_hostile_path_is_refused(
    overrides: dict[str, object],
) -> None:
    result = hit(**overrides)["result"]

    assert (
        genius.path_if_ours(result, artist_query="Робаний Йот", title=TITLE, duration_ms=None)
        is None
    )


async def test_a_search_picks_the_first_hit_that_is_ours() -> None:
    body = search_body(hit(title="Не та"), hit(path="/second-lyrics"), hit())
    async with client_for(site(body)) as client:
        path = await genius.search_for_path(
            client, artist_query="Робаний Йот", title=TITLE, duration_ms=None
        )

    assert path == "/second-lyrics"


async def test_a_search_with_no_hit_of_ours_finds_nothing() -> None:
    async with client_for(site(search_body(hit(title="Не та")))) as client:
        path = await genius.search_for_path(
            client, artist_query="Робаний Йот", title=TITLE, duration_ms=None
        )

    assert path is None


async def test_a_search_without_sections_finds_nothing() -> None:
    async with client_for(site({"response": {"sections": []}})) as client:
        path = await genius.search_for_path(
            client, artist_query="Робаний Йот", title=TITLE, duration_ms=None
        )

    assert path is None


async def test_the_search_is_one_get_with_the_artist_and_title_as_the_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=search_body())

    async with client_for(handler) as client:
        await genius.search_for_path(
            client, artist_query="Робаний Йот", title=TITLE, duration_ms=None
        )

    assert len(seen) == 1
    assert seen[0].url.host == "genius.com"
    assert seen[0].url.params["q"] == "Робаний Йот Стеля"
    assert seen[0].headers["user-agent"] == genius.REQUEST_HEADERS["User-Agent"]


async def test_a_non_json_search_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>blocked</html>")

    async with client_for(handler) as client:
        with pytest.raises(GeniusApiError, match="не-JSON"):
            await genius.search_for_path(
                client, artist_query="Робаний Йот", title=TITLE, duration_ms=None
            )


@pytest.mark.parametrize("status_code", [403, 404, 429, 500, 503])
async def test_a_status_that_is_not_200_raises(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="no")

    async with client_for(handler) as client:
        with pytest.raises(GeniusApiError, match=str(status_code)):
            await genius.send_request(client, genius.SEARCH_URL)


async def test_a_redirect_is_an_error_and_is_never_followed() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    async with client_for(handler) as client:
        with pytest.raises(GeniusApiError, match="302"):
            await genius.send_request(client, genius.SEARCH_URL)

    assert requests == [genius.SEARCH_URL]


async def test_a_body_past_the_ceiling_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genius, "MAX_PAGE_BYTES", 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 11)

    async with client_for(handler) as client:
        with pytest.raises(GeniusApiError, match="байтів"):
            await genius.send_request(client, genius.SEARCH_URL)


async def test_a_streamed_body_without_a_length_is_cut_off_past_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(genius, "MAX_PAGE_BYTES", 10)
    sent: list[bytes] = []

    async def endless() -> AsyncIterator[bytes]:
        for _ in range(1_000):
            sent.append(b"xxxx")
            yield b"xxxx"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless())

    async with client_for(handler) as client:
        with pytest.raises(GeniusApiError, match="понад"):
            await genius.send_request(client, genius.SEARCH_URL)

    assert len(sent) < 1_000


async def test_a_streamed_body_within_the_ceiling_is_returned_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(genius, "MAX_PAGE_BYTES", 10)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"abcde"
        yield b"fghij"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    async with client_for(handler) as client:
        assert await genius.send_request(client, genius.SEARCH_URL) == b"abcdefghij"


async def test_the_page_is_fetched_from_genius_by_the_validated_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=PAGE_HTML)

    async with client_for(handler) as client:
        page = await genius.fetch_page(client, PAGE_PATH)

    assert page == PAGE_HTML
    assert seen == [f"https://genius.com{PAGE_PATH}"]


async def test_a_found_page_is_plain_lines(fake_cache: SourceCache) -> None:
    async with client_for(site()) as client:
        result = await genius.fetch_lyrics(
            client, fake_cache, artist_names=ARTISTS, title=TITLE, duration_ms=158_000
        )

    assert result.outcome is LyricsOutcome.UNSYNCED
    assert result.lines == tuple(LyricsLine(start_ms=None, text=text) for text in EXPECTED_LINES)
    cache_mock: Any = fake_cache
    source, request_key, _ = cache_mock.get_or_fetch.await_args.args
    assert (source, request_key) == ("genius", "genius:lyrics:робаний йот|стеля")


async def test_no_matching_hit_is_absent(fake_cache: SourceCache) -> None:
    async with client_for(site(search_body())) as client:
        result = await genius.fetch_lyrics(
            client, fake_cache, artist_names=ARTISTS, title=TITLE, duration_ms=None
        )

    assert result.outcome is LyricsOutcome.ABSENT


async def test_a_page_without_lyrics_raises_and_so_is_never_cached(
    fake_cache: SourceCache,
) -> None:
    async with client_for(site(page="<html><body>captcha</body></html>")) as client:
        with pytest.raises(GeniusApiError, match="немає тексту"):
            await genius.fetch_lyrics(
                client, fake_cache, artist_names=ARTISTS, title=TITLE, duration_ms=None
            )


@pytest.mark.parametrize(("artist_names", "title"), [([], TITLE), (ARTISTS, "  "), ([" "], TITLE)])
async def test_an_incomplete_query_is_absent_without_a_request(
    fake_cache: SourceCache, artist_names: list[str], title: str
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made")

    async with client_for(refuse) as client:
        result = await genius.fetch_lyrics(
            client, fake_cache, artist_names=artist_names, title=title, duration_ms=None
        )

    assert result.outcome is LyricsOutcome.ABSENT


def test_a_negative_payload_is_absent() -> None:
    assert genius.result_from_payload({"found": False}).outcome is LyricsOutcome.ABSENT


def test_cached_lines_come_back_as_plain_lines() -> None:
    result = genius.result_from_payload({"found": True, "lines": ["раз", "два"]})

    assert result.outcome is LyricsOutcome.UNSYNCED
    assert [line.text for line in result.lines] == ["раз", "два"]


@pytest.mark.parametrize(
    "payload",
    [
        {"found": True},
        {"found": True, "lines": []},
        {"found": True, "lines": ["раз", 5]},
        {"found": True, "lines": ["раз", "  "]},
        {"found": True, "lines": "text"},
    ],
)
def test_a_cached_row_of_the_wrong_shape_raises(payload: dict[str, object]) -> None:
    with pytest.raises(GeniusApiError):
        genius.result_from_payload(payload)
