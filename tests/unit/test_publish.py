"""`telegram.publish`: the idempotent channel send, the preflight that turns
a raw 400/403 into a Ukrainian sentence, the post link built from the chat
the post actually reached, and the per-class error wording. Every Telegram
call is mocked — V16 forbids a unit test reaching the network."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendPhoto
from aiogram.types import (
    AcceptedGiftTypes,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberMember,
    User,
)

from pisniar.channel import CHANNEL_USERNAME
from pisniar.db.models import Post
from pisniar.telegram import publish as publish_module
from pisniar.telegram.publish import (
    CANNOT_POST_ERROR,
    CANNOT_REACH_CHANNEL_ERROR,
    NOT_AN_ADMINISTRATOR_ERROR,
    PublishPreflightError,
    channel_post_url,
    describe_publish_error,
    preflight_channel,
    publish_to_channel,
    warn_if_channel_identity_diverges,
)
from pisniar.telegram.video import RenderedVideo

CHANNEL_ID = -1001234567890
CAPTION_WITH_MARKUP = '<a href="https://open.spotify.com/track/x">Spotify</a> &amp; Co «Тест»'


def make_post(
    *,
    caption: str | None = CAPTION_WITH_MARKUP,
    channel_message_id: int | None = None,
) -> Post:
    post = Post(id=1, track_id=1, created_by_user_id=1, caption=caption)
    post.channel_message_id = channel_message_id
    return post


def make_chat(
    *, username: str | None = CHANNEL_USERNAME, chat_id: int = CHANNEL_ID
) -> ChatFullInfo:
    return ChatFullInfo(
        id=chat_id,
        type="channel",
        username=username,
        accent_color_id=0,
        max_reaction_count=11,
        accepted_gift_types=AcceptedGiftTypes(
            unlimited_gifts=False,
            limited_gifts=False,
            unique_gifts=False,
            premium_subscription=False,
            gifts_from_channels=False,
        ),
    )


def make_administrator(*, can_post_messages: bool) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        user=User(id=5, is_bot=True, first_name="pisniar"),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_send_welcome_messages=False,
        can_post_messages=can_post_messages,
    )


def make_bot(
    *, photo_message_id: int = 77, text_message_id: int = 78, video_message_id: int = 79
) -> AsyncMock:
    bot = AsyncMock()
    bot.send_photo.return_value.message_id = photo_message_id
    bot.send_message.return_value.message_id = text_message_id
    bot.send_video.return_value.message_id = video_message_id
    return bot


def make_video(tmp_path: Path) -> RenderedVideo:
    video_path = tmp_path / "render.mp4"
    video_path.write_bytes(b"video bytes")
    return RenderedVideo(path=video_path, width=720, height=1280, duration_ms=45_000)


async def test_a_rendered_post_goes_out_as_a_video_not_a_photo(tmp_path: Path) -> None:
    """§6's third branch. The caller decides it from the status, and once it
    has, the cover URL must not pull the post back to `send_photo`."""
    bot = make_bot()
    post = make_post()

    await publish_to_channel(
        bot, post, CHANNEL_ID, "https://cdn.example/cover.jpg", make_video(tmp_path)
    )

    bot.send_video.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert post.channel_chat_id == CHANNEL_ID
    assert post.channel_message_id == 79


async def test_the_video_caption_is_the_same_string_the_preview_carried(tmp_path: Path) -> None:
    """V41/S4-A14: the video caption limit is the photo caption limit, so
    nothing is rebuilt between the preview and the channel."""
    bot = make_bot()

    await publish_to_channel(bot, make_post(), CHANNEL_ID, None, make_video(tmp_path))

    sent = bot.send_video.await_args.kwargs
    assert sent["caption"] == CAPTION_WITH_MARKUP
    assert sent["parse_mode"] == "HTML"


async def test_an_already_published_post_sends_no_video_either(tmp_path: Path) -> None:
    bot = make_bot()
    post = make_post(channel_message_id=500)

    await publish_to_channel(bot, post, CHANNEL_ID, None, make_video(tmp_path))

    bot.send_video.assert_not_awaited()
    assert post.channel_message_id == 500


async def test_a_post_with_a_cover_goes_out_as_a_photo_with_the_caption() -> None:
    bot = make_bot()
    post = make_post()

    await publish_to_channel(bot, post, CHANNEL_ID, "https://cdn.example/cover.jpg")

    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    sent = bot.send_photo.await_args.kwargs
    assert sent["chat_id"] == CHANNEL_ID
    assert sent["photo"] == "https://cdn.example/cover.jpg"
    assert post.channel_chat_id == CHANNEL_ID
    assert post.channel_message_id == 77


async def test_the_caption_reaches_telegram_byte_for_byte_as_stored() -> None:
    """`post.caption` is already rendered, escaped HTML; escaping it again
    would turn `&amp;` into `&amp;amp;` in a post subscribers see (S3-A7)."""
    bot = make_bot()
    post = make_post()

    await publish_to_channel(bot, post, CHANNEL_ID, "https://cdn.example/cover.jpg")

    sent = bot.send_photo.await_args.kwargs
    assert sent["caption"] == CAPTION_WITH_MARKUP
    assert sent["parse_mode"] == "HTML"


async def test_without_a_cover_it_falls_back_to_a_message_with_no_preview() -> None:
    bot = make_bot()
    post = make_post()

    await publish_to_channel(bot, post, CHANNEL_ID, None)

    bot.send_photo.assert_not_awaited()
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == CAPTION_WITH_MARKUP
    assert sent["parse_mode"] == "HTML"
    assert sent["link_preview_options"].is_disabled is True
    assert post.channel_message_id == 78


async def test_an_empty_cover_url_is_treated_as_no_cover() -> None:
    bot = make_bot()

    await publish_to_channel(bot, make_post(), CHANNEL_ID, "")

    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()


async def test_a_post_that_already_has_a_channel_message_is_never_sent_again() -> None:
    """V18: the single most expensive failure of this step is a second copy
    in the channel, and this guard is what a redelivered task hits first."""
    bot = make_bot()
    post = make_post(channel_message_id=500)

    await publish_to_channel(bot, post, CHANNEL_ID, "https://cdn.example/cover.jpg")

    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert post.channel_message_id == 500


async def test_publishing_without_a_caption_raises_instead_of_sending() -> None:
    bot = make_bot()

    with pytest.raises(ValueError, match="no caption"):
        await publish_to_channel(bot, make_post(caption=None), CHANNEL_ID, None)

    bot.send_photo.assert_not_awaited()


async def test_a_telegram_failure_propagates_and_leaves_the_reference_null() -> None:
    """The opposite of `send_alert`: the post is the product, so a delivery
    failure must reach the task and turn the post `failed`."""
    bot = make_bot()
    bot.send_photo.side_effect = TelegramBadRequest(
        method=SendPhoto(chat_id=CHANNEL_ID, photo="x"), message="failed to get HTTP URL content"
    )
    post = make_post()

    with pytest.raises(TelegramBadRequest):
        await publish_to_channel(bot, post, CHANNEL_ID, "https://cdn.example/cover.jpg")

    assert post.channel_chat_id is None
    assert post.channel_message_id is None


async def test_preflight_accepts_an_administrator_allowed_to_post() -> None:
    bot = make_bot()
    bot.get_chat.return_value = make_chat()
    bot.get_chat_member.return_value = make_administrator(can_post_messages=True)

    chat = await preflight_channel(bot, CHANNEL_ID)

    assert chat.id == CHANNEL_ID
    bot.get_chat.assert_awaited_once_with(CHANNEL_ID)
    bot.get_chat_member.assert_awaited_once_with(CHANNEL_ID, bot.id)


async def test_preflight_refuses_an_administrator_without_the_posting_right() -> None:
    bot = make_bot()
    bot.get_chat.return_value = make_chat()
    bot.get_chat_member.return_value = make_administrator(can_post_messages=False)

    with pytest.raises(PublishPreflightError) as excinfo:
        await preflight_channel(bot, CHANNEL_ID)

    assert str(excinfo.value) == CANNOT_POST_ERROR


async def test_preflight_refuses_a_bot_that_is_only_a_member() -> None:
    bot = make_bot()
    bot.get_chat.return_value = make_chat()
    bot.get_chat_member.return_value = ChatMemberMember(
        user=User(id=5, is_bot=True, first_name="pisniar")
    )

    with pytest.raises(PublishPreflightError) as excinfo:
        await preflight_channel(bot, CHANNEL_ID)

    assert str(excinfo.value) == NOT_AN_ADMINISTRATOR_ERROR


async def test_preflight_hides_the_raw_telegram_error_behind_a_ukrainian_one() -> None:
    """S3-A6: the admin gets a sentence they can act on, and the 400 with
    its traceback stays in the logs."""
    bot = make_bot()
    bot.get_chat.side_effect = TelegramBadRequest(
        method=SendPhoto(chat_id=CHANNEL_ID, photo="x"), message="chat not found"
    )

    with pytest.raises(PublishPreflightError) as excinfo:
        await preflight_channel(bot, CHANNEL_ID)

    assert str(excinfo.value) == CANNOT_REACH_CHANNEL_ERROR
    assert "chat not found" not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, TelegramBadRequest)


def test_a_channel_that_is_not_the_captions_channel_warns() -> None:
    """S3-R7: every caption links to `CHANNEL_LINK_URL` while the target
    comes from config, and nothing derives one from the other."""
    with structlog.testing.capture_logs() as captured_logs:
        diverged = warn_if_channel_identity_diverges(make_chat(username="test_channel"))

    assert diverged is True
    assert [entry["event"] for entry in captured_logs if entry["log_level"] == "warning"] == [
        "publish_channel_is_not_the_caption_channel"
    ]


def test_the_captions_own_channel_does_not_warn() -> None:
    with structlog.testing.capture_logs() as captured_logs:
        diverged = warn_if_channel_identity_diverges(make_chat())

    assert diverged is False
    assert captured_logs == []


def test_the_post_link_uses_the_public_username_when_there_is_one() -> None:
    assert channel_post_url(make_chat(username="pisniarnia"), 12) == "https://t.me/pisniarnia/12"


def test_the_post_link_falls_back_to_the_private_form() -> None:
    """Built from the chat the post actually went to, so a test channel with
    no username still produces a link the admin can open."""
    assert channel_post_url(make_chat(username=None), 12) == "https://t.me/c/1234567890/12"


def test_forbidden_retry_after_and_bad_request_read_differently() -> None:
    forbidden = describe_publish_error(
        TelegramForbiddenError(method=SendPhoto(chat_id=1, photo="x"), message="bot was kicked")
    )
    retry_after = describe_publish_error(
        TelegramRetryAfter(
            method=SendPhoto(chat_id=1, photo="x"), message="Too Many Requests", retry_after=30
        )
    )
    bad_request = describe_publish_error(
        TelegramBadRequest(method=SendPhoto(chat_id=1, photo="x"), message="wrong file identifier")
    )

    assert "403" in forbidden
    assert "30" in retry_after and "429" in retry_after
    assert "400" in bad_request and "wrong file identifier" in bad_request
    assert len({forbidden, retry_after, bad_request}) == 3


def test_a_preflight_error_is_passed_through_unchanged() -> None:
    assert describe_publish_error(PublishPreflightError(CANNOT_POST_ERROR)) == CANNOT_POST_ERROR


def test_the_publish_target_is_never_a_literal_in_the_publish_code() -> None:
    """V25: the channel is a parameter, not a constant. A literal chat id or
    a second `pisniarnia` here is exactly how the caption's channel and the
    publish target drift apart (S3-R7)."""
    source = inspect.getsource(publish_module)

    assert "pisniarnia" not in source
    for function in (preflight_channel, publish_to_channel):
        function_source = inspect.getsource(function)
        assert re.search(r"-?\d{6,}", function_source) is None
        assert "chat_id=chat_id" in function_source or "channel_id" in function_source
