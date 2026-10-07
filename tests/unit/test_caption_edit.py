"""Caption editing and the existence probe. The error texts below were measured
against the live API in a private chat; anything else Telegram says must propagate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import EditMessageCaption

from pisniar.db.states import PostStatus
from pisniar.telegram import caption_edit
from pisniar.telegram.caption_edit import (
    EditOutcome,
    MessageProbe,
    MessageRole,
    UnsupportedMessageKindError,
    classify_edit_error,
    edit_message_caption_by_kind,
    keyboard_for_state,
    probe_message,
)

NOT_MODIFIED_TEXT = (
    "Bad Request: message is not modified: specified new message content and reply markup "
    "are exactly the same as a current content and reply markup of the message"
)
NOT_FOUND_TEXT = "Bad Request: message to edit not found"
NO_CAPTION_TEXT = "Bad Request: there is no caption in the message to edit"
NO_TEXT_TEXT = "Bad Request: there is no text in the message to edit"
METHOD = EditMessageCaption(chat_id=1, message_id=2, caption="x")


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=METHOD, message=text)


def make_bot(**side_effects: Exception) -> AsyncMock:
    bot = AsyncMock()
    for method_name, error in side_effects.items():
        getattr(bot, method_name).side_effect = error
    return bot


def test_the_measured_not_modified_text_is_classified() -> None:
    assert classify_edit_error(bad_request(NOT_MODIFIED_TEXT)) is EditOutcome.NOT_MODIFIED


def test_the_measured_not_found_text_is_classified() -> None:
    assert classify_edit_error(bad_request(NOT_FOUND_TEXT)) is EditOutcome.MESSAGE_MISSING


@pytest.mark.parametrize(
    "text",
    [
        NO_CAPTION_TEXT,
        NO_TEXT_TEXT,
        "Bad Request: message can't be edited",
        "Bad Request: chat not found",
        "Bad Request: MESSAGE_ID_INVALID",
        "Bad Request: message to edit",
        "Bad Request: not found",
        "",
    ],
)
def test_any_other_bad_request_is_not_classified(text: str) -> None:
    """a "not found" in other words must never be read as a deletion."""
    assert classify_edit_error(bad_request(text)) is None


def test_a_non_bad_request_with_a_measured_phrase_is_not_classified() -> None:
    forbidden = TelegramForbiddenError(
        method=METHOD, message="Forbidden: message to edit not found"
    )

    assert classify_edit_error(forbidden) is None
    assert classify_edit_error(TelegramNetworkError(method=METHOD, message="boom")) is None


def test_a_not_found_answer_is_not_a_success() -> None:
    assert EditOutcome.MESSAGE_MISSING is not EditOutcome.EDITED
    assert classify_edit_error(bad_request(NOT_FOUND_TEXT)) is not EditOutcome.NOT_MODIFIED


@pytest.mark.parametrize("message_kind", ["photo", "video"])
async def test_a_photo_or_video_is_edited_as_a_caption_with_html(message_kind: str) -> None:
    bot = make_bot()

    outcome = await edit_message_caption_by_kind(bot, -100, 7, message_kind, "<b>new</b>")

    assert outcome is EditOutcome.EDITED
    bot.edit_message_caption.assert_awaited_once()
    sent = bot.edit_message_caption.await_args.kwargs
    assert sent["chat_id"] == -100
    assert sent["message_id"] == 7
    assert sent["caption"] == "<b>new</b>"
    assert sent["parse_mode"] == "HTML"
    assert sent["reply_markup"] is None
    bot.edit_message_text.assert_not_awaited()


async def test_a_text_message_is_edited_as_text_without_a_link_preview() -> None:
    bot = make_bot()

    outcome = await edit_message_caption_by_kind(bot, 5, 7, "text", "<b>new</b>")

    assert outcome is EditOutcome.EDITED
    sent = bot.edit_message_text.await_args.kwargs
    assert sent["text"] == "<b>new</b>"
    assert sent["parse_mode"] == "HTML"
    assert sent["link_preview_options"].is_disabled is True
    bot.edit_message_caption.assert_not_awaited()


async def test_the_given_keyboard_travels_with_the_edit() -> None:
    bot = make_bot()
    keyboard = keyboard_for_state(PostStatus.RESOLVED, MessageRole.DRAFT, 7, 5, None)

    await edit_message_caption_by_kind(bot, 5, 7, "text", "x", keyboard)

    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is keyboard


@pytest.mark.parametrize(
    ("method_name", "message_kind"),
    [("edit_message_caption", "photo"), ("edit_message_text", "text")],
)
async def test_not_modified_is_a_successful_outcome(method_name: str, message_kind: str) -> None:
    bot = make_bot(**{method_name: bad_request(NOT_MODIFIED_TEXT)})

    outcome = await edit_message_caption_by_kind(bot, 5, 7, message_kind, "x")

    assert outcome is EditOutcome.NOT_MODIFIED


async def test_a_missing_message_is_reported_not_raised() -> None:
    bot = make_bot(edit_message_caption=bad_request(NOT_FOUND_TEXT))

    outcome = await edit_message_caption_by_kind(bot, -100, 7, "photo", "x")

    assert outcome is EditOutcome.MESSAGE_MISSING


@pytest.mark.parametrize(
    "error",
    [
        bad_request(NO_CAPTION_TEXT),
        bad_request("Bad Request: message can't be edited"),
        TelegramForbiddenError(method=METHOD, message="Forbidden: bot was kicked"),
        TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=11),
        TelegramNetworkError(method=METHOD, message="timeout"),
    ],
)
async def test_every_unmeasured_answer_propagates(error: Exception) -> None:
    bot = make_bot(edit_message_caption=error)

    with pytest.raises(type(error)):
        await edit_message_caption_by_kind(bot, -100, 7, "photo", "x")


async def test_an_unknown_message_kind_is_refused_before_anything_is_sent() -> None:
    bot = make_bot()

    with pytest.raises(UnsupportedMessageKindError):
        await edit_message_caption_by_kind(bot, -100, 7, "audio", "x")

    bot.edit_message_caption.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()


async def test_a_not_modified_probe_answer_means_the_message_lives() -> None:
    bot = make_bot(edit_message_reply_markup=bad_request(NOT_MODIFIED_TEXT))

    assert await probe_message(bot, -100, 7) is MessageProbe.EXISTS

    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=-100, message_id=7, reply_markup=None
    )


async def test_the_probe_reads_not_found_as_a_deleted_message() -> None:
    bot = make_bot(edit_message_reply_markup=bad_request(NOT_FOUND_TEXT))

    assert await probe_message(bot, -100, 7) is MessageProbe.MISSING


async def test_a_probe_that_succeeds_found_a_live_message() -> None:
    assert await probe_message(make_bot(), -100, 7) is MessageProbe.EXISTS


@pytest.mark.parametrize(
    "error",
    [
        bad_request("Bad Request: chat not found"),
        bad_request("Bad Request: message not found"),
        TelegramForbiddenError(method=METHOD, message="Forbidden: bot was kicked"),
        TelegramNetworkError(method=METHOD, message="timeout"),
    ],
)
async def test_a_doubtful_probe_answer_never_proves_a_deletion(error: Exception) -> None:
    bot = make_bot(edit_message_reply_markup=error)

    with pytest.raises(type(error)):
        await probe_message(bot, -100, 7)


def test_a_resolved_draft_carries_the_draft_keyboard() -> None:
    keyboard = keyboard_for_state(PostStatus.RESOLVED, MessageRole.DRAFT, 7, 5, "https://x.example")

    assert keyboard is not None
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "Опублікувати в канал",
        "Зробити відео",
        "Редагувати",
    ]


def test_a_rendered_preview_carries_the_preview_keyboard() -> None:
    keyboard = keyboard_for_state(PostStatus.RENDERED, MessageRole.PREVIEW, 7, 5, None)

    assert keyboard is not None
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "Опублікувати в канал"
    ]


@pytest.mark.parametrize(
    ("status", "role"),
    [
        (PostStatus.RENDERED, MessageRole.DRAFT),
        (PostStatus.RESOLVED, MessageRole.PREVIEW),
        (PostStatus.FAILED, MessageRole.DRAFT),
        (PostStatus.FAILED, MessageRole.PREVIEW),
        (PostStatus.PUBLISHED, MessageRole.DRAFT),
        (PostStatus.PUBLISHING, MessageRole.PREVIEW),
        (PostStatus.DOWNLOADING, MessageRole.DRAFT),
    ],
)
def test_a_message_in_any_other_state_carries_no_keyboard(
    status: PostStatus, role: MessageRole
) -> None:
    assert keyboard_for_state(status, role, 7, 5, "https://x.example") is None


def test_the_phrases_are_the_measured_ones() -> None:
    assert caption_edit.MESSAGE_NOT_MODIFIED_PHRASE in NOT_MODIFIED_TEXT
    assert caption_edit.MESSAGE_NOT_FOUND_PHRASE in NOT_FOUND_TEXT
