"""The two lookups behind «Знайти синхронізацію» and «Знайти просто текст»: a track's text from
the outside sources, offered to the admin and written nowhere. The database is read first and its
session closed; the network runs with nothing open, under one time limit; the answer is
normalized so that it would pass `plan_lyrics`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import NoReturn, cast

import httpx
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import MAX_LYRIC_TEXT_LENGTH, Post, Track
from pisniar.logging import get_logger
from pisniar.resolve import lyrics_lookup
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.lyrics_lookup import (
    LYRICS_OVER_LIMITS,
    PLAIN_TEXT_SOURCES,
    LyricsLookupQuery,
    SourceAnswer,
    fetch_plain_text,
    look_up_timed_text,
    plain_lines_of,
)
from pisniar.resolve.sources.lrclib import LyricsOutcome
from pisniar.webapp.database import DATABASE_UNAVAILABLE_MESSAGE, read_from_database
from pisniar.webapp.editing import POST_NOT_FOUND_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_view import current_audio_asset
from pisniar.webapp.lyrics_editing import AUDIO_MISSING_MESSAGE
from pisniar.webapp.lyrics_rules import normalized_text
from pisniar.webapp.schemas import LyricsProposal, LyricsSourceName, ProposedLine
from pisniar.webapp.status import choosability_of

logger = get_logger(__name__)

# two lines at one time are told apart by this much, so the times stay strictly rising
TIME_COLLISION_STEP_MS = 10

SYNCED_NOT_FOUND_MESSAGE = "Тексту з таймкодами для цього треку не знайдено ні на YouTube Music, ні в LRCLIB."
PLAIN_NOT_FOUND_MESSAGE = "У {label} тексту цього треку не знайдено. Спробуйте інше джерело."
SOURCE_UNAVAILABLE_MESSAGE = "Джерело тексту не відповіло. Спробуйте за хвилину."
OVER_LIMITS_MESSAGE = "Знайдений текст завеликий, щоб його зберегти."
KIND_SYNCED = "synced"
KIND_PLAIN = "plain"


class ProposalOverLimitsError(Exception):
    """a text that would not pass the stored-text limits, however it is cut."""


@dataclass(frozen=True)
class LookupSubject:
    """what the database says about the track, copied out so no ORM row survives the session."""

    query: LyricsLookupQuery
    audio_duration_ms: int


def artist_names_of(stored_artists: Sequence[object]) -> tuple[str, ...]:
    names = (entry.get("name") for entry in stored_artists if isinstance(entry, dict))
    return tuple(name for name in names if isinstance(name, str) and name.strip())


def known_source(name: str) -> LyricsSourceName:
    if name == "ytmusic":
        return "ytmusic"
    if name == "lrclib":
        return "lrclib"
    if name == "genius":
        return "genius"
    raise ValueError(f"невідоме джерело тексту: {name}")


def fitted_text(raw_text: str) -> tuple[str, bool]:
    """spaces collapsed and a long line cut, never dropped; `True` when it was cut."""
    text = normalized_text(raw_text)
    if len(text) > MAX_LYRIC_TEXT_LENGTH:
        return text[:MAX_LYRIC_TEXT_LENGTH], True
    return text, False


def within_stored_text_limits(lines: Sequence[ProposedLine]) -> bool:
    characters = sum(len(line.text) for line in lines)
    return lyrics_lookup.fits_stored_text_limits(len(lines), characters)


def synced_proposal(answer: SourceAnswer, audio_duration_ms: int) -> LyricsProposal:
    """times kept, in order and strictly rising; a line at or past the end of the audio is
    counted and left out. Raises when the rest does not fit the limits."""
    timed = sorted(
        (line for line in answer.result.lines if line.start_ms is not None),
        key=lambda line: line.start_ms or 0,
    )
    lines: list[ProposedLine] = []
    previous_ms: int | None = None
    dropped = truncated = 0

    for line in timed:
        start_ms = line.start_ms or 0
        if previous_ms is not None:
            start_ms = max(start_ms, previous_ms + TIME_COLLISION_STEP_MS)
        previous_ms = start_ms

        if start_ms >= audio_duration_ms:
            dropped += 1
            continue
        text, was_cut = fitted_text(line.text)
        truncated += was_cut
        lines.append(ProposedLine(text=text, start_ms=start_ms))

    if not within_stored_text_limits(lines):
        raise ProposalOverLimitsError
    return LyricsProposal(
        kind=KIND_SYNCED,
        source=known_source(answer.source),
        lines=lines,
        dropped_line_count=dropped,
        truncated_line_count=truncated,
    )


def plain_proposal(
    source: LyricsSourceName,
    texts: Sequence[str],
    found_count: int,
) -> LyricsProposal:
    """lines without times; `found_count` is how many the source gave, the rest were empty or a
    lone note. Raises when the text does not fit the limits."""
    lines: list[ProposedLine] = []
    truncated = 0
    for raw_text in texts:
        text, was_cut = fitted_text(raw_text)
        truncated += was_cut
        lines.append(ProposedLine(text=text, start_ms=None))

    if not within_stored_text_limits(lines):
        raise ProposalOverLimitsError
    return LyricsProposal(
        kind=KIND_PLAIN,
        source=source,
        lines=lines,
        dropped_line_count=found_count - len(texts),
        truncated_line_count=truncated,
    )


@dataclass(frozen=True)
class LookupScope:
    """what a lookup log line says about the request: the kind and, for plain text, the source."""

    kind: str
    source: str | None = None


SYNCED_SCOPE = LookupScope(KIND_SYNCED)


def refuse(
    status_code: int,
    code: str,
    message: str,
    scope: LookupScope,
    user_id: int,
    post_id: int,
) -> NoReturn:
    logger.warning(
        "lyrics_lookup_refused",
        reason=code,
        kind=scope.kind,
        source=scope.source,
        user_id=user_id,
        post_id=post_id,
    )
    raise ApiProblem(status_code, code, message)


async def fetch_lookup_subject(
    session: AsyncSession,
    post_id: int,
    scope: LookupScope,
    user_id: int,
) -> LookupSubject | None:
    """`None` for a missing post; a 409 when the text is not editable now or has no audio."""
    row = (
        await session.execute(select(Post, Track).join(Track, Track.id == Post.track_id).where(Post.id == post_id))
    ).one_or_none()
    if row is None:
        return None
    post, track = row.tuple()

    verdict = choosability_of(post.status, post.failed_from_status)
    if not verdict.choosable:
        assert verdict.reason is not None
        refuse(409, "not_choosable", verdict.reason, scope, user_id, post_id)

    asset = await current_audio_asset(session, track)
    if asset is None:
        refuse(409, "audio_missing", AUDIO_MISSING_MESSAGE, scope, user_id, post_id)

    return LookupSubject(
        query=LyricsLookupQuery(
            track_id=track.id,
            artist_names=artist_names_of(track.artists),
            title=track.title or "",
            duration_ms=track.duration_ms,
            video_id=asset.video_id,
        ),
        audio_duration_ms=asset.duration_ms,
    )


def lookup_tools_of(
    request: Request,
    scope: LookupScope,
    user_id: int,
    post_id: int,
) -> tuple[httpx.AsyncClient, SourceCache]:
    http_client = cast("httpx.AsyncClient | None", request.app.state.http_client)
    session_factory = cast("async_sessionmaker[AsyncSession] | None", request.app.state.session_factory)
    if http_client is None or session_factory is None:
        logger.error("webapp_lyrics_lookup_not_configured")
        refuse(503, "lyrics_source_unavailable", SOURCE_UNAVAILABLE_MESSAGE, scope, user_id, post_id)
    ttl_seconds = cast("int", request.app.state.source_fetch_ttl_seconds)
    return http_client, SourceCache(session_factory=session_factory, ttl_seconds=ttl_seconds)


async def within_time_limit[ResultT](
    lookup: Awaitable[ResultT],
    scope: LookupScope,
    user_id: int,
    post_id: int,
) -> ResultT:
    """the one limit of a lookup. A database failure under the cache is not a source's silence."""
    try:
        return await asyncio.wait_for(lookup, lyrics_lookup.LYRICS_LOOKUP_TIMEOUT_SECONDS)
    except TimeoutError:
        refuse(503, "lyrics_source_unavailable", SOURCE_UNAVAILABLE_MESSAGE, scope, user_id, post_id)
    except SQLAlchemyError:
        logger.exception("webapp_lyrics_lookup_database_failed")
        raise ApiProblem(503, "database_unavailable", DATABASE_UNAVAILABLE_MESSAGE) from None


