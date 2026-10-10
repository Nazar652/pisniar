"""Applying a rebuilt caption to the messages Telegram holds, and archiving a post whose channel
message is gone. The post row is locked through one Telegram call and the track row never;
Telegram is edited first and the database second.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.locks import lock_post_without_waiting
from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus, set_post_status
from pisniar.logging import get_logger
from pisniar.post.caption import MEDIA_CAPTION_LIMIT
from pisniar.telegram.caption_edit import (
    TEXT_MESSAGE_KIND,
    EditOutcome,
    MessageProbe,
    MessageRole,
    edit_message_caption_by_kind,
    keyboard_for_state,
    probe_message,
)
from pisniar.telegram.publish import describe_publish_error
from pisniar.webapp.editing import refuse_unless_editable
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import ApplyResponse, ArchiveResponse
from pisniar.webapp.service import caption_sha256, rebuild_caption
from pisniar.webapp.status import STATUS_LABELS

logger = get_logger(__name__)

TELEGRAM_TIMEOUT_SECONDS = 15
VIDEO_MESSAGE_KIND = "video"
CAPTION_CHANGED_MESSAGE = "Підпис змінився відтоді, як ви його переглядали. Перегляньте його ще раз."
CAPTION_UNAVAILABLE_MESSAGE = "У треку ще немає назви, підпис побудувати не можна."
MESSAGE_MISSING_MESSAGE = "Повідомлення в Telegram видалено або недоступне. Підпис у базі не змінено."
TELEGRAM_FAILED_MESSAGE = "Telegram не прийняв зміну. Нічого не змінено, спробуйте пізніше."
NOT_PUBLISHED_MESSAGE = "Архівувати за зниклим повідомленням можна лише опублікований пост."
CHANNEL_MESSAGE_EXISTS_MESSAGE = "Повідомлення в каналі існує, тому пост не архівовано."

ApplyResult = Literal["edited", "not_modified", "stored_only"]


@dataclass(frozen=True)
class EditTarget:
    """one message that carries the caption, with the keyboard its state calls for."""

    role: str
    chat_id: int
    message_id: int
    message_kind: str
    reply_markup: InlineKeyboardMarkup | None


@dataclass(frozen=True)
class AppliedCaption:
    """the stored result, plus the messages still to be edited once the lock is gone."""

    response: ApplyResponse
    html: str
    remaining: list[EditTarget]


def message_targets(post: Post, webapp_public_url: str | None) -> list[EditTarget]:
    """the first target decides the apply; the others follow it after the commit."""
    status = PostStatus(post.status)
    if status is PostStatus.PUBLISHED:
        if post.channel_chat_id is None or post.channel_message_id is None:
            return []
        channel = EditTarget(
            "channel",
            post.channel_chat_id,
            post.channel_message_id,
            post.channel_message_kind or TEXT_MESSAGE_KIND,
            None,
        )
        return [channel]

    draft = ("draft", post.draft_chat_id, post.draft_message_id, TEXT_MESSAGE_KIND)
    preview = ("preview", post.preview_chat_id, post.preview_message_id, VIDEO_MESSAGE_KIND)
    ordered = [preview, draft] if status is PostStatus.RENDERED else [draft, preview]

    targets = []
    for role, chat_id, message_id, kind in ordered:
        if chat_id is None or message_id is None:
            continue
        keyboard = keyboard_for_state(status, MessageRole(role), post.id, chat_id, webapp_public_url)
        targets.append(EditTarget(role, chat_id, message_id, kind, keyboard))
    return targets


def refuse_telegram_error(
    error: Exception,
    event: str,
    user_id: int,
    post_id: int,
) -> NoReturn:
    """a rate limit says how long to wait, a 403 says what the bot lost, anything else gets a
    general sentence while the full error stays in the log."""
    if isinstance(error, TelegramRetryAfter):
        logger.warning(event, reason="rate_limited", user_id=user_id, post_id=post_id)
        raise ApiProblem(
            429,
            "telegram_rate_limited",
            f"Telegram просить зачекати {error.retry_after} с. Нічого не змінено.",
        )
    if isinstance(error, TelegramForbiddenError):
        logger.warning(event, reason="forbidden", user_id=user_id, post_id=post_id)
        raise ApiProblem(502, "telegram_forbidden", describe_publish_error(error))

    logger.error(event, reason="telegram_error", user_id=user_id, post_id=post_id, exc_info=error)
    raise ApiProblem(502, "telegram_failed", TELEGRAM_FAILED_MESSAGE)


def refuse_apply(
    reason: str,
    user_id: int,
    post_id: int,
    problem: ApiProblem,
) -> NoReturn:
    logger.warning("caption_apply_refused", reason=reason, user_id=user_id, post_id=post_id)
    raise problem


async def apply_caption(
    session: AsyncSession,
    bot: Bot,
    post_id: int,
    expected_sha: str,
    user_id: int,
    webapp_public_url: str | None,
) -> AppliedCaption | None:
    """the caller commits. The track is read without a lock; Telegram is edited before the
    caption is stored, and a refused edit raises with nothing written."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    refuse_unless_editable(post, user_id, "caption_apply_refused")
    track = await session.get(Track, post.track_id)
    view, html = rebuild_caption(post, track) if track is not None else (None, None)
    if view is None or html is None:
        problem = ApiProblem(409, "caption_unavailable", CAPTION_UNAVAILABLE_MESSAGE)
        refuse_apply("no_caption", user_id, post_id, problem)

    if caption_sha256(html) != expected_sha:
        problem = ApiProblem(409, "caption_changed", CAPTION_CHANGED_MESSAGE)
        refuse_apply("stale", user_id, post_id, problem)
    if view.visible_length > MEDIA_CAPTION_LIMIT:
        message = f"Підпис задовгий: {view.visible_length} символів із {MEDIA_CAPTION_LIMIT}."
        refuse_apply("too_long", user_id, post_id, ApiProblem(422, "caption_too_long", message))

    targets = message_targets(post, webapp_public_url)
    result: ApplyResult = "stored_only"
    if targets:
        first = targets[0]
        try:
            outcome = await edit_message_caption_by_kind(
                bot,
                first.chat_id,
                first.message_id,
                first.message_kind,
                html,
                first.reply_markup,
                request_timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except (TelegramAPIError, ValueError) as error:
            refuse_telegram_error(error, "caption_apply_refused", user_id, post_id)

        if outcome is EditOutcome.MESSAGE_MISSING:
            problem = ApiProblem(409, "channel_message_missing", MESSAGE_MISSING_MESSAGE)
            refuse_apply("message_missing", user_id, post_id, problem)
        result = "edited" if outcome is EditOutcome.EDITED else "not_modified"

    if post.caption != html:
        post.caption = html
    await session.flush()

    logger.info(
        "caption_applied",
        user_id=user_id,
        post_id=post_id,
        targets=[target.role for target in targets],
        result=result,
    )
    response = ApplyResponse(caption_sha256=expected_sha, result=result, other_messages_failed=[])
    return AppliedCaption(response=response, html=html, remaining=targets[1:])


async def edit_remaining(
    bot: Bot,
    applied: AppliedCaption,
    user_id: int,
    post_id: int,
) -> ApplyResponse:
    """best effort after the commit: a failure here is logged and named, never undone."""
    failed: list[str] = []
    for target in applied.remaining:
        try:
            outcome = await edit_message_caption_by_kind(
                bot,
                target.chat_id,
                target.message_id,
                target.message_kind,
                applied.html,
                target.reply_markup,
                request_timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except (TelegramAPIError, ValueError):
            logger.warning(
                "caption_apply_secondary_failed",
                user_id=user_id,
                post_id=post_id,
                role=target.role,
                exc_info=True,
            )
            failed.append(target.role)
            continue
        if outcome is EditOutcome.MESSAGE_MISSING:
            failed.append(target.role)

    return applied.response.model_copy(update={"other_messages_failed": failed})


def refuse_archive(
    reason: str,
    user_id: int,
    post_id: int,
    problem: ApiProblem,
) -> NoReturn:
    logger.warning("post_archive_refused", reason=reason, user_id=user_id, post_id=post_id)
    raise problem


async def archive_post(
    session: AsyncSession,
    bot: Bot,
    post_id: int,
    user_id: int,
) -> ArchiveResponse | None:
    """only a channel message Telegram answers «not found» for archives the post; the track is
    never locked and a doubtful answer changes nothing."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    not_published = ApiProblem(409, "not_published", NOT_PUBLISHED_MESSAGE)
    if post.status is not PostStatus.PUBLISHED:
        refuse_archive("not_published", user_id, post_id, not_published)
    if post.channel_chat_id is None or post.channel_message_id is None:
        refuse_archive("no_reference", user_id, post_id, not_published)

    try:
        probe = await probe_message(
            bot,
            post.channel_chat_id,
            post.channel_message_id,
            request_timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
    except TelegramAPIError as error:
        refuse_telegram_error(error, "post_archive_refused", user_id, post_id)

    if probe is MessageProbe.EXISTS:
        exists = ApiProblem(409, "channel_message_exists", CHANNEL_MESSAGE_EXISTS_MESSAGE)
        refuse_archive("message_exists", user_id, post_id, exists)

    set_post_status(post, PostStatus.ARCHIVED)
    await session.flush()
    logger.info("post_archived_channel_message_missing", user_id=user_id, post_id=post_id)
    return ArchiveResponse(post_id=post_id, status_label=STATUS_LABELS[PostStatus.ARCHIVED])
