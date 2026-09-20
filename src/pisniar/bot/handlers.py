"""Command handlers.

Reachable only by admins — `AdminAllowlistMiddleware` has already filtered
everything else out by the time a handler runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
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
    is_safe_url,
    is_within_length_limit,
)
from pisniar.telegram.keyboards import parse_publish_callback
from pisniar.worker.celery_app import (
    PUBLISH_POST_TASK_NAME,
    RESOLVE_POST_TASK_NAME,
    TASK_TIME_LIMIT_SECONDS,
)

logger = get_logger(__name__)

router = Router(name="commands")

DISPATCH_FAILED_ERROR = "не вдалося поставити задачу в чергу"
PUBLISH_DISPATCH_FAILED_ERROR = "не вдалося поставити задачу публікації в чергу"
UNKNOWN_CALLBACK_ANSWER = "Невідома дія."

# D-64: a clip is a `youtube` link, never a `youtubeMusic` one — the latter
# is the canonical "— Topic" upload and belongs to the platform half.
CLIP_PLATFORM = "youtube"

PAIR_SHAPE_HINT = "Очікується пара «посилання на платформу + кліп YouTube»"
TWO_PLATFORM_LINKS_ANSWER = (
    f"Обидва посилання ведуть на платформи, кліпа серед них немає. {PAIR_SHAPE_HINT} — "
    f"надішліть одне посилання окремо або додайте до нього кліп."
)
TWO_CLIPS_ANSWER = (
    f"Обидва посилання — кліпи YouTube. {PAIR_SHAPE_HINT} — надішліть один кліп окремо "
    f"або додайте до нього посилання на платформу."
)
TOO_MANY_LINKS_ANSWER = (
    "У повідомленні посилань: {count}. Надішліть одне посилання на трек або пару "
    "«посилання на платформу + кліп YouTube»."
)

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
    if message.text is None or message.from_user is None:
        return

    user_id = message.from_user.id

    arguments = message.text.split(maxsplit=1)
    if len(arguments) < 2 or not arguments[1].strip().isdigit():
        await message.answer("Формат команди: /retry <post_id>")
        logger.warning("retry_refused", reason="bad_arguments", user_id=user_id)
        return
    post_id = int(arguments[1].strip())

    # Bound before the first refusal can fire, not after the transaction:
    # a `/retry` that never dispatches is exactly the one worth tracing (D-70).
    bind_post_id(post_id)
    logger.info("retry_requested", user_id=user_id, post_id=post_id)

    republish = False
    reclaimed_stale_resolving = False

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
            logger.warning("retry_refused", reason="post_locked", user_id=user_id, post_id=post_id)
            return

        if post is None:
            await message.answer(f"Пост {post_id} не знайдено.")
            logger.warning(
                "retry_refused", reason="post_not_found", user_id=user_id, post_id=post_id
            )
            return

        if post.status is PostStatus.FAILED and post.draft_message_id is not None:
            # The draft was delivered, so the failure came after it — on
            # publication (D-47). Resolving here substitutes the intent.
            republish = True
            set_post_status(post, PostStatus.PUBLISHING)

        elif post.status is PostStatus.RESOLVING:
            if not _resolving_has_gone_stale(post):
                # The self-edge exists for Celery redelivery, not as a button:
                # a second task would race the first into two drafts (D-43/F-9).
                await message.answer(
                    f"Пост {post_id} уже обробляється. Зачекайте на чернетку або на помилку."
                )
                logger.warning(
                    "retry_refused", reason="already_resolving", user_id=user_id, post_id=post_id
                )
                return

            # The self-edge changes no column, so the ORM `onupdate` never
            # fires and every later /retry would pass this guard too (R3-4).
            post.updated_at = datetime.now(UTC)
            reclaimed_stale_resolving = True

        if not republish:
            try:
                set_post_status(post, PostStatus.RESOLVING)
            except IllegalStatusTransitionError:
                await message.answer(
                    f"Пост {post_id} зараз у стані «{post.status.value}» — /retry недоступний."
                )
                logger.warning(
                    "retry_refused",
                    reason="illegal_status",
                    status=post.status.value,
                    user_id=user_id,
                    post_id=post_id,
                )
                return

    # The two branches are different intents, not two spellings of one — which
    # is the distinction D-47 introduced and D-70 asked to make visible.
    branch = "republish" if republish else "resolve"
    logger.info(
        "retry_branch_selected",
        branch=branch,
        user_id=user_id,
        post_id=post_id,
        reclaimed_stale_resolving=reclaimed_stale_resolving,
    )

    if republish:
        if not await _dispatch_publish_task(celery_client, post_id, user_id):
            await _mark_dispatch_failure(
                session_factory,
                post_id,
                expected_status=PostStatus.PUBLISHING,
                error=PUBLISH_DISPATCH_FAILED_ERROR,
            )
            await message.answer("Не вдалося поставити задачу в чергу. Спробуйте ще раз.")
            logger.warning(
                "retry_refused",
                reason="dispatch_failed",
                branch=branch,
                user_id=user_id,
                post_id=post_id,
            )
            return

        await message.answer(f"Повторюю публікацію поста {post_id}.")
        logger.info("retry_dispatched", branch=branch, user_id=user_id, post_id=post_id)
        return

    if not await _dispatch_resolve_task(celery_client, post_id):
        await _mark_dispatch_failure(
            session_factory,
            post_id,
            expected_status=PostStatus.RESOLVING,
            error=DISPATCH_FAILED_ERROR,
        )
        await message.answer("Не вдалося поставити задачу в чергу. Спробуйте ще раз.")
        logger.warning(
            "retry_refused",
            reason="dispatch_failed",
            branch=branch,
            user_id=user_id,
            post_id=post_id,
        )
        return

    await message.answer(f"Повторюю обробку поста {post_id}.")
    logger.info("retry_dispatched", branch=branch, user_id=user_id, post_id=post_id)


@router.callback_query()
async def handle_publish_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """The publish trigger (D-46). Reachable only by admins for the same
    reason every command is: the allowlist is an *outer* middleware on
    `dispatcher.update`, and `event_from_user` is populated for a
    `callback_query` too — `test_callback_authorization.py` proves it.
    """
    post_id = parse_publish_callback(callback.data)
    if post_id is None or callback.from_user is None:
        logger.warning(
            "callback_rejected",
            reason="unparsable_callback_data",
            user_id=callback.from_user.id if callback.from_user is not None else None,
        )
        await callback.answer(UNKNOWN_CALLBACK_ANSWER)
        return

    bind_post_id(post_id)
    user_id = callback.from_user.id

    # `published_by_user_id` is written on success only, so until now a
    # refused or failed tap left no trace of who tapped at all (D-70).
    logger.info("publish_button_tapped", user_id=user_id, post_id=post_id)

    refusal: str | None = None
    draft_reference: tuple[int, int] | None = None

    async with session_scope(session_factory) as session:
        try:
            # Locked, not just read: this is the double-tap guard that the
            # partial unique index cannot be (two sends, two message ids).
            post = await session.get(Post, post_id, with_for_update={"nowait": True})
        except DBAPIError:
            await session.rollback()
            refusal = f"Пост {post_id} зараз обробляється. Спробуйте за кілька секунд."
            logger.warning(
                "publish_refused", reason="post_locked", user_id=user_id, post_id=post_id
            )
        else:
            refusal, draft_reference = _claim_post_for_publishing(post, post_id, user_id)

    if refusal is not None:
        await callback.answer(refusal, show_alert=True)
        return

    # After the commit, before the dispatch: removing it any earlier would
    # strand a post the database then declined to move.
    await _remove_publish_keyboard(bot, draft_reference)

    if not await _dispatch_publish_task(celery_client, post_id, user_id):
        await _mark_dispatch_failure(
            session_factory,
            post_id,
            expected_status=PostStatus.PUBLISHING,
            error=PUBLISH_DISPATCH_FAILED_ERROR,
        )
        await callback.answer(
            f"Не вдалося поставити задачу в чергу. Спробуйте /retry {post_id}.", show_alert=True
        )
        logger.warning(
            "publish_refused", reason="dispatch_failed", user_id=user_id, post_id=post_id
        )
        return

    await callback.answer("Публікую…")
    logger.info("publish_task_dispatched", user_id=user_id, post_id=post_id)


def _claim_post_for_publishing(
    post: Post | None, post_id: int, user_id: int
) -> tuple[str | None, tuple[int, int] | None]:
    """Moves a locked post into `publishing`, or explains why it cannot be.
    Returns `(refusal, draft_reference)` — exactly one of them is set. Each
    refusal is logged where its branch lives, with its own reason (D-70)."""
    if post is None:
        logger.warning("publish_refused", reason="post_not_found", user_id=user_id, post_id=post_id)
        return f"Пост {post_id} не знайдено.", None

    if post.channel_message_id is not None:
        logger.warning(
            "publish_refused", reason="already_published", user_id=user_id, post_id=post_id
        )
        return f"Пост {post_id} уже опубліковано.", None

    if post.status is PostStatus.PUBLISHING:
        # The `publishing` self-edge is for Celery redelivery, not a button:
        # `set_post_status` accepts it and a second tap would dispatch twice.
        logger.warning(
            "publish_refused", reason="already_publishing", user_id=user_id, post_id=post_id
        )
        return f"Пост {post_id} уже публікується.", None

    try:
        set_post_status(post, PostStatus.PUBLISHING)
    except IllegalStatusTransitionError:
        logger.warning(
            "publish_refused",
            reason="illegal_status",
            status=post.status.value,
            user_id=user_id,
            post_id=post_id,
        )
        return f"Пост {post_id} у стані «{post.status.value}» — публікація недоступна.", None

    if post.draft_chat_id is None or post.draft_message_id is None:
        return None, None
    return None, (post.draft_chat_id, post.draft_message_id)


async def _remove_publish_keyboard(bot: Bot, draft_reference: tuple[int, int] | None) -> None:
    """Cosmetic on its own — the status transition above is the real guard —
    so a failure here must not stop a publication the database already owns."""
    if draft_reference is None:
        return

    chat_id, message_id = draft_reference
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramAPIError:
        logger.warning("publish_keyboard_removal_failed", message_id=message_id, exc_info=True)


@router.message(F.text)
async def handle_link(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """Any plain-text message is treated as a link submission — commands
    above already claimed everything that starts with `/`. One link, or the
    «platform link + clip» pair D-69 accepts in a single message."""
    if message.from_user is None or message.text is None:
        return

    submitted_urls = _split_links(message.text)

    if len(submitted_urls) > 2:
        await message.answer(TOO_MANY_LINKS_ANSWER.format(count=len(submitted_urls)))
        logger.info("link_rejected", reason="too_many_links", link_count=len(submitted_urls))
        return

    classified_urls = [classify_url(submitted_url) for submitted_url in submitted_urls]

    for submitted_url, candidate in zip(submitted_urls, classified_urls, strict=True):
        rejection = _reject_url(submitted_url, candidate)
        if rejection is not None:
            answer, log_fields = rejection
            await message.answer(answer)
            logger.info("link_rejected", **log_fields)
            return

    clip_count = sum(1 for url in classified_urls if url.platform == CLIP_PLATFORM)
    if len(classified_urls) == 2 and clip_count != 1:
        both_clips = clip_count == 2
        await message.answer(TWO_CLIPS_ANSWER if both_clips else TWO_PLATFORM_LINKS_ANSWER)
        logger.info(
            "link_rejected",
            reason="two_clips" if both_clips else "two_platform_links",
            platforms=[url.platform for url in classified_urls],
        )
        return

    raw_url, classified, clip = _select_track_source(submitted_urls, classified_urls)

    post_id: int | None = None
    track_existed = False
    failure_answer: str | None = None

    async with session_scope(session_factory) as session:
        try:
            track, track_existed = await _get_or_create_track(session, raw_url, classified, clip)

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
    logger.info(
        "link_received",
        post_id=post_id,
        track_existed=track_existed,
        link_count=len(submitted_urls),
        clip_attached=clip is not None,
    )

    if not await _dispatch_resolve_task(celery_client, post_id):
        await _mark_dispatch_failure(
            session_factory,
            post_id,
            expected_status=PostStatus.RESOLVING,
            error=DISPATCH_FAILED_ERROR,
        )
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


def _split_links(text: str) -> list[str]:
    """Whitespace-separated links, but only when *every* token is one: a
    message with prose around a link stays a single unrecognized submission,
    answered exactly as it was before D-69."""
    tokens = text.split()
    if len(tokens) > 1 and all(is_safe_url(token) for token in tokens):
        return tokens
    return [text.strip()]


def _reject_url(raw_url: str, classified: ClassifiedUrl) -> tuple[str, dict[str, object]] | None:
    """The three syntactic refusals as `(answer, log fields)`, so a pair is
    validated link by link rather than as one opaque blob."""
    # UNIQUE on a btree tops out near 2704 bytes and raises
    # ProgramLimitExceededError, which `except IntegrityError` misses (F-22).
    if not is_within_length_limit(raw_url) or not is_within_length_limit(classified.normalized_url):
        return (
            f"Посилання задовге (ліміт {MAX_NORMALIZED_URL_BYTES} байтів). "
            f"Надішліть коротке посилання на трек.",
            {
                "reason": "too_long",
                "raw_url_bytes": len(raw_url.encode("utf-8")),
                "normalized_url_bytes": len(classified.normalized_url.encode("utf-8")),
                "limit_bytes": MAX_NORMALIZED_URL_BYTES,
            },
        )

    if classified.kind is UrlKind.NOT_TRACK:
        reason = NOT_TRACK_REASON_UA.get(classified.not_track_reason or "", "не трек")
        return (
            f"Це посилання на {reason}, а не на трек. Надішліть пряме посилання на трек.",
            {
                "reason": "not_track",
                "not_track_reason": classified.not_track_reason,
                "normalized_url": classified.normalized_url,
            },
        )

    if classified.kind is UrlKind.UNKNOWN:
        # Only the length: the raw text of an unrecognized message is not a
        # URL and has no business in the logs.
        return (
            "Не розпізнав це як посилання на трек.",
            {"reason": "unknown", "raw_text_length": len(raw_url)},
        )

    return None


def _select_track_source(
    submitted_urls: list[str], classified_urls: list[ClassifiedUrl]
) -> tuple[str, ClassifiedUrl, ClassifiedUrl | None]:
    """Which link becomes `track.source_url` and which is seeded as the clip.
    A pair resolves from its platform half (D-69): the clip's own metadata is
    the raw YouTube caption, which is the whole reason the pair exists."""
    clip = next((url for url in classified_urls if url.platform == CLIP_PLATFORM), None)

    for submitted_url, classified in zip(submitted_urls, classified_urls, strict=True):
        if classified.platform != CLIP_PLATFORM:
            return submitted_url, classified, clip

    return submitted_urls[0], classified_urls[0], clip


def _seed_user_input_clip(track: Track, clip: ClassifiedUrl | None) -> None:
    """Seeded immediately, not just at resolve time — a user-supplied clip
    must survive even if resolution never completes. `manual` is the top tier,
    so `merge_stored_platform_links` carries it into every later resolve, and
    the admin's word is the only thing binding it to a platform link (D-69)."""
    if clip is None:
        return

    stored = track.platform_links or {}
    link = {"url": clip.normalized_url, "source": USER_INPUT_SOURCE, "match": "manual"}
    if stored.get(CLIP_PLATFORM) == link:
        return

    # Reassigned, never mutated in place: the column is plain JSONB, so an
    # in-place edit would never be seen as dirty and would not be written.
    track.platform_links = {**stored, CLIP_PLATFORM: link}


