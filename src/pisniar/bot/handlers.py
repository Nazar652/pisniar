"""Command handlers.

Reachable only by admins — `AdminAllowlistMiddleware` has already filtered
everything else out by the time a handler runs.
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from celery import Celery
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import Post, Track
from pisniar.db.session import session_scope
from pisniar.db.states import IllegalStatusTransitionError, PostStatus, set_post_status
from pisniar.logging import bind_post_id, get_logger
from pisniar.resolve.urls import (
    MAX_NORMALIZED_URL_BYTES,
    ClassifiedUrl,
    UrlKind,
    classify_url,
    is_within_length_limit,
)
from pisniar.worker.celery_app import RESOLVE_POST_TASK_NAME

logger = get_logger(__name__)

router = Router(name="commands")

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
        post = await session.get(Post, post_id)
        if post is None:
            await message.answer(f"Пост {post_id} не знайдено.")
            return
        if post.status is PostStatus.RESOLVING:
            # The self-edge exists for Celery redelivery, not as a button:
            # a second task would race the first into two drafts (D-43/F-9).
            await message.answer(
                f"Пост {post_id} уже обробляється. Зачекайте на чернетку або на помилку."
            )
            return
        try:
            set_post_status(post, PostStatus.RESOLVING)
        except IllegalStatusTransitionError:
            await message.answer(
                f"Пост {post_id} зараз у стані «{post.status.value}» — /retry недоступний."
            )
            return

    bind_post_id(post_id)
    if not await _dispatch_resolve_task(celery_client, post_id):
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
        return

    if classified.kind is UrlKind.NOT_TRACK:
        reason = NOT_TRACK_REASON_UA.get(classified.not_track_reason or "", "не трек")
        await message.answer(
            f"Це посилання на {reason}, а не на трек. Надішліть пряме посилання на трек."
        )
        return
    if classified.kind is UrlKind.UNKNOWN:
        await message.answer("Не розпізнав це як посилання на трек.")
        return

    async with session_scope(session_factory) as session:
        track, track_existed = await _get_or_create_track(session, raw_url, classified)

        post = Post(track_id=track.id, created_by_user_id=message.from_user.id)
        session.add(post)
        await session.flush()
        set_post_status(post, PostStatus.RESOLVING)
        post_id = post.id

    bind_post_id(post_id)
    logger.info("link_received", post_id=post_id, track_existed=track_existed)

    if not await _dispatch_resolve_task(celery_client, post_id):
        await message.answer(
            f"Пост {post_id} створено, але не вдалося поставити задачу в чергу. "
            f"Спробуйте /retry {post_id}."
        )
        return

    receipt = f"Приймаю в обробку, post_id={post_id}."
    if track_existed:
        receipt += " Цей трек уже реєструвався раніше — створюю ще один пост."
    await message.answer(receipt)


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

    track = Track(source_url=raw_url, source_url_normalized=classified.normalized_url)
    if classified.platform == "youtube":
        # Seeded immediately, not just at resolve time — a user-supplied
        # clip must survive even if resolution never completes.
        track.platform_links = {
            "youtube": {"url": classified.normalized_url, "source": "user_input", "match": "manual"}
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
