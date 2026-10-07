"""The two inline buttons and the grammar of the payload they send back.
The payload arrives from a Telegram client and selects both the row and the
*action*, so everything that is not exactly `<known prefix>:<ascii digits>`
must be refused — a mistyped prefix must never fall through to publishing."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from aiogram.types import InlineKeyboardMarkup

from pisniar.telegram.keyboards import (
    MAX_CALLBACK_POST_ID_DIGITS,
    PUBLISH_BUTTON_TEXT,
    RENDER_BUTTON_TEXT,
    CallbackAction,
    ParsedCallback,
    draft_keyboard,
    parse_callback,
    preview_keyboard,
)


def buttons(markup: InlineKeyboardMarkup) -> list[tuple[str, str | None]]:
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def test_the_draft_carries_publish_and_render_in_that_order() -> None:
    assert buttons(draft_keyboard(42)) == [
        (PUBLISH_BUTTON_TEXT, "publish:42"),
        (RENDER_BUTTON_TEXT, "render:42"),
    ]


def test_the_preview_carries_publication_alone() -> None:
    """there is nothing left to render under a preview, and a second render
    button there would re-download a track that is already on the volume."""
    assert buttons(preview_keyboard(42)) == [(PUBLISH_BUTTON_TEXT, "publish:42")]


@pytest.mark.parametrize("build_keyboard", [draft_keyboard, preview_keyboard])
def test_callback_data_fits_telegram_s_64_byte_cap(
    build_keyboard: Callable[[int], InlineKeyboardMarkup],
) -> None:
    for _, callback_data in buttons(build_keyboard(9_223_372_036_854_775_807)):
        assert callback_data is not None
        assert len(callback_data.encode("utf-8")) <= 64


def test_every_button_s_own_payload_round_trips() -> None:
    parsed = [parse_callback(callback_data) for _, callback_data in buttons(draft_keyboard(7))]

    assert parsed == [
        ParsedCallback(action=CallbackAction.PUBLISH, post_id=7),
        ParsedCallback(action=CallbackAction.RENDER, post_id=7),
    ]


def test_the_render_prefix_is_parsed_as_render_and_not_as_publish() -> None:
    """a render tap must never be routed into the publication path, which
    cannot be undone."""
    parsed = parse_callback("render:7")

    assert parsed is not None
    assert parsed.action is CallbackAction.RENDER


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
        "renderx:1",
        "render",
        "render:",
        "render:-1",
        "Render:1",
        ":1",
        # `isdigit()` alone accepts Arabic-Indic digits, and `int()` converts
        # them happily — a post id must stay plain ASCII.
        "publish:١٢",
        "render:١٢",
        "publish:" + "9" * (MAX_CALLBACK_POST_ID_DIGITS + 1),
        "render:" + "9" * (MAX_CALLBACK_POST_ID_DIGITS + 1),
    ],
)
def test_malformed_callback_data_is_refused(callback_data: str | None) -> None:
    assert parse_callback(callback_data) is None
