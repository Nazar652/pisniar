"""`lrclib`: the five answer classes, told apart by the **shape of the body**, the
joined artist string with its `/api/search` fallback, and the two things that must
never be swallowed. Every response is an `httpx.MockTransport`: a live 503 cannot be
reproduced on demand, so the only honest 503 is a mocked one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.lrc import LrcParseError
from pisniar.resolve.sources import lrclib
from pisniar.resolve.sources.lrclib import LrclibApiError, LyricsOutcome

ARTIST_NAMES = ["Burned Time Machine", "0%Mercury"]
JOINED_ARTIST = "Burned Time Machine, 0%Mercury"
TITLE = "Покаяння"
DURATION_MS = 224_000

SYNCED_RECORD: dict[str, object] = {
    "id": 3396226,
    "trackName": TITLE,
    "artistName": JOINED_ARTIST,
    "albumName": TITLE,
    "duration": 224.0,
    "instrumental": False,
    "plainLyrics": "Перший рядок\nДругий рядок",
    "syncedLyrics": "[00:12.50] Перший рядок\n[00:17.20] Другий рядок",
}
PLAIN_ONLY_RECORD: dict[str, object] = {
    **SYNCED_RECORD,
    "syncedLyrics": "",
}
INSTRUMENTAL_RECORD: dict[str, object] = {
    **SYNCED_RECORD,
    "instrumental": True,
    "plainLyrics": "",
    "syncedLyrics": "",
}
EMPTY_POSITIVE_RECORD: dict[str, object] = {
    **SYNCED_RECORD,
    "instrumental": False,
    "plainLyrics": "",
    "syncedLyrics": "",
}

# The literal bodies, re-captured from the container on 2026-09-28: the field is
# `statusCode`, and a fixture keyed on a guessed `code` turns the 404 into an outage.
TRACK_NOT_FOUND_BODY = {
    "message": "Failed to find specified track",
    "name": "TrackNotFound",
    "statusCode": 404,
}
SERVER_OVERLOADED_BODY = {
    "message": "The server is busy, please retry in a moment",
    "name": "ServerOverloaded",
    "statusCode": 503,
}


class RecordingCache:
    """Stands in for `SourceCache`: a miss calls through and what a real cache would
    write stays visible; a raising `fetch()` leaves `stored` empty (the 503 tests)."""

    def __init__(self) -> None:
        self.stored: dict[str, dict[str, object]] = {}
        self.keys: list[str] = []

    async def get_or_fetch(
        self,
        source: str,
        request_key: str,
        fetch: Callable[[], Awaitable[dict[str, object]]],
    ) -> dict[str, object]:
        self.keys.append(request_key)
        if request_key in self.stored:
            return self.stored[request_key]

        payload = await fetch()
        self.stored[request_key] = payload
        return payload


def make_cache() -> tuple[SourceCache, RecordingCache]:
    recording = RecordingCache()
    return cast(SourceCache, recording), recording


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def always(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: response


def by_endpoint(
    get_response: httpx.Response, search_response: httpx.Response
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return search_response if request.url.path.endswith("/search") else get_response

    return handler


async def fetch(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cache: SourceCache | None = None,
    artist_names: list[str] | None = None,
    title: str = TITLE,
    duration_ms: int | None = DURATION_MS,
) -> lrclib.LyricsResult:
    resolved_cache = cache if cache is not None else make_cache()[0]
    async with make_client(handler) as client:
        return await lrclib.fetch_lyrics(
            client,
            resolved_cache,
            artist_names=ARTIST_NAMES if artist_names is None else artist_names,
            title=title,
            duration_ms=duration_ms,
        )


# the five answer classes


async def test_synced_lyrics_are_classified_as_synced_with_absolute_timecodes() -> None:
    result = await fetch(always(httpx.Response(200, json=SYNCED_RECORD)))

    assert result.outcome is LyricsOutcome.SYNCED
    assert result.has_timecodes is True
    assert [(line.start_ms, line.text) for line in result.lines] == [
        (12_500, "Перший рядок"),
        (17_200, "Другий рядок"),
    ]


async def test_plain_lyrics_without_timecodes_are_kept_as_their_own_class() -> None:
    """«Текст є, але без таймкодів — збережено для редактора» is a different
    sentence from «тексту немає», and a different class."""
    result = await fetch(always(httpx.Response(200, json=PLAIN_ONLY_RECORD)))

    assert result.outcome is LyricsOutcome.UNSYNCED
    assert result.has_timecodes is False
    assert [(line.start_ms, line.text) for line in result.lines] == [
        (None, "Перший рядок"),
        (None, "Другий рядок"),
    ]


async def test_instrumental_is_a_normal_state_not_a_failure() -> None:
    result = await fetch(always(httpx.Response(200, json=INSTRUMENTAL_RECORD)))

    assert result.outcome is LyricsOutcome.INSTRUMENTAL
    assert result.lines == ()


async def test_a_documented_404_with_an_empty_search_is_a_cached_miss() -> None:
    cache, recording = make_cache()

    result = await fetch(
        by_endpoint(
            httpx.Response(404, json=TRACK_NOT_FOUND_BODY),
            httpx.Response(200, json=[]),
        ),
        cache=cache,
    )

    assert result.outcome is LyricsOutcome.ABSENT
    assert list(recording.stored.values()) == [{"found": False}]


async def test_an_empty_positive_raises_and_nothing_is_cached() -> None:
    """200, both lyric fields empty, `instrumental` not true: it looks like a valid
    negative and proves nothing, so it must never settle into the cache."""
    cache, recording = make_cache()

    with pytest.raises(LrclibApiError, match="без тексту й без позначки instrumental"):
        await fetch(always(httpx.Response(200, json=EMPTY_POSITIVE_RECORD)), cache=cache)

    assert recording.stored == {}


# "everything else": loud, and never cached


async def test_a_503_raises_after_its_retries_and_nothing_is_cached() -> None:
    """404 is cached, 503 is not: exhausting the retries keeps the class
    «не відповіло», never «не знайдено»."""
    cache, recording = make_cache()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json=SERVER_OVERLOADED_BODY)

    with pytest.raises(LrclibApiError, match="не відповів"):
        await fetch(handler, cache=cache)

    assert attempts == lrclib.MAX_ATTEMPTS
    assert recording.stored == {}


async def test_a_503_that_clears_on_the_first_retry_is_not_an_outage() -> None:
    """Measured 503s clear on the first repeat: they are server load, not a
    property of the request."""
    responses = [
        httpx.Response(503, json=SERVER_OVERLOADED_BODY),
        httpx.Response(200, json=SYNCED_RECORD),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    result = await fetch(handler)

    assert result.outcome is LyricsOutcome.SYNCED
    assert responses == []


async def test_a_404_with_a_non_json_body_raises() -> None:
    """A proxy answered, not the application — so this proves nothing about
    the track and must not be cached as a miss."""
    cache, recording = make_cache()

    with pytest.raises(LrclibApiError, match="не-JSON"):
        await fetch(always(httpx.Response(404, text="<html>not found</html>")), cache=cache)

    assert recording.stored == {}


async def test_a_404_with_an_undocumented_body_raises() -> None:
    """The whitelist is a whitelist: a 404 naming a different error is an
    unverified combination, not a second way of saying "not found"."""
    cache, recording = make_cache()
    body = {"message": "?", "name": "SomethingElse", "statusCode": 404}

    with pytest.raises(LrclibApiError, match="недокументованим тілом"):
        await fetch(always(httpx.Response(404, json=body)), cache=cache)

    assert recording.stored == {}


async def test_the_miss_is_read_off_status_code_not_a_guessed_code_field() -> None:
    """The live 404 has no `code`; keying on it would turn every real miss into
    «не відповіло» and skip the search fallback."""
    body = {"message": "Failed to find specified track", "name": "TrackNotFound", "code": 404}
    cache, recording = make_cache()

    with pytest.raises(LrclibApiError, match="недокументованим тілом"):
        await fetch(always(httpx.Response(404, json=body)), cache=cache)

    assert recording.stored == {}


async def test_an_unexpected_status_raises() -> None:
    cache, recording = make_cache()

    with pytest.raises(LrclibApiError, match="HTTP 403"):
        await fetch(always(httpx.Response(403, json={"code": 403})), cache=cache)

    assert recording.stored == {}


async def test_a_timeout_raises_after_its_retries_and_nothing_is_cached() -> None:
    cache, recording = make_cache()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("lrclib did not answer")

    with pytest.raises(LrclibApiError, match="не відповів"):
        await fetch(handler, cache=cache)

    assert attempts == lrclib.MAX_ATTEMPTS
    assert recording.stored == {}


async def test_a_payload_that_is_not_lrc_raises_and_nothing_is_cached() -> None:
    """The parser drops one crooked line with a WARN; a whole payload that
    does not parse is a different format, and that is loud."""
    cache, recording = make_cache()
    record = {**SYNCED_RECORD, "syncedLyrics": "не таймкод\nтеж ні\nі це\nі це теж\nі ще"}

    with pytest.raises(LrcParseError):
        await fetch(always(httpx.Response(200, json=record)), cache=cache)

    assert recording.stored == {}


async def test_a_record_cached_before_the_check_existed_still_raises_on_the_read_path() -> None:
    """The second barrier, like `songlink._require_entity_data`: the shape is
    re-judged after the cache, so an older row cannot be served as an answer."""
    cache, recording = make_cache()
    recording.stored["lrclib:lyrics:burned time machine, 0%mercury|покаяння"] = {
        "found": True,
        "record": EMPTY_POSITIVE_RECORD,
    }

    with pytest.raises(LrclibApiError, match="без тексту й без позначки instrumental"):
        await fetch(always(httpx.Response(500)), cache=cache)


# the joined artist string and the search fallback


def test_artist_names_are_joined_with_a_comma() -> None:
    assert lrclib.join_artist_names(ARTIST_NAMES) == JOINED_ARTIST


def test_joining_skips_blank_names_and_collapses_whitespace() -> None:
    assert lrclib.join_artist_names(["  Тол  ", "", "   "]) == "Тол"


async def test_the_request_carries_the_joined_artist_string_not_the_first_name() -> None:
    """Measured live: `artist_name=Burned Time Machine` answers 404, while
    `Burned Time Machine, 0%Mercury` answers 200 with 1312 synced characters."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SYNCED_RECORD)

    await fetch(handler)

    assert requests[0].url.params["artist_name"] == JOINED_ARTIST
    assert requests[0].url.params["track_name"] == TITLE


