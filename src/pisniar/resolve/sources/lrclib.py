"""LRCLIB (step 4B, D-88): the one lyrics source, and the adapter whose
answer is classified by the **shape of the body**, never by the status code
(D-89). Five classes: synced, unsynced, instrumental, a documented miss —
and "everything else", which raises and is therefore never cached.

The dangerous shape is the *empty positive*: HTTP 200 where both lyric
fields are empty and `instrumental` is not `true`. It looks valid and empty
while meaning nothing at all — the literal form of D-48, where a quota
refusal settled into the cache for an hour and R2-5 could not see it because
the payload looked positive. It raises.

`404 TrackNotFound` is **not** proof that the track has no lyrics (D-90):
LRCLIB stores one joined artist string and compares it exactly, so a miss
may only mean our string was not theirs. Hence the joined query and the
`/api/search` fallback before a miss is called final.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import httpx

from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import Throttler
from pisniar.resolve.json_utils import as_dict, as_list, as_str
from pisniar.resolve.lrc import parse_lrc
from pisniar.resolve.matching import is_match

logger = get_logger(__name__)

# Its own instance, like every other adapter's: a slow LRCLIB call must not
# delay a Deezer one.
THROTTLE = Throttler(min_interval_seconds=1.0)
SOURCE_NAME = "lrclib"

GET_URL = "https://lrclib.net/api/get"
SEARCH_URL = "https://lrclib.net/api/search"

# LRCLIB asks clients to identify themselves; same shape as `musicbrainz`.
REQUEST_HEADERS = {"User-Agent": "pisniar-v2/0.1 (personal Telegram music bot)"}

# D-90, a sixth of the coverage on one line: our artists are separate rows,
# LRCLIB keeps one joined string and compares it exactly (case-insensitively).
# `artists[0].name` measured 4/9 where the joined string measured 5/9.
ARTIST_JOIN_SEPARATOR = ", "

# The whitelist, built from the literal bodies gate 4B.0.4 recorded: this is
# the *only* shape that proves "not found". Deezer's `NOT_FOUND_ERROR_CODES`
# precedent — anything outside the list is undocumented and therefore loud,
# a proxy's HTML 404 included.
NOT_FOUND_STATUS_CODE = 404
NOT_FOUND_ERROR_NAMES = frozenset({"TrackNotFound"})

# D-91: the "stable 503" of spike 0e was server load, not a property of the
# request — every observed one cleared on the first retry. The backoff sits
# *before* classification and covers only the "did not answer" class;
# exhausting it stays "did not answer" and never becomes "not found".
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0

# `/api/search` answers up to 20 foreign rows for a common title (measured on
# spike 0e: "Berserker", "Сила"), every one of them verified before use.
MAX_SEARCH_CANDIDATES = 20


class LyricsOutcome(StrEnum):
    """The five reason classes of §10, spelled the way the log says them.

    `SOURCE_UNAVAILABLE` is the one outcome this adapter never returns — it
    is what `LrclibApiError` means, and the caller names it from the
    exception (§6: "не відповів" must never read as "немає").
    """

    SYNCED = "lyrics_synced"
    UNSYNCED = "lyrics_unsynced"
    INSTRUMENTAL = "lyrics_instrumental"
    ABSENT = "lyrics_absent"
    REJECTED_BY_MATCHING = "lyrics_rejected_by_matching"
    SOURCE_UNAVAILABLE = "lyrics_source_unavailable"


class LrclibApiError(RuntimeError):
    """A response whose shape does not prove anything — 5xx, a timeout, a
    non-JSON 404, an undocumented 404 body, or the empty positive. Raised
    rather than cached, exactly like `DeezerApiError` and
    `SonglinkParseError`: caching an outage as "no lyrics" would be S4B-R4.
    """

    outcome = LyricsOutcome.SOURCE_UNAVAILABLE


@dataclass(frozen=True)
class LyricsLine:
    """One line as the source gave it. `start_ms` is absolute from the start
    of the *full* track (D-5) and `None` for a plain-text line — the shape
    step 7's editor fills in."""

    start_ms: int | None
    text: str


@dataclass(frozen=True)
class LyricsResult:
    outcome: LyricsOutcome
    lines: tuple[LyricsLine, ...] = ()

    @property
    def has_timecodes(self) -> bool:
        return self.outcome is LyricsOutcome.SYNCED


def join_artist_names(artist_names: Sequence[str]) -> str:
    """D-90: one comma-joined string, because that is the single value
    LRCLIB compares against. `Burned Time Machine, 0%Mercury` answers 200
    where either half alone answers 404."""
    return ARTIST_JOIN_SEPARATOR.join(
        " ".join(name.split()) for name in artist_names if name and name.strip()
    )


