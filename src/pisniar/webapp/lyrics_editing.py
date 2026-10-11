"""Reading and replacing a track's lyrics from the Mini App. The save is one short transaction:
the post is read without a lock, the track is locked `FOR NO KEY UPDATE` (the lock every writer of
`lyric` takes), the old lines go, the new ones come in under `source='manual'` and the marker
that keeps the automatic sources away is set. Telegram and the queue are never called here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.locks import lock_track_for_write
from pisniar.db.models import (
    MAX_LYRIC_LINES,
    MAX_LYRIC_TEXT_LENGTH,
    MAX_LYRICS_TOTAL_CHARACTERS,
    Lyric,
    Post,
    Track,
)
from pisniar.db.states import PostStatus
from pisniar.logging import get_logger
from pisniar.webapp.editing import STALE_DATA_MESSAGE, count_media_posts
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_view import current_audio_asset
from pisniar.webapp.lyrics_rules import LineDraft, LyricsRuleError, plan_lyrics
from pisniar.webapp.schemas import LyricLineView, LyricsUpdate, LyricsView
from pisniar.webapp.status import choosability_of

logger = get_logger(__name__)

MANUAL_SOURCE = "manual"
# a render of the track reads the lines now; the save waits for it. `EDITING` is not here: the
# audio is ready and nobody reads the text.
LYRICS_BLOCKING_STATUSES = (PostStatus.DOWNLOADING, PostStatus.DOWNLOADED, PostStatus.RENDERING)
MEDIA_IN_PROGRESS_MESSAGE = "Для цього треку зараз готується відео. Дочекайтесь завершення і збережіть текст знову."
AUDIO_MISSING_MESSAGE = "Аудіо для поточного посилання YouTube Music ще не завантажено, тому відстукати текст не можна."


class LyricsReplaceError(RuntimeError):
    """the write did not land as a whole; the transaction rolls back and the old text stays."""


def lyrics_version_of(lines: Sequence[Lyric], edited_at: datetime | None) -> str:
    """a digest of the stored text and its marker: whatever another writer changes, it changes."""
    canonical = json.dumps(
        {
            "lines": [[line.line_index, line.start_ms, line.text, line.source] for line in lines],
            "edited_at": None if edited_at is None else edited_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def read_stored_lines(session: AsyncSession, track_id: int) -> list[Lyric]:
    """every stored line, timed or not, in the only order there is."""
    statement = select(Lyric).where(Lyric.track_id == track_id).order_by(Lyric.line_index)
    return list((await session.scalars(statement)).all())


async def other_post_ids_of(
    session: AsyncSession,
    track_id: int,
    post_id: int,
) -> list[int]:
    statement = select(Post.id).where(Post.track_id == track_id, Post.id != post_id).order_by(Post.id)
    return list((await session.scalars(statement)).all())


def view_of(
    lines: Sequence[Lyric],
    edited_at: datetime | None,
    other_post_ids: list[int],
) -> LyricsView:
    return LyricsView(
        lines=[LyricLineView(text=line.text, start_ms=line.start_ms, source=line.source) for line in lines],
        lyrics_version=lyrics_version_of(lines, edited_at),
        edited_by_admin=edited_at is not None,
        other_post_ids=other_post_ids,
        max_lines=MAX_LYRIC_LINES,
        max_line_length=MAX_LYRIC_TEXT_LENGTH,
        max_total_characters=MAX_LYRICS_TOTAL_CHARACTERS,
    )


def refuse(
    status_code: int,
    code: str,
    message: str,
    event: str,
    user_id: int,
    post_id: int,
) -> NoReturn:
    logger.warning(event, reason=code, user_id=user_id, post_id=post_id)
    raise ApiProblem(status_code, code, message)


async def fetch_lyrics_view(
    session: AsyncSession,
    post_id: int,
    user_id: int,
) -> LyricsView | None:
    """no lock and no write; `None` for a missing post, a 409 for one whose text is not editable."""
    post = await session.get(Post, post_id)
    if post is None:
        return None

    verdict = choosability_of(post.status, post.failed_from_status)
    if not verdict.choosable:
        assert verdict.reason is not None
        refuse(409, "not_choosable", verdict.reason, "lyrics_view_refused", user_id, post_id)

    track = await session.get(Track, post.track_id)
    if track is None:
        return None
    lines = await read_stored_lines(session, track.id)
    view = view_of(lines, track.lyrics_edited_at, await other_post_ids_of(session, track.id, post_id))

    logger.info(
        "lyrics_viewed",
        user_id=user_id,
        post_id=post_id,
        track_id=track.id,
        line_count=len(lines),
        edited_by_admin=view.edited_by_admin,
    )
    return view


async def write_lines(
    session: AsyncSession,
    track_id: int,
    lines: Sequence[LineDraft],
) -> None:
    """the old lines out, the new ones in as one statement; a count that falls short means a
    writer skipped the lock, and the whole transaction must roll back."""
    await session.execute(delete(Lyric).where(Lyric.track_id == track_id))
    if not lines:
        return

    inserted = await session.execute(
        insert(Lyric)
        .values(
            [
                {
                    "track_id": track_id,
                    "line_index": line_index,
                    "start_ms": line.start_ms,
                    "text": line.text,
                    "source": MANUAL_SOURCE,
                }
                for line_index, line in enumerate(lines)
            ]
        )
        .returning(Lyric.id)
    )
    inserted_count = len(inserted.all())
    if inserted_count != len(lines):
        raise LyricsReplaceError(f"трек {track_id}: записано {inserted_count} рядків із {len(lines)}")


async def replace_lyrics(
    session: AsyncSession,
    post_id: int,
    update: LyricsUpdate,
    user_id: int,
) -> LyricsView | None:
    """the caller commits. Every refusal leaves the stored text whole."""
    event = "lyrics_save_refused"
    post = await session.get(Post, post_id)
    if post is None:
        return None

    verdict = choosability_of(post.status, post.failed_from_status)
    if not verdict.choosable:
        assert verdict.reason is not None
        refuse(409, "not_choosable", verdict.reason, event, user_id, post_id)

    track = await lock_track_for_write(session, post.track_id)
    if track is None:
        return None

    if await count_media_posts(session, track.id, LYRICS_BLOCKING_STATUSES) > 0:
        refuse(409, "media_in_progress", MEDIA_IN_PROGRESS_MESSAGE, event, user_id, post_id)

    asset = await current_audio_asset(session, track)
    if asset is None:
        refuse(409, "audio_missing", AUDIO_MISSING_MESSAGE, event, user_id, post_id)

    drafts = [LineDraft(text=line.text, start_ms=line.start_ms) for line in update.lines]
    try:
        planned = plan_lyrics(drafts, asset.duration_ms)
    except LyricsRuleError as error:
        logger.warning(
            event,
            reason="invalid_lyrics",
            line_number=error.number,
            user_id=user_id,
            post_id=post_id,
        )
        raise ApiProblem(422, "invalid_lyrics", error.message) from None

    stored = await read_stored_lines(session, track.id)
    if lyrics_version_of(stored, track.lyrics_edited_at) != update.expected_lyrics_version:
        refuse(409, "stale_data", STALE_DATA_MESSAGE, event, user_id, post_id)

    await write_lines(session, track.id, planned)
    track.lyrics_edited_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(track)

    saved = await read_stored_lines(session, track.id)
    logger.info(
        "lyrics_saved",
        user_id=user_id,
        post_id=post_id,
        track_id=track.id,
        line_count=len(saved),
        timed_count=sum(1 for line in saved if line.start_ms is not None),
        cleared=not saved,
    )
    return view_of(saved, track.lyrics_edited_at, await other_post_ids_of(session, track.id, post_id))
