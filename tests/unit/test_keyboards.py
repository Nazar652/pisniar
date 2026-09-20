"""`publish_keyboard`/`parse_publish_callback`: the one button under a draft
and the grammar of the payload it sends back. The payload arrives from a
Telegram client and selects the row to publish, so everything that is not
exactly `publish:<ascii digits>` must be refused."""

from __future__ import annotations

import pytest

from pisniar.telegram.keyboards import (
    MAX_CALLBACK_POST_ID_DIGITS,
    PUBLISH_BUTTON_TEXT,
    parse_publish_callback,
    publish_keyboard,
)


def test_publish_keyboard_carries_one_button_with_the_post_id() -> None:
    markup = publish_keyboard(42)

    (row,) = markup.inline_keyboard
    (button,) = row
    assert button.text == PUBLISH_BUTTON_TEXT
    assert button.callback_data == "publish:42"


def test_publish_keyboard_callback_data_fits_telegram_s_64_byte_cap() -> None:
    markup = publish_keyboard(9_223_372_036_854_775_807)

    callback_data = markup.inline_keyboard[0][0].callback_data
    assert callback_data is not None
    assert len(callback_data.encode("utf-8")) <= 64


def test_a_button_s_own_payload_round_trips() -> None:
    markup = publish_keyboard(7)

    assert parse_publish_callback(markup.inline_keyboard[0][0].callback_data) == 7


@pytest.mark.parametrize(
    "callback_data",
    [
        None,
        "",
        "publish",
        "publish:",
        "publish:abc",
        "publish:1;2",
        "publish:-1",
        "publish: 1",
        "archive:1",
        "publishx:1",
        ":1",
        # `isdigit()` alone accepts Arabic-Indic digits, and `int()` converts
        # them happily — a post id must stay plain ASCII.
        "publish:١٢",
        "publish:" + "9" * (MAX_CALLBACK_POST_ID_DIGITS + 1),
    ],
)
def test_malformed_callback_data_is_refused(callback_data: str | None) -> None:
    assert parse_publish_callback(callback_data) is None
