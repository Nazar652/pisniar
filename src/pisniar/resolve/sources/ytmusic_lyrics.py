"""YouTube Music lyrics of the `youtubeMusic` videoId, the recording the audio comes from. Both `ytmusicapi` calls are
sync, so they run in one `asyncio.to_thread` under `wait_for`. An answer is classified by its shape
(timed, plain, none); any other shape raises and is never cached.
"""

from __future__ import annotations

import asyncio

from ytmusicapi.models.lyrics import LyricLine

from pisniar.logging import get_logger
from pisniar.resolve.cache import SourceCache, build_request_key
from pisniar.resolve.http import TIMEOUT_SECONDS
from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_str
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.resolve.sources.ytmusic import SOURCE_NAME, THROTTLE, get_client

logger = get_logger(__name__)

# YouTube Music's mark for an instrumental gap: shown as a timed line, dropped from plain text.
MUSIC_NOTE = "♪"


class YtmusicLyricsApiError(RuntimeError):
    """an answer whose shape proves nothing. Raised rather than cached, like
    `LrclibApiError`: an outage cached as "no lyrics" would hide them for a TTL."""

    outcome = LyricsOutcome.SOURCE_UNAVAILABLE


async def fetch_lyrics(cache: SourceCache, *, video_id: str) -> LyricsResult:
    """`video_id` is a validated `youtubeMusic` id. Transport errors and timeouts
    propagate, so the caller's isolation is what turns them into "no text"."""
    cache_key = build_request_key("ytmusic:lyrics:", video_id, casefold=False)
    asked_youtube = False

    async def do_fetch() -> dict[str, object]:
        nonlocal asked_youtube
        asked_youtube = True

        await THROTTLE.wait()
        # `wait_for` frees this task, not the thread, as in `ytmusic.search_by_name`.
        answer = await asyncio.wait_for(asyncio.to_thread(fetch_answer_sync, video_id), timeout=TIMEOUT_SECONDS)
        return payload_from_answer(answer)

    payload = await cache.get_or_fetch(SOURCE_NAME, cache_key, do_fetch)
    result = result_from_payload(payload)

    logger.info(
        "ytmusic_lyrics_outcome",
        outcome=result.outcome.value,
        line_count=len(result.lines),
        from_cache=not asked_youtube,
    )
    return result


def fetch_answer_sync(video_id: str) -> object:
    """`None` when the video has no lyrics page at all."""
    client = get_client()
    watch_playlist = as_dict(client.get_watch_playlist(video_id))
    browse_id = as_str(watch_playlist.get("lyrics"))
    if not browse_id:
        return None
    return client.get_lyrics(browse_id, timestamps=True)


def payload_from_answer(answer: object) -> dict[str, object]:
    """`{"found": false}`, `{"found": true, "timed": [[start_ms, text], ...]}` or
    `{"found": true, "plain": text}`."""
    if answer is None:
        return {"found": False}

    body = as_dict(answer)
    lyrics = body.get("lyrics")

    if body.get("hasTimestamps") is True and isinstance(lyrics, list) and lyrics:
        pairs: list[list[object]] = []
        for line in lyrics:
            if not isinstance(line, LyricLine):
                raise YtmusicLyricsApiError("ytmusic: рядок тексту з таймкодами не схожий на LyricLine")
            pairs.append([line.start_time, line.text])
        return {"found": True, "timed": pairs}

    if body.get("hasTimestamps") is False and isinstance(lyrics, str) and lyrics.strip():
        return {"found": True, "plain": lyrics}

    raise YtmusicLyricsApiError("ytmusic: відповідь про текст не схожа ні на таймкодований, ні на простий")


def result_from_payload(payload: dict[str, object]) -> LyricsResult:
    """a cached row is re-read through the same checks, not trusted."""
    if not payload.get("found"):
        return LyricsResult(outcome=LyricsOutcome.ABSENT)

    timed = payload.get("timed")
    if isinstance(timed, list):
        timed_lines = sorted(
            (timed_line_from_pair(as_list(pair)) for pair in timed),
            key=lambda line: line.start_ms or 0,
        )
        # only gaps and no words says nothing about the song.
        if not any(line.text not in ("", MUSIC_NOTE) for line in timed_lines):
            return LyricsResult(outcome=LyricsOutcome.ABSENT)
        return LyricsResult(outcome=LyricsOutcome.SYNCED, lines=tuple(timed_lines))

    plain = as_str(payload.get("plain"))
    if plain is None:
        raise YtmusicLyricsApiError("ytmusic: у кешованій відповіді немає ні рядків, ні тексту")

    plain_lines = tuple(
        LyricsLine(start_ms=None, text=text)
        for text in (clean_text(raw_line) for raw_line in plain.splitlines())
        if text not in ("", MUSIC_NOTE)
    )
    if not plain_lines:
        return LyricsResult(outcome=LyricsOutcome.ABSENT)
    return LyricsResult(outcome=LyricsOutcome.UNSYNCED, lines=plain_lines)


def timed_line_from_pair(pair: list[object]) -> LyricsLine:
    """the note stays as text, so the break shows it; an empty line stays a pause."""
    start_ms = as_int(pair[0]) if len(pair) == 2 else None
    text = as_str(pair[1]) if len(pair) == 2 else None
    if start_ms is None or start_ms < 0 or text is None:
        raise YtmusicLyricsApiError("ytmusic: рядок із таймкодом має хибну форму")
    return LyricsLine(start_ms=start_ms, text=clean_text(text))


def clean_text(raw_text: str) -> str:
    return " ".join(raw_text.split())
