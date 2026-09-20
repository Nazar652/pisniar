"""Inline keyboards and the `callback_data` grammar behind them. Deliberately
a leaf over `aiogram` alone: `bot/` has to parse a callback without importing
`post/`, `db/` or `worker/` (AGENTS.md boundaries), and `telegram.draft` has
to build the same button from inside the worker.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PUBLISH_CALLBACK_PREFIX = "publish"
PUBLISH_BUTTON_TEXT = "Опублікувати в канал"

# Telegram caps `callback_data` at 64 bytes; a BIGINT post id never needs
# more digits than this, and anything longer is not one of ours.
MAX_CALLBACK_POST_ID_DIGITS = 19


def publish_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """The one button under a draft (D-46) — the whole publish trigger."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=PUBLISH_BUTTON_TEXT,
                    callback_data=f"{PUBLISH_CALLBACK_PREFIX}:{post_id}",
                )
            ]
        ]
    )


def parse_publish_callback(callback_data: str | None) -> int | None:
    """The post id, or `None` for anything that is not exactly
    `publish:<ascii digits>`. The payload arrives from a Telegram client, so
    it is arbitrary input — and it is about to select the row to publish.
    """
    if callback_data is None:
        return None

    prefix, separator, raw_post_id = callback_data.partition(":")
    if separator != ":" or prefix != PUBLISH_CALLBACK_PREFIX:
        return None

    # `isascii()` as well as `isdigit()`: the latter accepts Arabic-Indic and
    # other Unicode digits, which `int()` would then happily convert.
    if not raw_post_id.isascii() or not raw_post_id.isdigit():
        return None
    if len(raw_post_id) > MAX_CALLBACK_POST_ID_DIGITS:
        return None

    return int(raw_post_id)