async def _get_or_create_track(
    session: AsyncSession,
    raw_url: str,
    classified: ClassifiedUrl,
    clip: ClassifiedUrl | None = None,
) -> tuple[Track, bool]:
    """Race-safe get-or-create on `source_url_normalized` (UNIQUE): two
    concurrent submissions of the same link both miss the SELECT, one wins
    the INSERT, the other re-reads the winner's row instead of erroring.
    The clip is seeded before the flush — `platform_links` has a server
    default, and re-reading one afterwards is a lazy load in an async session.
    """
    existing = await _select_track_by_url(session, classified.normalized_url)
    if existing is not None:
        _seed_user_input_clip(existing, clip)
        return existing, True

    # `DBAPIError` is deliberately not caught here — the caller turns it into
    # an answer to the admin, and this function has no message of its own.
    track = Track(source_url=raw_url, source_url_normalized=classified.normalized_url)
    _seed_user_input_clip(track, clip)

    session.add(track)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _select_track_by_url(session, classified.normalized_url)
        if existing is not None:
            _seed_user_input_clip(existing, clip)
            return existing, True
        raise
    return track, False


async def _select_track_by_url(session: AsyncSession, normalized_url: str) -> Track | None:
    return await session.scalar(select(Track).where(Track.source_url_normalized == normalized_url))


