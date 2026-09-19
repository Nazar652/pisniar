"""Command handlers.

Reachable only by admins — `AdminAllowlistMiddleware` has already filtered
everything else out by the time a handler runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from celery import Celery
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import Post, Track
from pisniar.db.session import session_scope
from pisniar.db.states import IllegalStatusTransitionError, PostStatus, set_post_status
from pisniar.logging import bind_post_id, get_logger
from pisniar.resolve.types import USER_INPUT_SOURCE
from pisniar.resolve.urls import (
    MAX_NORMALIZED_URL_BYTES,
    ClassifiedUrl,
    UrlKind,
    classify_url,
    is_within_length_limit,
)
from pisniar.worker.celery_app import RESOLVE_POST_TASK_NAME, TASK_TIME_LIMIT_SECONDS

logger = get_logger(__name__)

router = Router(name="commands")

DISPATCH_FAILED_ERROR = "не вдалося поставити задачу в чергу"

NOT_TRACK_REASON_UA = {
    "album": "альбом",
    "artist": "артиста",
    "playlist": "плейлист",
    "show": "подкаст",
    "episode": "епізод",
}


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    await message.answer("Бот Піснярні на зв'язку. Надішліть посилання на трек.")


@router.message(Command("retry"))
async def handle_retry(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    if message.text is None:
        return

    arguments = message.text.split(maxsplit=1)
    if len(arguments) < 2 or not arguments[1].strip().isdigit():
        await message.answer("Формат команди: /retry <post_id>")
        return
    post_id = int(arguments[1].strip())

    async with session_scope(session_factory) as session:
        try:
            # Locked, not just read: without it two near-simultaneous
            # `/retry` both pass the status check and dispatch (R2-12).
            post = await session.get(Post, post_id, with_for_update={"nowait": True})
        except DBAPIError:
            await session.rollback()
            await message.answer(
                f"Пост {post_id} зараз обробляється воркером. Зачекайте на чернетку або помилку."
            )
            return

        if post is None:
            await message.answer(f"Пост {post_id} не знайдено.")
            return

        if post.status is PostStatus.RESOLVING:
            if not _resolving_has_gone_stale(post):
                # The self-edge exists for Celery redelivery, not as a button:
                # a second task would race the first into two drafts (D-43/F-9).
                await message.answer(
                    f"Пост {post_id} уже обробляється. Зачекайте на чернетку або на помилку."
                )
                return

            # The self-edge changes no column, so the ORM `onupdate` never
            # fires and every later /retry would pass this guard too (R3-4).
            post.updated_at = datetime.now(UTC)

        try:
            set_post_status(post, PostStatus.RESOLVING)
        except IllegalStatusTransitionError:
            await message.answer(
                f"Пост {post_id} зараз у стані «{post.status.value}» — /retry недоступний."
            )
            return

    bind_post_id(post_id)
    if not await _dispatch_resolve_task(celery_client, post_id):
        await _mark_dispatch_failure(session_factory, post_id)
        await message.answer("Не вдалося поставити задачу в чергу. Спробуйте ще раз.")
        return

    await message.answer(f"Повторюю обробку поста {post_id}.")


@router.message(F.text)
async def handle_link(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """Any plain-text message is treated as a link submission — commands
    above already claimed everything that starts with `/`."""
    if message.from_user is None or message.text is None:
        return

    raw_url = message.text.strip()
    classified = classify_url(raw_url)

    # UNIQUE on a btree tops out near 2704 bytes and raises
    # ProgramLimitExceededError, which `except IntegrityError` misses (F-22).
    if not is_within_length_limit(raw_url) or not is_within_length_limit(classified.normalized_url):
        await message.answer(
            f"Посилання задовге (ліміт {MAX_NORMALIZED_URL_BYTES} байтів). "
            f"Надішліть коротке посилання на трек."
        )
        logger.info(
            "link_rejected",
            reason="too_long",
            raw_url_bytes=len(raw_url.encode("utf-8")),
            normalized_url_bytes=len(classified.normalized_url.encode("utf-8")),
            limit_bytes=MAX_NORMALIZED_URL_BYTES,
        )
        return

    if classified.kind is UrlKind.NOT_TRACK:
        reason = NOT_TRACK_REASON_UA.get(classified.not_track_reason or "", "не трек")
        await message.answer(
            f"Це посилання на {reason}, а не на трек. Надішліть пряме посилання на трек."
        )
        logger.info(
            "link_rejected",
            reason="not_track",
            not_track_reason=classified.not_track_reason,
            normalized_url=classified.normalized_url,
        )
        return

    if classified.kind is UrlKind.UNKNOWN:
        await message.answer("Не розпізнав це як посилання на трек.")
        # Only the length: the raw text of an unrecognized message is not a
        # URL and has no business in the logs.
        logger.info("link_rejected", reason="unknown", raw_text_length=len(raw_url))
        return

    post_id: int | None = None
    track_existed = False
    failure_answer: str | None = None

    async with session_scope(session_factory) as session:
        try:
            track, track_existed = await _get_or_create_track(session, raw_url, classified)

            post = Post(track_id=track.id, created_by_user_id=message.from_user.id)
            session.add(post)
            await session.flush()
        except IntegrityError:
            # The worker's cleanup deleted this track between the SELECT and
            # this INSERT; unhandled, aiogram answers the admin nothing (R2-13).
            await session.rollback()
            logger.warning("link_post_insert_lost_its_track")
            failure_answer = "Трек саме прибрали з бази. Надішліть посилання ще раз."
        except DBAPIError:
            # `lock_timeout` lives on the bot engine too now, so an acquisition
            # here can raise a plain `DBAPIError` — the clause above catches
            # only its `IntegrityError` subclass, and the admin got silence.
            await session.rollback()
            logger.exception("link_database_error")
            failure_answer = "База даних зараз недоступна або зайнята. Надішліть посилання ще раз."
        else:
            set_post_status(post, PostStatus.RESOLVING)
            post_id = post.id

    if failure_answer is not None:
        await message.answer(failure_answer)
        return
    assert post_id is not None

    bind_post_id(post_id)
    logger.info("link_received", post_id=post_id, track_existed=track_existed)

    if not await _dispatch_resolve_task(celery_client, post_id):
        await _mark_dispatch_failure(session_factory, post_id)
        await message.answer(
            f"Пост {post_id} створено, але не вдалося поставити задачу в чергу. "
            f"Спробуйте /retry {post_id}."
        )
        return

    receipt = f"Приймаю в обробку, post_id={post_id}."
    if track_existed:
        receipt += " Цей трек уже реєструвався раніше — створюю ще один пост."
    await message.answer(receipt)


def _resolving_has_gone_stale(post: Post) -> bool:
    """The only way out of `RESOLVING` when the worker died mid-task (R2-2).
    No race with F-9: Celery's hard limit kills a live task at
    `TASK_TIME_LIMIT_SECONDS`, so a running one is always fresher than this.
    """
    age_seconds = (datetime.now(UTC) - post.updated_at).total_seconds()
    return age_seconds > TASK_TIME_LIMIT_SECONDS


async def _get_or_create_track(
    session: AsyncSession, raw_url: str, classified: ClassifiedUrl
) -> tuple[Track, bool]:
    """Race-safe get-or-create on `source_url_normalized` (UNIQUE): two
    concurrent submissions of the same link both miss the SELECT, one wins
    the INSERT, the other re-reads the winner's row instead of erroring."""
    existing = await session.scalar(
        select(Track).where(Track.source_url_normalized == classified.normalized_url)
    )
    if existing is not None:
        return existing, True

    # `DBAPIError` is deliberately not caught here — the caller turns it into
    # an answer to the admin, and this function has no message of its own.
    track = Track(source_url=raw_url, source_url_normalized=classified.normalized_url)
    if classified.platform == "youtube":
        # Seeded immediately, not just at resolve time — a user-supplied
        # clip must survive even if resolution never completes.
        track.platform_links = {
            "youtube": {
                "url": classified.normalized_url,
                "source": USER_INPUT_SOURCE,
                "match": "manual",
            }
        }
    session.add(track)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Track).where(Track.source_url_normalized == classified.normalized_url)
        )
        if existing is not None:
            return existing, True
        raise
    return track, False


async def _mark_dispatch_failure(
    session_factory: async_sessionmaker[AsyncSession], post_id: int
) -> None:
    """D-53: the post is already committed as `RESOLVING`, so leaving it
    there makes the bot's own "спробуйте /retry" advice false for the next
    `TASK_TIME_LIMIT_SECONDS` — no task exists to move it on.
    """
    try:
        async with session_scope(session_factory) as session:
            post = await session.get(Post, post_id, with_for_update=True)
            if post is None or post.status is not PostStatus.RESOLVING:
                return

            set_post_status(post, PostStatus.FAILED, error=DISPATCH_FAILED_ERROR)
    except Exception:
        logger.exception("dispatch_failure_status_write_failed", post_id=post_id)


async def _dispatch_resolve_task(celery_client: Celery, post_id: int) -> bool:
    # `send_task()` blocks synchronously; off-loop, or an unreachable Redis
    # stalls this single-loop process's event loop (D-23).
    try:
        await asyncio.to_thread(
            celery_client.send_task, RESOLVE_POST_TASK_NAME, kwargs={"post_id": post_id}
        )
    except Exception:
        logger.exception("resolve_task_dispatch_failed", post_id=post_id)
        return False
    return True