async def test_a_404_falls_back_to_search_before_calling_the_miss_final() -> None:
    """`404 TrackNotFound` only proves LRCLIB has nothing under the exact
    string we sent — our artists are separate rows, theirs is one string."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[SYNCED_RECORD])
        return httpx.Response(404, json=TRACK_NOT_FOUND_BODY)

    result = await fetch(handler)

    assert [request.url.path for request in requests] == ["/api/get", "/api/search"]
    assert result.outcome is LyricsOutcome.SYNCED


async def test_the_search_fallback_asks_by_title_alone() -> None:
    """An `artist_name` filter here would reproduce the exact-string mismatch
    the fallback exists to recover from."""
    search_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            search_requests.append(request)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json=TRACK_NOT_FOUND_BODY)

    await fetch(handler)

    assert dict(search_requests[0].url.params) == {"track_name": TITLE}


async def test_the_search_fallback_keeps_only_the_row_that_is_ours() -> None:
    """Measured live: a common title answers 20 foreign songs, so the
    verification, not the search, is what makes the fallback safe."""
    foreign_rows = [{**SYNCED_RECORD, "artistName": f"Хтось інший {index}"} for index in range(19)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[*foreign_rows, SYNCED_RECORD])
        return httpx.Response(404, json=TRACK_NOT_FOUND_BODY)

    result = await fetch(handler)

    assert result.outcome is LyricsOutcome.SYNCED


async def test_a_search_row_without_any_lyrics_is_skipped_not_raised_on() -> None:
    """An empty row among search results is an answer about some other track, not
    an empty positive; only `/api/get` answers our question."""
    cache, recording = make_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[EMPTY_POSITIVE_RECORD])
        return httpx.Response(404, json=TRACK_NOT_FOUND_BODY)

    result = await fetch(handler, cache=cache)

    assert result.outcome is LyricsOutcome.ABSENT
    assert list(recording.stored.values()) == [{"found": False}]


# belonging: not verified means not written at all


async def test_a_record_for_a_different_track_is_rejected_and_carries_no_lines() -> None:
    record = {**SYNCED_RECORD, "trackName": "Зовсім інша пісня"}

    result = await fetch(always(httpx.Response(200, json=record)))

    assert result.outcome is LyricsOutcome.REJECTED_BY_MATCHING
    assert result.lines == ()


async def test_an_empty_artist_name_is_rejected_by_a_branch_of_its_own() -> None:
    """`is_match` reads an empty `artistName` as no match anyway, so without this
    branch a truncated answer would look like a foreign one."""
    record = {**SYNCED_RECORD, "artistName": ""}

    result = await fetch(always(httpx.Response(200, json=record)))

    assert result.outcome is LyricsOutcome.REJECTED_BY_MATCHING


async def test_a_duration_outside_the_tolerance_is_rejected() -> None:
    record = {**SYNCED_RECORD, "duration": 300.0}

    result = await fetch(always(httpx.Response(200, json=record)))

    assert result.outcome is LyricsOutcome.REJECTED_BY_MATCHING


async def test_a_duration_inside_the_tolerance_is_accepted() -> None:
    record = {**SYNCED_RECORD, "duration": 225.5}

    result = await fetch(always(httpx.Response(200, json=record)))

    assert result.outcome is LyricsOutcome.SYNCED


async def test_a_track_we_know_no_duration_for_is_still_matched_on_names() -> None:
    result = await fetch(always(httpx.Response(200, json=SYNCED_RECORD)), duration_ms=None)

    assert result.outcome is LyricsOutcome.SYNCED


# what must never be swallowed


async def test_a_soft_time_limit_is_not_swallowed_by_the_retry_loop() -> None:
    """Celery raises it as an ordinary exception; `except httpx.HTTPError` is narrow
    so a timeout is never retried and reported as «джерело не відповіло»."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise SoftTimeLimitExceeded

    with pytest.raises(SoftTimeLimitExceeded):
        await fetch(handler)

    assert attempts == 1