async def read_subject(
    request: Request,
    post_id: int,
    scope: LookupScope,
    user_id: int,
) -> LookupSubject:
    logger.info(
        "lyrics_lookup_requested",
        kind=scope.kind,
        source=scope.source,
        user_id=user_id,
        post_id=post_id,
    )
    subject = await read_from_database(request, lambda session: fetch_lookup_subject(session, post_id, scope, user_id))
    if subject is None:
        raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
    return subject


def log_found(
    proposal: LyricsProposal,
    user_id: int,
    post_id: int,
) -> LyricsProposal:
    logger.info(
        "lyrics_lookup_found",
        kind=proposal.kind,
        source=proposal.source,
        line_count=len(proposal.lines),
        dropped_line_count=proposal.dropped_line_count,
        truncated_line_count=proposal.truncated_line_count,
        user_id=user_id,
        post_id=post_id,
    )
    return proposal


async def propose_synced_lyrics(
    request: Request,
    post_id: int,
    user_id: int,
) -> LyricsProposal:
    """the timed text of YouTube Music, else of LRCLIB; nothing is locked and nothing is written."""
    scope = SYNCED_SCOPE
    subject = await read_subject(request, post_id, scope, user_id)
    http_client, cache = lookup_tools_of(request, scope, user_id, post_id)

    answer, reason = await within_time_limit(
        look_up_timed_text(http_client, cache, subject.query), scope, user_id, post_id
    )
    if answer is None:
        if reason == LyricsOutcome.SOURCE_UNAVAILABLE.value:
            refuse(
                503,
                "lyrics_source_unavailable",
                SOURCE_UNAVAILABLE_MESSAGE,
                scope,
                user_id,
                post_id,
            )
        if reason == LYRICS_OVER_LIMITS:
            refuse(409, "lyrics_over_limits", OVER_LIMITS_MESSAGE, scope, user_id, post_id)
        refuse(409, "synced_lyrics_not_found", SYNCED_NOT_FOUND_MESSAGE, scope, user_id, post_id)

    try:
        proposal = synced_proposal(answer, subject.audio_duration_ms)
    except ProposalOverLimitsError:
        refuse(409, "lyrics_over_limits", OVER_LIMITS_MESSAGE, scope, user_id, post_id)
    if not proposal.lines:
        refuse(409, "synced_lyrics_not_found", SYNCED_NOT_FOUND_MESSAGE, scope, user_id, post_id)
    return log_found(proposal, user_id, post_id)