async def _mark_dispatch_failure(
    session_factory: async_sessionmaker[AsyncSession],
    post_id: int,
    *,
    expected_status: PostStatus,
    error: str,
) -> None:
    """D-53: the post is already committed as `RESOLVING`/`PUBLISHING`, so
    leaving it there makes the bot's own "спробуйте /retry" advice false for
    the next `TASK_TIME_LIMIT_SECONDS` — no task exists to move it on.
    """
    try:
        async with session_scope(session_factory) as session:
            post = await session.get(Post, post_id, with_for_update=True)
            if post is None or post.status is not expected_status:
                return

            set_post_status(post, PostStatus.FAILED, error=error)
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


async def _dispatch_publish_task(
    celery_client: Celery, post_id: int, published_by_user_id: int
) -> bool:
    """By name, never by importing the task: `bot/` must not reach into
    `worker/tasks` (AGENTS.md). `published_by_user_id` is whoever tapped the
    button or ran `/retry`, not whoever submitted the link (D-62)."""
    try:
        await asyncio.to_thread(
            celery_client.send_task,
            PUBLISH_POST_TASK_NAME,
            kwargs={"post_id": post_id, "published_by_user_id": published_by_user_id},
        )
    except Exception:
        logger.exception("publish_task_dispatch_failed", post_id=post_id)
        return False
    return True
