"""Inline keyboards and the `callback_data` grammar behind them. Deliberately
a leaf over `aiogram` alone: `bot/` has to parse a callback without importing
`post/`, `db/` or `worker/` (AGENTS.md boundaries), and `telegram.draft` has
to build the same buttons from inside the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PUBLISH_CALLBACK_PREFIX = "publish"
PUBLISH_BUTTON_TEXT = "Опублікувати в канал"

# D-77: the media chain is started by a second button, never automatically —
# disk and minutes of CPU are spent only on a draft the admin approved.
RENDER_CALLBACK_PREFIX = "render"
RENDER_BUTTON_TEXT = "Зробити відео"

# Telegram caps `callback_data` at 64 bytes; a BIGINT post id never needs
# more digits than this, and anything longer is not one of ours.
MAX_CALLBACK_POST_ID_DIGITS = 19


class CallbackAction(StrEnum):
    PUBLISH = PUBLISH_CALLBACK_PREFIX
    RENDER = RENDER_CALLBACK_PREFIX


@dataclass(frozen=True)
class ParsedCallback:
    action: CallbackAction
    post_id: int


def draft_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Both triggers under a text draft: publish the cover as a photo, or
    spend the disk and the minutes on a video first (D-46 + D-77)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(PUBLISH_BUTTON_TEXT, CallbackAction.PUBLISH, post_id)],
            [_button(RENDER_BUTTON_TEXT, CallbackAction.RENDER, post_id)],
        ]
    )


def preview_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """The video preview carries publication alone — there is nothing left to
    render, and the old draft's keyboard is removed when this one arrives."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(PUBLISH_BUTTON_TEXT, CallbackAction.PUBLISH, post_id)]]
    )


def parse_callback(callback_data: str | None) -> ParsedCallback | None:
    """The action and the post id, or `None` for anything that is not exactly
    `<known prefix>:<ascii digits>`. The payload arrives from a Telegram
    client, so it is arbitrary input — and it selects the row to act on.
    """
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


def _button(text: str, action: CallbackAction, post_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{action.value}:{post_id}")