async def propose_plain_lyrics(
    request: Request,
    post_id: int,
    source: LyricsSourceName,
    user_id: int,
) -> LyricsProposal:
    """the words of one chosen source, without times; nothing is locked and nothing is written."""
    scope = LookupScope(KIND_PLAIN, source)
    subject = await read_subject(request, post_id, scope, user_id)
    http_client, cache = lookup_tools_of(request, scope, user_id, post_id)

    result = await within_time_limit(
        fetch_plain_text(source, http_client, cache, subject.query), scope, user_id, post_id
    )
    if result.outcome is LyricsOutcome.SOURCE_UNAVAILABLE:
        refuse(503, "lyrics_source_unavailable", SOURCE_UNAVAILABLE_MESSAGE, scope, user_id, post_id)

    texts = plain_lines_of(result)
    if not texts:
        label = next(entry.label for entry in PLAIN_TEXT_SOURCES if entry.name == source)
        refuse(
            409,
            "plain_lyrics_not_found",
            PLAIN_NOT_FOUND_MESSAGE.format(label=label),
            scope,
            user_id,
            post_id,
        )

    try:
        proposal = plain_proposal(source, texts, len(result.lines))
    except ProposalOverLimitsError:
        refuse(409, "lyrics_over_limits", OVER_LIMITS_MESSAGE, scope, user_id, post_id)
    return log_found(proposal, user_id, post_id)
