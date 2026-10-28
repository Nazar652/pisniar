"""The «Редагувати» `web_app` button: it appears only in a private chat and only when the
Mini App address is configured, on the draft, the preview and the publication confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardMarkup

from pisniar.db.models import Post
from pisniar.telegram.draft import deliver_draft
from pisniar.telegram.keyboards import (
    EDIT_BUTTON_TEXT,
    draft_keyboard,
    edit_button_rows,
    post_list_keyboard,
    preview_keyboard,
    published_keyboard,
    webapp_url,
)
from pisniar.telegram.preview import deliver_preview
from pisniar.telegram.video import RenderedVideo

URL = "https://pisniar.example.ts.net"
PRIVATE_CHAT = 555
GROUP_CHAT = -1001234567890


def web_app_urls(keyboard: InlineKeyboardMarkup | None) -> list[str]:
    if keyboard is None:
        return []
    return [button.web_app.url for row in keyboard.inline_keyboard for button in row if button.web_app is not None]


def test_the_editor_url_of_a_post_and_the_list_url() -> None:
    assert webapp_url(URL, 7) == f"{URL}/?post_id=7"
    assert webapp_url(URL) == f"{URL}/"


def test_a_private_chat_with_the_address_gets_the_edit_button() -> None:
    rows = edit_button_rows(PRIVATE_CHAT, URL, 7)

    assert len(rows) == 1
    button = rows[0][0]
    assert button.text == EDIT_BUTTON_TEXT
    assert button.web_app is not None
    assert button.web_app.url == f"{URL}/?post_id=7"
    assert button.callback_data is None


@pytest.mark.parametrize("chat_id", [GROUP_CHAT, -1, 0, None])
def test_no_chat_but_a_private_one_gets_the_edit_button(chat_id: int | None) -> None:
    assert edit_button_rows(chat_id, URL, 7) == []


def test_no_address_means_no_edit_button() -> None:
    assert edit_button_rows(PRIVATE_CHAT, None, 7) == []


def test_the_draft_keyboard_adds_the_edit_row_after_the_render_trigger() -> None:
    keyboard = draft_keyboard(7, PRIVATE_CHAT, URL)

    assert len(keyboard.inline_keyboard) == 2
    assert [button.callback_data for row in keyboard.inline_keyboard[:1] for button in row] == ["render:7"]
    assert web_app_urls(keyboard) == [f"{URL}/?post_id=7"]


def test_the_preview_keyboard_adds_the_edit_row_after_publication() -> None:
    keyboard = preview_keyboard(7, PRIVATE_CHAT, URL)

    assert len(keyboard.inline_keyboard) == 2
    assert keyboard.inline_keyboard[0][0].callback_data == "publish:7"
    assert web_app_urls(keyboard) == [f"{URL}/?post_id=7"]


@pytest.mark.parametrize("build", [draft_keyboard, preview_keyboard])
def test_without_a_private_chat_or_an_address_the_keyboards_are_the_plain_ones(
    build: Callable[..., InlineKeyboardMarkup],
) -> None:
    plain = build(7)

    assert build(7, GROUP_CHAT, URL) == plain
    assert build(7, PRIVATE_CHAT, None) == plain
    assert web_app_urls(plain) == []


def test_the_confirmation_keyboard_is_the_edit_button_alone() -> None:
    keyboard = published_keyboard(7, PRIVATE_CHAT, URL)

    assert keyboard is not None
    assert web_app_urls(keyboard) == [f"{URL}/?post_id=7"]
    assert len(keyboard.inline_keyboard) == 1


def test_the_confirmation_has_no_keyboard_where_the_button_cannot_open() -> None:
    assert published_keyboard(7, GROUP_CHAT, URL) is None
    assert published_keyboard(7, PRIVATE_CHAT, None) is None


def test_the_post_list_button_opens_the_list_in_a_private_chat_only() -> None:
    assert web_app_urls(post_list_keyboard(PRIVATE_CHAT, URL)) == [f"{URL}/"]
    assert post_list_keyboard(GROUP_CHAT, URL) is None
    assert post_list_keyboard(PRIVATE_CHAT, None) is None


async def test_a_draft_delivered_to_a_private_chat_carries_the_edit_button() -> None:
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 42
    post = Post(id=7, track_id=1, created_by_user_id=1, caption="caption")

    await deliver_draft(bot, post, PRIVATE_CHAT, URL)

    assert web_app_urls(bot.send_message.await_args.kwargs["reply_markup"]) == [f"{URL}/?post_id=7"]


async def test_a_draft_delivered_to_a_group_never_carries_it_and_is_still_delivered() -> None:
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 42
    post = Post(id=7, track_id=1, created_by_user_id=1, caption="caption")

    await deliver_draft(bot, post, GROUP_CHAT, URL)

    assert web_app_urls(bot.send_message.await_args.kwargs["reply_markup"]) == []
    assert post.draft_message_id == 42


async def test_a_preview_delivered_to_a_private_chat_carries_the_edit_button(
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "render.mp4"
    video_path.write_bytes(b"video bytes")
    video = RenderedVideo(path=video_path, width=720, height=896, duration_ms=45_000)
    bot = AsyncMock()
    bot.send_video.return_value.message_id = 43
    post = Post(id=7, track_id=1, created_by_user_id=1, caption="caption")

    await deliver_preview(bot, post, PRIVATE_CHAT, video, URL)

    assert web_app_urls(bot.send_video.await_args.kwargs["reply_markup"]) == [f"{URL}/?post_id=7"]


async def test_a_preview_delivered_to_a_group_never_carries_it(tmp_path: Path) -> None:
    video_path = tmp_path / "render.mp4"
    video_path.write_bytes(b"video bytes")
    video = RenderedVideo(path=video_path, width=720, height=896, duration_ms=45_000)
    bot = AsyncMock()
    bot.send_video.return_value.message_id = 43
    post = Post(id=7, track_id=1, created_by_user_id=1, caption="caption")

    await deliver_preview(bot, post, GROUP_CHAT, video, URL)

    assert web_app_urls(bot.send_video.await_args.kwargs["reply_markup"]) == []
    assert post.preview_message_id == 43
