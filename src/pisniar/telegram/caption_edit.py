"""Editing the caption of a message the bot already sent, and checking whether one still exists.

Telegram's errors are classified only by the exact phrases it was measured to return; any
other answer propagates, so an unknown failure is never read as success or as a deletion.
"""

from __future__ import annotations

from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from pisniar.db.states import PostStatus
from pisniar.logging import get_logger
from pisniar.telegram.keyboards import draft_keyboard, preview_keyboard

logger = get_logger(__name__)

# measured against the live API: both are `400 Bad Request` with these phrases inside
MESSAGE_NOT_MODIFIED_PHRASE = "message is not modified"
MESSAGE_NOT_FOUND_PHRASE = "message to edit not found"

TEXT_MESSAGE_KIND = "text"
CAPTION_MESSAGE_KINDS = frozenset({"photo", "video"})


class MessageRole(StrEnum):
    DRAFT = "draft"
    PREVIEW = "preview"


class EditOutcome(StrEnum):
    EDITED = "edited"
    NOT_MODIFIED = "not_modified"
    MESSAGE_MISSING = "message_missing"


class MessageProbe(StrEnum):
    EXISTS = "exists"
    MISSING = "missing"


class UnsupportedMessageKindError(ValueError):
    """a kind outside `photo`, `video` and `text` is not a message this module can edit."""


def classify_edit_error(error: TelegramAPIError) -> EditOutcome | None:
    """`None` for every answer that is neither measured phrase: the caller re-raises it."""
    if not isinstance(error, TelegramBadRequest):
        return None
    if MESSAGE_NOT_MODIFIED_PHRASE in error.message:
        return EditOutcome.NOT_MODIFIED
    if MESSAGE_NOT_FOUND_PHRASE in error.message:
        return EditOutcome.MESSAGE_MISSING
    return None


async def edit_message_caption_by_kind(
    bot: Bot,
    chat_id: int,
    message_id: int,
    message_kind: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> EditOutcome:
    """photo and video carry a caption, a text message carries it as its text. An edit without
    `reply_markup` removes the keyboard, so the caller passes the one that belongs."""
    try:
        if message_kind in CAPTION_MESSAGE_KINDS:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        elif message_kind == TEXT_MESSAGE_KIND:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=caption,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=reply_markup,
            )
        else:
            raise UnsupportedMessageKindError(f"message kind {message_kind!r} cannot be edited")
    except TelegramAPIError as error:
        outcome = classify_edit_error(error)
        if outcome is None:
            raise
        logger.info(
            "caption_edit_answered",
            chat_id=chat_id,
            message_id=message_id,
            message_kind=message_kind,
            outcome=outcome.value,
        )
        return outcome

    return EditOutcome.EDITED


async def probe_message(bot: Bot, chat_id: int, message_id: int) -> MessageProbe:
    """clearing the keyboard of a keyboardless message changes nothing: live answers «not modified»,
    deleted «not found». Anything else raises, so a doubtful answer never proves a deletion."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramAPIError as error:
        outcome = classify_edit_error(error)
        if outcome is EditOutcome.NOT_MODIFIED:
            return MessageProbe.EXISTS
        if outcome is EditOutcome.MESSAGE_MISSING:
            return MessageProbe.MISSING
        raise

    return MessageProbe.EXISTS


def keyboard_for_state(
    status: PostStatus,
    role: MessageRole,
    post_id: int,
    chat_id: int,
    webapp_public_url: str | None,
) -> InlineKeyboardMarkup | None:
    """the keyboard a draft or a preview carries in this state; a message in any other
    state carries none, and editing it must not bring a stale button back."""
    if status is PostStatus.RESOLVED and role is MessageRole.DRAFT:
        return draft_keyboard(post_id, chat_id, webapp_public_url)
    if status is PostStatus.RENDERED and role is MessageRole.PREVIEW:
        return preview_keyboard(post_id, chat_id, webapp_public_url)
    return None
