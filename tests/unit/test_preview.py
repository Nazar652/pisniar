"""`telegram.preview`: the only safeguard before publication. A published post
cannot be revoked, so the admin must see the exact file before it goes out.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import SendVideo

from pisniar.db.models import Post
from pisniar.telegram import preview as preview_module
from pisniar.telegram.keyboards import CallbackAction, parse_callback
from pisniar.telegram.preview import (
    deliver_preview,
    remove_draft_keyboard,
    remove_preview_keyboard,
)
from pisniar.telegram.video import RenderedVideo


def make_post(
    *,
    caption: str | None = "caption text",
    preview_message_id: int | None = None,
    draft: tuple[int, int] | None = (555, 41),
) -> Post:
    post = Post(id=1, track_id=1, created_by_user_id=1, caption=caption)
    post.preview_message_id = preview_message_id
    if draft is not None:
        post.draft_chat_id, post.draft_message_id = draft
    return post


def make_video(tmp_path: Path) -> RenderedVideo:
    video_path = tmp_path / "render.mp4"
    video_path.write_bytes(b"video bytes")
    return RenderedVideo(path=video_path, width=720, height=1280, duration_ms=45_000)


def make_bot(message_id: int = 42) -> AsyncMock:
    bot = AsyncMock()
    bot.send_video.return_value.message_id = message_id
    return bot


async def test_the_preview_is_a_new_message_with_its_own_reference(tmp_path: Path) -> None:
    """`editMessageMedia` cannot turn a text draft into a media message, so
    the preview has a chat/message pair of its own (migration 0005)."""
    bot = make_bot()
    post = make_post()

    await deliver_preview(bot, post, 999, make_video(tmp_path))

    bot.send_video.assert_awaited_once()
    assert post.preview_chat_id == 999
    assert post.preview_message_id == 42
    assert post.draft_message_id == 41


async def test_the_preview_carries_the_publish_button(tmp_path: Path) -> None:
    """without it the whole chain dead-ends: the draft's keyboard is removed
    on the render tap, so the preview is the only way left to publish."""
    bot = make_bot()
    post = make_post()

    await deliver_preview(bot, post, 999, make_video(tmp_path))

    reply_markup = bot.send_video.await_args.kwargs["reply_markup"]
    parsed = parse_callback(reply_markup.inline_keyboard[0][0].callback_data)
    assert parsed is not None
    assert (parsed.action, parsed.post_id) == (CallbackAction.PUBLISH, post.id)


async def test_a_post_that_already_has_a_preview_gets_a_new_one_and_points_at_it(
    tmp_path: Path,
) -> None:
    """the guard against a second send is the `rendering` status under the post lock, held
    by the caller; here the new message replaces the reference."""
    bot = make_bot(message_id=43)
    post = make_post(preview_message_id=42)

    await deliver_preview(bot, post, 999, make_video(tmp_path))

    bot.send_video.assert_awaited_once()
    assert post.preview_message_id == 43


async def test_a_superseded_preview_loses_its_keyboard() -> None:
    bot = AsyncMock()

    await remove_preview_keyboard(bot, 999, 42)

    assert bot.edit_message_reply_markup.await_args.kwargs == {
        "chat_id": 999,
        "message_id": 42,
        "reply_markup": None,
    }


async def test_a_failed_superseded_keyboard_removal_never_raises_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(preview_module, "logger", fake_logger)
    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = TelegramAPIError(
        method=SendVideo(chat_id=999, video="x"), message="boom"
    )

    await remove_preview_keyboard(bot, 999, 42)

    fake_logger.warning.assert_called_once()


async def test_a_keyboard_that_is_already_gone_is_a_success_not_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(preview_module, "logger", fake_logger)
    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = TelegramBadRequest(
        method=SendVideo(chat_id=999, video="x"),
        message="Bad Request: message is not modified: specified new message content",
    )

    await remove_preview_keyboard(bot, 999, 42)

    fake_logger.warning.assert_not_called()
    fake_logger.info.assert_called_once()


async def test_previewing_without_a_caption_raises_instead_of_sending(tmp_path: Path) -> None:
    bot = make_bot()

    with pytest.raises(ValueError, match="no caption"):
        await deliver_preview(bot, make_post(caption=None), 999, make_video(tmp_path))

    bot.send_video.assert_not_awaited()


async def test_a_telegram_failure_propagates_and_leaves_the_reference_null(
    tmp_path: Path,
) -> None:
    """the opposite of `send_alert`: the preview is the product, so its failure
    must reach the task and turn the post `failed`."""
    bot = make_bot()
    bot.send_video.side_effect = TelegramAPIError(
        method=SendVideo(chat_id=999, video="x"), message="boom"
    )
    post = make_post()

    with pytest.raises(TelegramAPIError):
        await deliver_preview(bot, post, 999, make_video(tmp_path))

    assert post.preview_chat_id is None
    assert post.preview_message_id is None


async def test_the_old_draft_loses_its_keyboard() -> None:
    """its publish button would send the cover photo instead of the video."""
    bot = AsyncMock()

    await remove_draft_keyboard(bot, make_post())

    assert bot.edit_message_reply_markup.await_args.kwargs == {
        "chat_id": 555,
        "message_id": 41,
        "reply_markup": None,
    }


async def test_a_post_without_a_draft_has_no_keyboard_to_remove() -> None:
    bot = AsyncMock()

    await remove_draft_keyboard(bot, make_post(draft=None))

    bot.edit_message_reply_markup.assert_not_awaited()


async def test_a_failed_keyboard_removal_never_raises() -> None:
    """cosmetic on its own — the status transition is the real guard — so it
    must not undo a render that already succeeded."""
    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = TelegramAPIError(
        method=SendVideo(chat_id=555, video="x"), message="message not found"
    )

    await remove_draft_keyboard(bot, make_post())