async def fetch_lyrics(
    client: httpx.AsyncClient,
    cache: SourceCache,
    *,
    artist_names: Sequence[str],
    title: str,
    duration_ms: int | None,
) -> LyricsResult:
    """artist + title + duration -> lines with optional timecodes. The
    duration is never sent to LRCLIB — gate 4B.0.4 measured the 5/9 coverage
    on the artist+title pair alone — it is what `is_match` checks the answer
    against afterwards.

    Raises `LrclibApiError` when the source did not answer in a shape that
    proves anything, and `lrc.LrcParseError` when a payload that claims to
    carry timecodes is not LRC at all. Both happen before anything is
    cached.
    """
    artist_query = join_artist_names(artist_names)
    normalized_title = " ".join(title.split())

    if not artist_query or not normalized_title:
        logger.warning(
            "lrclib_query_incomplete",
            has_artist=bool(artist_query),
            has_title=bool(normalized_title),
        )
        return LyricsResult(outcome=LyricsOutcome.ABSENT)

    cache_key = build_request_key("lrclib:lyrics:", artist_query, normalized_title)

    async def do_fetch() -> dict[str, object]:
        record = await _get_record(client, artist_query, normalized_title)
        if record is not None:
            # Classified here as well as on the read path: an empty positive
            # must raise *before* `SourceCache` can write it (D-48).
            _classify_record(record)
            return {"found": True, "record": record}

        fallback = await _search_for_record(
            client, artist_query=artist_query, title=normalized_title, duration_ms=duration_ms
        )
        if fallback is None:
            return {"found": False}
        return {"found": True, "record": fallback}

    payload = await cache.get_or_fetch(SOURCE_NAME, cache_key, do_fetch)
    result = _result_from_payload(
        payload, artist_query=artist_query, title=normalized_title, duration_ms=duration_ms
    )

    logger.info(
        "lrclib_lyrics_outcome",
        outcome=result.outcome.value,
        line_count=len(result.lines),
        artist_count=len(artist_names),
    )
    return result


async def _request(client: httpx.AsyncClient, url: str, params: dict[str, str]) -> httpx.Response:
    """The retry loop, and the only place a transport failure becomes an
    `LrclibApiError`. `except httpx.HTTPError` is deliberately narrow:
    `SoftTimeLimitExceeded` and `SQLAlchemyError` are not source failures
    and must travel straight through.
    """
    last_reason = "no attempt was made"

    for attempt in range(MAX_ATTEMPTS):
        await THROTTLE.wait()
        try:
            response = await client.get(url, params=params, headers=REQUEST_HEADERS)
        except httpx.HTTPError as error:
            last_reason = f"{type(error).__name__}: {error}"
        else:
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            last_reason = f"HTTP {response.status_code}"

        logger.warning("lrclib_attempt_failed", attempt=attempt, reason=last_reason, url=url)
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * 2**attempt)

    raise LrclibApiError(f"lrclib не відповів за {MAX_ATTEMPTS} спроб: {last_reason}")


async def _get_record(
    client: httpx.AsyncClient, artist_query: str, title: str
) -> dict[str, object] | None:
    """`None` means the documented miss and nothing else — every other shape
    has already raised by the time this returns."""
    response = await _request(client, GET_URL, {"artist_name": artist_query, "track_name": title})

    if _is_documented_miss(response):
        return None

    return _require_ok_json_object(response)


async def _search_for_record(
    client: httpx.AsyncClient, *, artist_query: str, title: str, duration_ms: int | None
) -> dict[str, object] | None:
    """D-90: `404 TrackNotFound` only proves LRCLIB has nothing under the
    exact artist string we sent, so a miss is not final until the title has
    been searched too. Searched **by title alone** on purpose — an
    `artist_name` filter here would reproduce the very mismatch this call
    exists to recover from — and every row is verified locally, which is
    what makes the 20 foreign rows a common title returns harmless.
    """
    response = await _request(client, SEARCH_URL, {"track_name": title})
    body = _require_ok_json(response)

    candidates = [as_dict(candidate) for candidate in as_list(body)[:MAX_SEARCH_CANDIDATES]]
    for candidate in candidates:
        if not _belongs_to_track(
            candidate, artist_query=artist_query, title=title, duration_ms=duration_ms
        ):
            continue
        # An empty row among search results is noise, not the D-48 shape:
        # it is an answer about some other track, so it is skipped, not
        # raised on. Only `/api/get` answers *our* question.
        if _has_content(candidate):
            logger.info("lrclib_search_fallback_matched", candidate_count=len(candidates))
            return candidate

    logger.info("lrclib_search_fallback_empty", candidate_count=len(candidates))
    return None


