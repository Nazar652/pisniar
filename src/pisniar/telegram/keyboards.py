"""Inline keyboards and the `callback_data` grammar behind them. Deliberately a leaf over `aiogram` alone: `bot/` has
to parse a callback without importing `post/`, `db/` or `worker/` (AGENTS.md boundaries), and `telegram.draft` has
to build the same buttons from inside the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

PUBLISH_CALLBACK_PREFIX = "publish"
PUBLISH_BUTTON_TEXT = "Опублікувати в канал"

# the media chain is started by a second button, never automatically —
# disk and minutes of CPU are spent only on a draft the admin approved.
RENDER_CALLBACK_PREFIX = "render"
RENDER_BUTTON_TEXT = "Зробити відео"

EDIT_BUTTON_TEXT = "Редагувати"
POST_LIST_BUTTON_TEXT = "Пости"

# telegram caps `callback_data` at 64 bytes; a BIGINT post id never needs
# more digits than this, and anything longer is not one of ours.
MAX_CALLBACK_POST_ID_DIGITS = 19


class CallbackAction(StrEnum):
    PUBLISH = PUBLISH_CALLBACK_PREFIX
    RENDER = RENDER_CALLBACK_PREFIX


@dataclass(frozen=True)
class ParsedCallback:
    action: CallbackAction
    post_id: int


def webapp_url(webapp_public_url: str, post_id: int | None = None) -> str:
    """the editor of one post, or the list of posts when no id is given."""
    if post_id is None:
        return f"{webapp_public_url}/"
    return f"{webapp_public_url}/?post_id={post_id}"


def edit_button_rows(
    chat_id: int | None,
    webapp_public_url: str | None,
    post_id: int,
) -> list[list[InlineKeyboardButton]]:
    """empty unless the chat is private and the Mini App is configured: telegram answers 400 to a
    `web_app` button in a group, and that would cost the whole message."""
    if webapp_public_url is None or chat_id is None or chat_id <= 0:
        return []
    return [
        [
            InlineKeyboardButton(
                text=EDIT_BUTTON_TEXT,
                web_app=WebAppInfo(url=webapp_url(webapp_public_url, post_id)),
            )
        ]
    ]


def post_list_keyboard(chat_id: int, webapp_public_url: str | None) -> InlineKeyboardMarkup | None:
    if webapp_public_url is None or chat_id <= 0:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=POST_LIST_BUTTON_TEXT,
                    web_app=WebAppInfo(url=webapp_url(webapp_public_url)),
                )
            ]
        ]
    )


def draft_keyboard(
    post_id: int,
    chat_id: int | None = None,
    webapp_public_url: str | None = None,
) -> InlineKeyboardMarkup:
    """under a text draft: the video trigger and the editor where it may open; the cover alone
    is published from the editor, never from the chat."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [callback_button(RENDER_BUTTON_TEXT, CallbackAction.RENDER, post_id)],
            *edit_button_rows(chat_id, webapp_public_url, post_id),
        ]
    )


def preview_keyboard(
    post_id: int,
    chat_id: int | None = None,
    webapp_public_url: str | None = None,
) -> InlineKeyboardMarkup:
    """the video preview carries publication alone — there is nothing left to
    render, and the text draft's keyboard is removed when this one arrives."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [callback_button(PUBLISH_BUTTON_TEXT, CallbackAction.PUBLISH, post_id)],
            *edit_button_rows(chat_id, webapp_public_url, post_id),
        ]
    )


def published_keyboard(
    post_id: int,
    chat_id: int,
    webapp_public_url: str | None,
) -> InlineKeyboardMarkup | None:
    """under the publication confirmation: the editor alone, `None` where it cannot open."""
    rows = edit_button_rows(chat_id, webapp_public_url, post_id)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def parse_callback(callback_data: str | None) -> ParsedCallback | None:
    """`None` for anything but `<known prefix>:<ascii digits>`: the payload is
    arbitrary client input, and it selects the row to act on."""
    if callback_data is None:
        return None

    prefix, separator, raw_post_id = callback_data.partition(":")
    if separator != ":":
        return None

    try:
        action = CallbackAction(prefix)
    except ValueError:
        return None

    # `isascii()` as well as `isdigit()`: the latter accepts Arabic-Indic and
    # other Unicode digits, which `int()` would then happily convert.
    if not raw_post_id.isascii() or not raw_post_id.isdigit():
        return None
    if len(raw_post_id) > MAX_CALLBACK_POST_ID_DIGITS:
        return None

    return ParsedCallback(action=action, post_id=int(raw_post_id))


def callback_button(
    text: str,
    action: CallbackAction,
    post_id: int,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{action.value}:{post_id}")