async def test_a_database_failure_inside_the_cache_is_not_swallowed() -> None:
    """`SourceCache` talks to Postgres inside this call — a dead database is
    infrastructure, never «тексту немає»."""

    class FailingCache:
        async def get_or_fetch(
            self,
            source: str,
            request_key: str,
            fetch: Callable[[], Awaitable[dict[str, object]]],
        ) -> dict[str, object]:
            raise SQLAlchemyError("database is down")

    with pytest.raises(SQLAlchemyError):
        await fetch(
            always(httpx.Response(200, json=SYNCED_RECORD)), cache=cast(SourceCache, FailingCache())
        )


# the query itself


async def test_an_incomplete_query_never_touches_the_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made without an artist and a title")

    result = await fetch(handler, artist_names=[], title=TITLE)

    assert result.outcome is LyricsOutcome.ABSENT


async def test_the_cache_key_ignores_case_but_not_the_composition_of_the_string() -> None:
    """LRCLIB ignores case but compares the composition of the string, so one
    artist and two must never share a cache row."""
    cache, recording = make_cache()
    handler = always(httpx.Response(200, json=SYNCED_RECORD))

    await fetch(handler, cache=cache)
    await fetch(handler, cache=cache, artist_names=[name.upper() for name in ARTIST_NAMES])
    await fetch(handler, cache=cache, artist_names=ARTIST_NAMES[:1])

    assert recording.keys[0] == recording.keys[1]
    assert recording.keys[2] != recording.keys[0]