def _is_documented_miss(response: httpx.Response) -> bool:
    """404 is not the verdict, the body is. `and`, not `or`, like Deezer's
    whitelist: either mark alone is an unverified combination."""
    if response.status_code != NOT_FOUND_STATUS_CODE:
        return False

    body = as_dict(_json_body(response))
    error_name = as_str(body.get("name"))
    # `statusCode`, not `code`: the literal body, re-measured from the
    # container — `{"message": ..., "name": "TrackNotFound", "statusCode": 404}`.
    error_code = body.get("statusCode")

    if error_code == NOT_FOUND_STATUS_CODE and error_name in NOT_FOUND_ERROR_NAMES:
        return True

    raise LrclibApiError(
        f"lrclib 404 із недокументованим тілом: name={error_name}, code={error_code}"
    )


def _require_ok_json(response: httpx.Response) -> object:
    if response.status_code != httpx.codes.OK:
        raise LrclibApiError(f"lrclib відповів HTTP {response.status_code}")
    return _json_body(response)


def _require_ok_json_object(response: httpx.Response) -> dict[str, object]:
    return as_dict(_require_ok_json(response))


def _json_body(response: httpx.Response) -> object:
    """A non-JSON body means something other than the application answered —
    a proxy, a captive portal, an error page. The body itself is never put in
    the message: it may carry the lyrics (S4B-R13) and it may be huge."""
    try:
        return response.json()
    except ValueError as error:
        content_type = response.headers.get("content-type", "невідомий")
        raise LrclibApiError(
            f"lrclib відповів {response.status_code} не-JSON тілом ({content_type})"
        ) from error


def _classify_record(record: dict[str, object]) -> LyricsResult:
    """The shape table of §7, in order. Synced first, so a record that
    carries both real lyrics and an `instrumental` flag is still rendered;
    the empty positive is what is left when nothing at all is there."""
    synced_lyrics = (as_str(record.get("syncedLyrics")) or "").strip()
    plain_lyrics = (as_str(record.get("plainLyrics")) or "").strip()

    if synced_lyrics:
        lines = tuple(
            LyricsLine(start_ms=line.start_ms, text=line.text) for line in parse_lrc(synced_lyrics)
        )
        return LyricsResult(outcome=LyricsOutcome.SYNCED, lines=lines)

    if plain_lyrics:
        lines = tuple(
            LyricsLine(start_ms=None, text=" ".join(line.split()))
            for line in plain_lyrics.splitlines()
            if line.strip()
        )
        return LyricsResult(outcome=LyricsOutcome.UNSYNCED, lines=lines)

    if record.get("instrumental") is True:
        return LyricsResult(outcome=LyricsOutcome.INSTRUMENTAL)

    raise LrclibApiError(
        "lrclib: порожній позитив — 200 без обох текстів і без instrumental (D-48)"
    )


def _has_content(record: dict[str, object]) -> bool:
    return bool(
        (as_str(record.get("syncedLyrics")) or "").strip()
        or (as_str(record.get("plainLyrics")) or "").strip()
        or record.get("instrumental") is True
    )


def _belongs_to_track(
    record: dict[str, object], *, artist_query: str, title: str, duration_ms: int | None
) -> bool:
    """`is_match` on `artistName`/`trackName` plus the duration tolerance.
    An empty field is a rejection with a name of its own: `is_match` reads it
    as "no match" anyway, and without the branch a truncated answer would be
    indistinguishable from a genuinely foreign one (§7).
    """
    candidate_artist = as_str(record.get("artistName")) or ""
    candidate_title = as_str(record.get("trackName")) or ""

    if not candidate_artist.strip() or not candidate_title.strip():
        logger.warning(
            "lrclib_candidate_without_identity",
            has_artist=bool(candidate_artist.strip()),
            has_title=bool(candidate_title.strip()),
        )
        return False

    return is_match(
        reference_artist=artist_query,
        reference_title=title,
        reference_duration_ms=duration_ms,
        candidate_artist=candidate_artist,
        candidate_title=candidate_title,
        candidate_duration_ms=_duration_ms_of(record),
    )


def _duration_ms_of(record: dict[str, object]) -> int | None:
    """LRCLIB answers seconds, and as a float — `as_int` would drop `233.0`
    on the floor and read it as an absent duration."""
    duration = record.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int | float):
        return None
    return round(duration * 1000)


def _result_from_payload(
    payload: dict[str, object], *, artist_query: str, title: str, duration_ms: int | None
) -> LyricsResult:
    """Guards the read path too, the way `songlink._require_entity_data`
    does: a row cached before a check existed would otherwise still be served
    as a valid answer."""
    if not payload.get("found"):
        return LyricsResult(outcome=LyricsOutcome.ABSENT)

    record = as_dict(payload.get("record"))
    if not _belongs_to_track(
        record, artist_query=artist_query, title=title, duration_ms=duration_ms
    ):
        logger.warning(
            "lrclib_rejected_by_matching",
            title=title,
            candidate_duration_ms=_duration_ms_of(record),
        )
        return LyricsResult(outcome=LyricsOutcome.REJECTED_BY_MATCHING)

    return _classify_record(record)
