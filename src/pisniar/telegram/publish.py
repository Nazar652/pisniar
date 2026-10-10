"""Channel publication, the mirror of `draft.py`: idempotent send, per-call
`parse_mode="HTML"`, the caption verbatim, failures propagated. `send_photo` with the cover is the normal path;
a `rendered` post goes out as `send_video`.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberOwner,
    LinkPreviewOptions,
)

from pisniar.channel import CHANNEL_USERNAME
from pisniar.db.models import Post
from pisniar.logging import get_logger
from pisniar.telegram.video import RenderedVideo, send_rendered_video

logger = get_logger(__name__)

CANNOT_REACH_CHANNEL_ERROR = (
    "Канал недоступний: бот не знайшов чат за CHANNEL_ID або не має до нього доступу. "
    "Перевірте CHANNEL_ID і те, що бота додано в канал."
)
NOT_AN_ADMINISTRATOR_ERROR = (
    "Бот не є адміністратором каналу — додайте його адміністратором і увімкніть право публікації повідомлень."
)
CANNOT_POST_ERROR = (
    "Бот є адміністратором каналу, але без права публікації — увімкніть «Публікація повідомлень» у його правах."
)


class PublishPreflightError(RuntimeError):
    """carries a Ukrainian, admin-readable reason. The Telegram error it was raised from stays in the logs and never
    reaches the admin's chat."""


async def preflight_channel(bot: Bot, channel_id: int) -> ChatFullInfo:
    """turns a raw `400`/`403` into an actionable sentence before anything is sent; the returned chat is the only honest
    source for the post's link afterwards."""
    try:
        chat = await bot.get_chat(channel_id)
        member = await bot.get_chat_member(channel_id, bot.id)
    except TelegramAPIError as error:
        logger.warning("publish_preflight_failed", channel_id=channel_id, exc_info=True)
        raise PublishPreflightError(CANNOT_REACH_CHANNEL_ERROR) from error

    if isinstance(member, ChatMemberAdministrator):
        if not member.can_post_messages:
            raise PublishPreflightError(CANNOT_POST_ERROR)
    elif not isinstance(member, ChatMemberOwner):
        raise PublishPreflightError(NOT_AN_ADMINISTRATOR_ERROR)

    warn_if_channel_identity_diverges(chat)
    return chat


def warn_if_channel_identity_diverges(chat: ChatFullInfo) -> bool:
    """a post can link to one channel and land in another. A warning, not a refusal: a test channel is exactly this
    divergence, on purpose."""
    if chat.username == CHANNEL_USERNAME:
        return False

    logger.warning(
        "publish_channel_is_not_the_caption_channel",
        chat_id=chat.id,
        chat_username=chat.username,
        caption_username=CHANNEL_USERNAME,
    )
    return True


async def publish_to_channel(
    bot: Bot,
    post: Post,
    chat_id: int,
    cover_url: str | None,
    video: RenderedVideo | None = None,
) -> None:
    """idempotent on `channel_message_id`. The caller picks the video branch from the status plus a `render` row, never
    from a leftover file."""
    if post.channel_message_id is not None:
        return
    if post.caption is None:
        raise ValueError(f"post {post.id} has no caption to publish")

    if video is not None:
        # the video caption limit is the same 1024 the photo caption already fits, so nothing is rebuilt: the admin
        # approved these exact bytes.
        sent_message = await send_rendered_video(bot, chat_id, post.caption, video)
        message_kind = "video"
    elif cover_url:
        # telegram fetches the cover by URL itself and can fail on it; that is a delivery failure to report, not one to
        # work around here.
        sent_message = await bot.send_photo(
            chat_id=chat_id,
            photo=cover_url,
            caption=post.caption,
            parse_mode="HTML",
        )
        message_kind = "photo"
    else:
        # no cover, so the caption is a plain message again — and a message full of platform links needs the preview
        # card suppressed.
        sent_message = await bot.send_message(
            chat_id=chat_id,
            text=post.caption,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        message_kind = "text"

    post.channel_chat_id = chat_id
    post.channel_message_id = sent_message.message_id
    post.channel_message_kind = message_kind


def channel_post_url(chat: ChatFullInfo, message_id: int) -> str:
    """built from the chat the post actually went to, never from `CHANNEL_LINK_URL` — the two are allowed to differ."""
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    return f"https://t.me/c/{private_chat_path(chat.id)}/{message_id}"


def private_chat_path(chat_id: int) -> str:
    """`t.me/c/` links use the id without the `-100` supergroup prefix."""
    return str(chat_id).removeprefix("-100").lstrip("-")


def describe_publish_error(error: Exception) -> str:
    """ukrainian and distinct per class: 403, 429 and 400 ask the admin for three different next actions, and a raw
    traceback asks for none."""
    if isinstance(error, PublishPreflightError):
        return str(error)
    if isinstance(error, TelegramForbiddenError):
        return "Telegram відмовив у доступі до каналу (403): бот більше не має права там писати."
    if isinstance(error, TelegramRetryAfter):
        return f"Telegram просить зачекати {error.retry_after} с (429). Пост не опубліковано — повторіть пізніше."
    if isinstance(error, TelegramBadRequest):
        return f"Telegram відхилив пост (400): {error.message}"
    return str(error)