async def test_the_outcome_log_tells_a_cached_answer_from_a_live_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post's history must show whether LRCLIB was asked; a stand-in logger,
    because a cached structlog proxy defeats `capture_logs()`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(lrclib, "logger", fake_logger)
    cache, _ = make_cache()
    handler = always(httpx.Response(200, json=SYNCED_RECORD))

    await fetch(handler, cache=cache)
    await fetch(handler, cache=cache)

    outcome_calls = [
        call.kwargs
        for call in fake_logger.info.call_args_list
        if call.args == ("lrclib_lyrics_outcome",)
    ]
    assert [(call["outcome"], call["from_cache"]) for call in outcome_calls] == [
        (LyricsOutcome.SYNCED.value, False),
        (LyricsOutcome.SYNCED.value, True),
    ]


# no database session spans a request


async def test_no_database_session_is_open_while_the_request_is_in_flight() -> None:
    """Asserted directly: the real `SourceCache` runs on a counting session factory,
    and the HTTP handler checks that no session is open during the request."""
    open_sessions = 0
    open_during_request: list[int] = []

    @asynccontextmanager
    async def session_scope() -> AsyncIterator[AsyncSession]:
        nonlocal open_sessions
        session = AsyncMock(spec=AsyncSession)
        session.scalar = AsyncMock(return_value=None)
        open_sessions += 1
        try:
            yield session
        finally:
            open_sessions -= 1

    def handler(request: httpx.Request) -> httpx.Response:
        open_during_request.append(open_sessions)
        return httpx.Response(200, json=SYNCED_RECORD)

    cache = SourceCache(session_factory=cast(Any, session_scope))
    result = await fetch(handler, cache=cache)

    assert result.outcome is LyricsOutcome.SYNCED
    assert open_during_request == [0]
    assert open_sessions == 0
