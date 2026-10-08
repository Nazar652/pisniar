"""Command handlers, reachable only by admins (`AdminAllowlistMiddleware`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from celery import Celery
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.config import get_settings
from pisniar.db.models import Post, Track
from pisniar.db.session import session_scope
from pisniar.db.states import IllegalStatusTransitionError, PostStatus, set_post_status
from pisniar.logging import bind_post_id, get_logger
from pisniar.resolve.types import USER_INPUT_SOURCE
from pisniar.resolve.urls import (
    MAX_NORMALIZED_URL_BYTES,
    ClassifiedUrl,
    UrlKind,
    classify_url,
    is_safe_url,
    is_within_length_limit,
)
from pisniar.telegram.keyboards import (
    CallbackAction,
    edit_button_rows,
    parse_callback,
    post_list_keyboard,
)
from pisniar.worker.celery_app import (
    DOWNLOAD_POST_AUDIO_TASK_NAME,
    PUBLISH_POST_TASK_NAME,
    RENDER_POST_VIDEO_TASK_NAME,
    RESOLVE_POST_TASK_NAME,
    TASK_TIME_LIMIT_SECONDS,
)

logger = get_logger(__name__)

router = Router(name="commands")

# the media time limits live in `Settings`, never in `worker.celery_app`:
# raising the resolve limits would silently widen the staleness window below.
settings = get_settings()

DISPATCH_FAILED_ERROR = "не вдалося поставити задачу в чергу"
PUBLISH_DISPATCH_FAILED_ERROR = "не вдалося поставити задачу публікації в чергу"
DOWNLOAD_DISPATCH_FAILED_ERROR = "не вдалося поставити задачу завантаження аудіо в чергу"
RENDER_DISPATCH_FAILED_ERROR = "не вдалося поставити задачу рендера відео в чергу"
UNKNOWN_CALLBACK_ANSWER = "Невідома дія."
ARCHIVED_POST_ANSWER = "Пост {post_id} архівовано — дій із ним немає."
EDIT_FORMAT_ANSWER = "Формат команди: /edit [post_id]"
EDITOR_NOT_CONFIGURED_ANSWER = "Редагування вимкнене: адресу Mini App не задано."
EDITOR_PRIVATE_ONLY_ANSWER = "Редактор відкривається лише в приватному чаті з ботом."
EDIT_LIST_ANSWER = "Відкрити список постів."
EDIT_POST_ANSWER = "Редагувати пост {post_id}."
POST_NOT_FOUND_ANSWER = "Пост {post_id} не знайдено."
POST_BUSY_ANSWER = "Пост {post_id} зараз обробляється. Спробуйте за кілька секунд."
DISPATCH_FAILED_RETRY_ANSWER = "Не вдалося поставити задачу в чергу. Спробуйте /retry {post_id}."

# the text draft's publish button is still live during a render, and a
# refusal naming `downloading` would read to the admin like a breakage.
MEDIA_IN_PROGRESS_STATUSES = frozenset(
    {PostStatus.DOWNLOADING, PostStatus.DOWNLOADED, PostStatus.RENDERING}
)
MEDIA_IN_PROGRESS_ANSWER = (
    "Для поста {post_id} саме зараз готується відео. Дочекайтесь прев'ю — "
    "кнопка публікації буде під ним."
)
VIDEO_READY_ANSWER = "Відео для поста {post_id} уже готове — публікуйте кнопкою під прев'ю з відео."
PREVIEW_ALREADY_SHOWN_ANSWER = (
    "Відео для поста {post_id} уже показано в прев'ю, і повторно воно не рендериться: "
    "у канал іде саме те відео. Якщо публікація не вдалася — /retry {post_id}."
)

# a clip is a `youtube` link, never a `youtubeMusic` one — the latter
# is the canonical "— Topic" upload and belongs to the platform half.
CLIP_PLATFORM = "youtube"

PAIR_SHAPE_HINT = "Очікується пара «посилання на платформу + кліп YouTube»"
TWO_PLATFORM_LINKS_ANSWER = (
    f"Обидва посилання ведуть на платформи, кліпа серед них немає. {PAIR_SHAPE_HINT} — "
    f"надішліть одне посилання окремо або додайте до нього кліп."
)
TWO_CLIPS_ANSWER = (
    f"Обидва посилання — кліпи YouTube. {PAIR_SHAPE_HINT} — надішліть один кліп окремо "
    f"або додайте до нього посилання на платформу."
)
TOO_MANY_LINKS_ANSWER = (
    "У повідомленні посилань: {count}. Надішліть одне посилання на трек або пару "
    "«посилання на платформу + кліп YouTube»."
)

NOT_TRACK_REASON_UKRAINIAN = {
    "album": "альбом",
    "artist": "артиста",
    "playlist": "плейлист",
    "show": "подкаст",
    "episode": "епізод",
}


class RetryBranch(StrEnum):
    RESOLVE = "resolve"
    DOWNLOAD = "download"
    RENDER = "render"
    REPUBLISH = "republish"


RETRY_TARGET_STATUS = {
    RetryBranch.RESOLVE: PostStatus.RESOLVING,
    RetryBranch.DOWNLOAD: PostStatus.DOWNLOADING,
    RetryBranch.RENDER: PostStatus.RENDERING,
    RetryBranch.REPUBLISH: PostStatus.PUBLISHING,
}

RETRY_DISPATCH_FAILED_ERROR = {
    RetryBranch.RESOLVE: DISPATCH_FAILED_ERROR,
    RetryBranch.DOWNLOAD: DOWNLOAD_DISPATCH_FAILED_ERROR,
    RetryBranch.RENDER: RENDER_DISPATCH_FAILED_ERROR,
    RetryBranch.REPUBLISH: PUBLISH_DISPATCH_FAILED_ERROR,
}

RETRY_RECEIPT = {
    RetryBranch.RESOLVE: "Повторюю обробку поста {post_id}.",
    RetryBranch.DOWNLOAD: "Повторюю завантаження аудіо для поста {post_id}.",
    RetryBranch.RENDER: "Повторюю рендер відео для поста {post_id}.",
    RetryBranch.REPUBLISH: "Повторюю публікацію поста {post_id}.",
}

# the stage the post is *stuck in*, and what repeating it means. Every one of
# these has a self-edge in `db.states`, which is what makes the reclaim legal.
STALE_STAGE_BRANCH = {
    PostStatus.RESOLVING: RetryBranch.RESOLVE,
    PostStatus.DOWNLOADING: RetryBranch.DOWNLOAD,
    # nothing runs in DOWNLOADED: the render dispatch happens right after the
    # download commits, so a post left here means the worker died in between.
    PostStatus.DOWNLOADED: RetryBranch.RENDER,
    PostStatus.RENDERING: RetryBranch.RENDER,
}

STALE_STAGE_ANSWER = {
    PostStatus.RESOLVING: "Пост {post_id} уже обробляється. Зачекайте на чернетку або на помилку.",
    PostStatus.DOWNLOADING: "Для поста {post_id} уже качається аудіо. Зачекайте на прев'ю.",
    PostStatus.DOWNLOADED: "Для поста {post_id} рендер уже в черзі. Зачекайте на прев'ю.",
    PostStatus.RENDERING: "Для поста {post_id} уже рендериться відео. Зачекайте на прев'ю.",
}

STALE_STAGE_REFUSAL_REASON = {
    PostStatus.RESOLVING: "already_resolving",
    PostStatus.DOWNLOADING: "already_downloading",
    PostStatus.DOWNLOADED: "render_already_queued",
    PostStatus.RENDERING: "already_rendering",
}


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    await message.answer("Бот Піснярні на зв'язку. Надішліть посилання на трек.")


@router.message(Command("retry"))
async def handle_retry(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    if message.text is None or message.from_user is None:
        return

    user_id = message.from_user.id

    arguments = message.text.split(maxsplit=1)
    if len(arguments) < 2 or not arguments[1].strip().isdigit():
        await message.answer("Формат команди: /retry <post_id>")
        logger.warning("retry_refused", reason="bad_arguments", user_id=user_id)
        return
    post_id = int(arguments[1].strip())

    # bound before the first refusal can fire, not after the transaction:
    # a `/retry` that never dispatches is exactly the one worth tracing.
    bind_post_id(post_id)
    logger.info("retry_requested", user_id=user_id, post_id=post_id)

    branch = RetryBranch.RESOLVE
    reclaimed_stale_stage: str | None = None

    async with session_scope(session_factory) as session:
        try:
            # locked, not just read: without it two near-simultaneous
            # `/retry` both pass the status check and dispatch.
            post = await session.get(Post, post_id, with_for_update={"nowait": True})
        except DBAPIError:
            await session.rollback()
            await message.answer(
                f"Пост {post_id} зараз обробляється воркером. Зачекайте на чернетку або помилку."
            )
            logger.warning("retry_refused", reason="post_locked", user_id=user_id, post_id=post_id)
            return

        if post is None:
            await message.answer(POST_NOT_FOUND_ANSWER.format(post_id=post_id))
            logger.warning(
                "retry_refused", reason="post_not_found", user_id=user_id, post_id=post_id
            )
            return

        if post.status is PostStatus.ARCHIVED:
            await message.answer(ARCHIVED_POST_ANSWER.format(post_id=post_id))
            logger.warning("retry_refused", reason="archived", user_id=user_id, post_id=post_id)
            return

        if post.status is PostStatus.FAILED:
            branch = retry_branch_for_failed_post(post)

        elif post.status in STALE_STAGE_BRANCH:
            stuck_status = post.status
            if not stage_has_gone_stale(post, stage_time_limit_seconds(stuck_status)):
                # the self-edges exist for Celery redelivery, not as a button:
                # a second task would race the first into two drafts.
                await message.answer(STALE_STAGE_ANSWER[stuck_status].format(post_id=post_id))
                logger.warning(
                    "retry_refused",
                    reason=STALE_STAGE_REFUSAL_REASON[stuck_status],
                    user_id=user_id,
                    post_id=post_id,
                )
                return

            # a self-edge changes no column, so the ORM `onupdate` never fires
            # and every later /retry would pass this guard too.
            post.updated_at = datetime.now(UTC)
            reclaimed_stale_stage = stuck_status.value
            branch = STALE_STAGE_BRANCH[stuck_status]

        try:
            set_post_status(post, RETRY_TARGET_STATUS[branch])
        except IllegalStatusTransitionError:
            await message.answer(
                f"Пост {post_id} зараз у стані «{post.status.value}» — /retry недоступний."
            )
            logger.warning(
                "retry_refused",
                reason="illegal_status",
                status=post.status.value,
                user_id=user_id,
                post_id=post_id,
            )
            return

    logger.info(
        "retry_branch_selected",
        branch=branch.value,
        user_id=user_id,
        post_id=post_id,
        reclaimed_stale_stage=reclaimed_stale_stage,
    )

    if not await dispatch_retry_task(celery_client, branch, post_id, user_id):
        await mark_dispatch_failure(
            session_factory,
            post_id,
            expected_status=RETRY_TARGET_STATUS[branch],
            error=RETRY_DISPATCH_FAILED_ERROR[branch],
        )
        await message.answer("Не вдалося поставити задачу в чергу. Спробуйте ще раз.")
        logger.warning(
            "retry_refused",
            reason="dispatch_failed",
            branch=branch.value,
            user_id=user_id,
            post_id=post_id,
        )
        return

    await message.answer(RETRY_RECEIPT[branch].format(post_id=post_id))
    logger.info("retry_dispatched", branch=branch.value, user_id=user_id, post_id=post_id)


@router.message(Command("edit"))
async def handle_edit(message: Message, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """without an id the button opens the list of posts, with one it opens that post's editor.
    The editor needs a private chat, where the `web_app` button is allowed."""
    if message.text is None or message.from_user is None:
        return

    user_id = message.from_user.id
    arguments = message.text.split(maxsplit=1)
    raw_post_id = arguments[1].strip() if len(arguments) > 1 else None
    if raw_post_id is not None and not raw_post_id.isdigit():
        await message.answer(EDIT_FORMAT_ANSWER)
        logger.warning("edit_refused", reason="bad_arguments", user_id=user_id)
        return

    webapp_public_url = settings.webapp_public_url
    if webapp_public_url is None:
        await message.answer(EDITOR_NOT_CONFIGURED_ANSWER)
        logger.warning("edit_refused", reason="webapp_not_configured", user_id=user_id)
        return

    chat_id = message.chat.id
    if chat_id <= 0:
        await message.answer(EDITOR_PRIVATE_ONLY_ANSWER)
        logger.warning("edit_refused", reason="not_a_private_chat", user_id=user_id)
        return

    if raw_post_id is None:
        await message.answer(
            EDIT_LIST_ANSWER, reply_markup=post_list_keyboard(chat_id, webapp_public_url)
        )
        logger.info("edit_list_offered", user_id=user_id)
        return

    post_id = int(raw_post_id)
    bind_post_id(post_id)

    async with session_scope(session_factory) as session:
        post = await session.get(Post, post_id)
        status = None if post is None else post.status

    if status is None:
        await message.answer(POST_NOT_FOUND_ANSWER.format(post_id=post_id))
        logger.warning("edit_refused", reason="post_not_found", user_id=user_id, post_id=post_id)
        return

    if status is PostStatus.ARCHIVED:
        await message.answer(ARCHIVED_POST_ANSWER.format(post_id=post_id))
        logger.warning("edit_refused", reason="archived", user_id=user_id, post_id=post_id)
        return

    await message.answer(
        EDIT_POST_ANSWER.format(post_id=post_id),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=edit_button_rows(chat_id, webapp_public_url, post_id)
        ),
    )
    logger.info("edit_offered", user_id=user_id, post_id=post_id)


def retry_branch_for_failed_post(post: Post) -> RetryBranch:
    """read from `failed_from_status`, never guessed from `draft_message_id`: a
    post that failed while downloading has a draft too."""
    stage = post.failed_from_status
    if stage is PostStatus.RESOLVING:
        return RetryBranch.RESOLVE
    if stage is PostStatus.DOWNLOADING:
        return RetryBranch.DOWNLOAD
    if stage in (PostStatus.DOWNLOADED, PostStatus.EDITING, PostStatus.RENDERING):
        return RetryBranch.RENDER
    if stage is PostStatus.PUBLISHING:
        return RetryBranch.REPUBLISH

    # a row with no recorded stage: a draft means the resolve succeeded and only
    # publication could have failed
    return RetryBranch.REPUBLISH if post.draft_message_id is not None else RetryBranch.RESOLVE


async def dispatch_retry_task(
    celery_client: Celery, branch: RetryBranch, post_id: int, user_id: int
) -> bool:
    if branch is RetryBranch.REPUBLISH:
        return await dispatch_publish_task(celery_client, post_id, user_id)
    if branch is RetryBranch.DOWNLOAD:
        return await dispatch_media_task(celery_client, DOWNLOAD_POST_AUDIO_TASK_NAME, post_id)
    if branch is RetryBranch.RENDER:
        return await dispatch_media_task(celery_client, RENDER_POST_VIDEO_TASK_NAME, post_id)
    return await dispatch_resolve_task(celery_client, post_id)


@router.callback_query()
async def handle_callback(
    callback: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """admin-only like every command: the allowlist is an *outer* middleware on
    `dispatcher.update`, and `event_from_user` is set for a `callback_query` too."""
    parsed = parse_callback(callback.data)
    if parsed is None or callback.from_user is None:
        logger.warning(
            "callback_rejected",
            reason="unparsable_callback_data",
            user_id=callback.from_user.id if callback.from_user is not None else None,
        )
        await callback.answer(UNKNOWN_CALLBACK_ANSWER)
        return

    bind_post_id(parsed.post_id)

    if parsed.action is CallbackAction.RENDER:
        await handle_render_tap(
            callback, parsed.post_id, callback.from_user.id, bot, session_factory, celery_client
        )
        return

    await handle_publish_tap(
        callback, parsed.post_id, callback.from_user.id, bot, session_factory, celery_client
    )


@dataclass(frozen=True)
class TapSpec:
    """what differs between the two buttons under a draft: log events, the claim
    that moves the status, the status a failed dispatch must undo, the answers."""

    tapped_event: str
    refused_event: str
    keyboard_failure_event: str
    claim: Callable[[Post | None, int, int], tuple[str | None, tuple[int, int] | None]]
    expected_status: PostStatus
    dispatch_failed_error: str
    dispatched_answer: str
    dispatched_event: str


async def handle_tap(
    callback: CallbackQuery,
    post_id: int,
    user_id: int,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    spec: TapSpec,
    start_task: Callable[[], Awaitable[bool]],
) -> None:
    """database first (lock, guard, status, commit), keyboard second, dispatch last."""
    logger.info(spec.tapped_event, user_id=user_id, post_id=post_id)

    refusal: str | None = None
    draft_reference: tuple[int, int] | None = None

    async with session_scope(session_factory) as session:
        try:
            # locked, not just read: this is the double-tap guard that the
            # partial unique index cannot be (two sends, two message ids).
            post = await session.get(Post, post_id, with_for_update={"nowait": True})
        except DBAPIError:
            await session.rollback()
            refusal = POST_BUSY_ANSWER.format(post_id=post_id)
            logger.warning(
                spec.refused_event, reason="post_locked", user_id=user_id, post_id=post_id
            )
        else:
            refusal, draft_reference = spec.claim(post, post_id, user_id)

    if refusal is not None:
        await callback.answer(refusal, show_alert=True)
        return

    # after the commit, before the dispatch: removing it any earlier would
    # strand a post the database then declined to move.
    await remove_keyboard(bot, draft_reference, spec.keyboard_failure_event)

    if not await start_task():
        await mark_dispatch_failure(
            session_factory,
            post_id,
            expected_status=spec.expected_status,
            error=spec.dispatch_failed_error,
        )
        await callback.answer(DISPATCH_FAILED_RETRY_ANSWER.format(post_id=post_id), show_alert=True)
        logger.warning(
            spec.refused_event, reason="dispatch_failed", user_id=user_id, post_id=post_id
        )
        return

    await callback.answer(spec.dispatched_answer)
    logger.info(spec.dispatched_event, user_id=user_id, post_id=post_id)


async def handle_publish_tap(
    callback: CallbackQuery,
    post_id: int,
    user_id: int,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    # `published_by_user_id` is written on success only, so the tap log is the
    # only trace of who tapped a refused or failed publish.
    spec = TapSpec(
        tapped_event="publish_button_tapped",
        refused_event="publish_refused",
        keyboard_failure_event="publish_keyboard_removal_failed",
        claim=claim_post_for_publishing,
        expected_status=PostStatus.PUBLISHING,
        dispatch_failed_error=PUBLISH_DISPATCH_FAILED_ERROR,
        dispatched_answer="Публікую…",
        dispatched_event="publish_task_dispatched",
    )
    await handle_tap(
        callback,
        post_id,
        user_id,
        bot,
        session_factory,
        spec,
        lambda: dispatch_publish_task(celery_client, post_id, user_id),
    )


async def handle_render_tap(
    callback: CallbackQuery,
    post_id: int,
    user_id: int,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """the media chain starts here and nowhere else. Minutes of CPU and
    hundreds of megabytes are spent only on a draft the admin approved."""
    spec = TapSpec(
        tapped_event="render_button_tapped",
        refused_event="render_refused",
        keyboard_failure_event="render_keyboard_removal_failed",
        claim=claim_post_for_media,
        expected_status=PostStatus.DOWNLOADING,
        dispatch_failed_error=DOWNLOAD_DISPATCH_FAILED_ERROR,
        dispatched_answer="Роблю відео… Прев'ю прийде окремим повідомленням.",
        dispatched_event="download_task_dispatched",
    )
    await handle_tap(
        callback,
        post_id,
        user_id,
        bot,
        session_factory,
        spec,
        lambda: dispatch_media_task(celery_client, DOWNLOAD_POST_AUDIO_TASK_NAME, post_id),
    )


def claim_post_for_publishing(
    post: Post | None, post_id: int, user_id: int
) -> tuple[str | None, tuple[int, int] | None]:
    """returns `(refusal, draft_reference)`, exactly one of them set. Each refusal
    is logged in its own branch with its own reason."""
    if post is None:
        logger.warning("publish_refused", reason="post_not_found", user_id=user_id, post_id=post_id)
        return POST_NOT_FOUND_ANSWER.format(post_id=post_id), None

    if post.channel_message_id is not None:
        logger.warning(
            "publish_refused", reason="already_published", user_id=user_id, post_id=post_id
        )
        return f"Пост {post_id} уже опубліковано.", None

    if post.status is PostStatus.PUBLISHING:
        # the `publishing` self-edge is for Celery redelivery, not a button:
        # `set_post_status` accepts it and a second tap would dispatch twice.
        logger.warning(
            "publish_refused", reason="already_publishing", user_id=user_id, post_id=post_id
        )
        return f"Пост {post_id} уже публікується.", None

    if post.status in MEDIA_IN_PROGRESS_STATUSES:
        # `RESOLVED -> PUBLISHING` is still legal, so this tap would
        # otherwise be refused by a state name instead of by a reason.
        logger.warning(
            "publish_refused",
            reason="media_in_progress",
            status=post.status.value,
            user_id=user_id,
            post_id=post_id,
        )
        return MEDIA_IN_PROGRESS_ANSWER.format(post_id=post_id), None

    try:
        set_post_status(post, PostStatus.PUBLISHING)
    except IllegalStatusTransitionError:
        logger.warning(
            "publish_refused",
            reason="illegal_status",
            status=post.status.value,
            user_id=user_id,
            post_id=post_id,
        )
        return f"Пост {post_id} у стані «{post.status.value}» — публікація недоступна.", None

    return None, button_message_reference(post)


def claim_post_for_media(
    post: Post | None, post_id: int, user_id: int
) -> tuple[str | None, tuple[int, int] | None]:
    """the render button's mirror of `claim_post_for_publishing`."""
    if post is None:
        logger.warning("render_refused", reason="post_not_found", user_id=user_id, post_id=post_id)
        return POST_NOT_FOUND_ANSWER.format(post_id=post_id), None

    if post.channel_message_id is not None:
        logger.warning(
            "render_refused", reason="already_published", user_id=user_id, post_id=post_id
        )
        return f"Пост {post_id} уже опубліковано — відео йому вже не допоможе.", None

    if post.status in MEDIA_IN_PROGRESS_STATUSES:
        logger.warning(
            "render_refused",
            reason="media_in_progress",
            status=post.status.value,
            user_id=user_id,
            post_id=post_id,
        )
        return MEDIA_IN_PROGRESS_ANSWER.format(post_id=post_id), None

    if post.status is PostStatus.RENDERED:
        logger.warning(
            "render_refused", reason="already_rendered", user_id=user_id, post_id=post_id
        )
        return VIDEO_READY_ANSWER.format(post_id=post_id), None

    if post.preview_message_id is not None:
        logger.warning(
            "render_refused",
            reason="already_previewed",
            status=post.status.value,
            user_id=user_id,
            post_id=post_id,
        )
        return PREVIEW_ALREADY_SHOWN_ANSWER.format(post_id=post_id), None

    try:
        set_post_status(post, PostStatus.DOWNLOADING)
    except IllegalStatusTransitionError:
        logger.warning(
            "render_refused",
            reason="illegal_status",
            status=post.status.value,
            user_id=user_id,
            post_id=post_id,
        )
        return f"Пост {post_id} у стані «{post.status.value}» — відео недоступне.", None

    return None, button_message_reference(post)


def button_message_reference(post: Post) -> tuple[int, int] | None:
    """once a preview exists it carries the button: the text draft lost its
    keyboard when the preview arrived."""
    if post.preview_chat_id is not None and post.preview_message_id is not None:
        return post.preview_chat_id, post.preview_message_id
    if post.draft_chat_id is not None and post.draft_message_id is not None:
        return post.draft_chat_id, post.draft_message_id
    return None


async def remove_keyboard(
    bot: Bot, draft_reference: tuple[int, int] | None, failure_event: str
) -> None:
    """cosmetic on its own — the status transition above is the real guard —
    so a failure here must not stop work the database already owns."""
    if draft_reference is None:
        return

    chat_id, message_id = draft_reference
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramAPIError:
        logger.warning(failure_event, message_id=message_id, exc_info=True)


@router.message(F.text)
async def handle_link(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """commands above already claimed everything starting with `/`. One link, or
    a «platform link + clip» pair in one message."""
    if message.from_user is None or message.text is None:
        return

    submitted_urls = split_links(message.text)

    if len(submitted_urls) > 2:
        await message.answer(TOO_MANY_LINKS_ANSWER.format(count=len(submitted_urls)))
        logger.info("link_rejected", reason="too_many_links", link_count=len(submitted_urls))
        return

    classified_urls = [classify_url(submitted_url) for submitted_url in submitted_urls]

    for submitted_url, candidate in zip(submitted_urls, classified_urls, strict=True):
        rejection = reject_url(submitted_url, candidate)
        if rejection is not None:
            answer, log_fields = rejection
            await message.answer(answer)
            logger.info("link_rejected", **log_fields)
            return

    clip_count = sum(1 for url in classified_urls if url.platform == CLIP_PLATFORM)
    if len(classified_urls) == 2 and clip_count != 1:
        both_clips = clip_count == 2
        await message.answer(TWO_CLIPS_ANSWER if both_clips else TWO_PLATFORM_LINKS_ANSWER)
        logger.info(
            "link_rejected",
            reason="two_clips" if both_clips else "two_platform_links",
            platforms=[url.platform for url in classified_urls],
        )
        return

    raw_url, classified, clip = select_track_source(submitted_urls, classified_urls)

    post_id: int | None = None
    track_existed = False
    failure_answer: str | None = None

    async with session_scope(session_factory) as session:
        try:
            track, track_existed = await get_or_create_track(session, raw_url, classified, clip)

            post = Post(track_id=track.id, created_by_user_id=message.from_user.id)
            session.add(post)
            await session.flush()
        except IntegrityError:
            # the worker's cleanup deleted this track between the SELECT and
            # this INSERT; unhandled, aiogram answers the admin nothing.
            await session.rollback()
            logger.warning("link_post_insert_lost_its_track")
            failure_answer = "Трек саме прибрали з бази. Надішліть посилання ще раз."
        except DBAPIError:
            # `lock_timeout` is on the bot engine too, so this can raise a plain
            # `DBAPIError`, which the `IntegrityError` clause above misses.
            await session.rollback()
            logger.exception("link_database_error")
            failure_answer = "База даних зараз недоступна або зайнята. Надішліть посилання ще раз."
        else:
            set_post_status(post, PostStatus.RESOLVING)
            post_id = post.id

    if failure_answer is not None:
        await message.answer(failure_answer)
        return
    assert post_id is not None

    bind_post_id(post_id)
    logger.info(
        "link_received",
        post_id=post_id,
        track_existed=track_existed,
        link_count=len(submitted_urls),
        clip_attached=clip is not None,
    )

    if not await dispatch_resolve_task(celery_client, post_id):
        await mark_dispatch_failure(
            session_factory,
            post_id,
            expected_status=PostStatus.RESOLVING,
            error=DISPATCH_FAILED_ERROR,
        )
        await message.answer(
            f"Пост {post_id} створено, але не вдалося поставити задачу в чергу. "
            f"Спробуйте /retry {post_id}."
        )
        return

    receipt = f"Приймаю в обробку, post_id={post_id}."
    if track_existed:
        receipt += " Цей трек уже реєструвався раніше — створюю ще один пост."
    await message.answer(receipt)


def stage_has_gone_stale(post: Post, time_limit_seconds: int) -> bool:
    """the only exit when the worker died mid-task. It never races a live task:
    Celery's hard limit kills one first, so a running task is always fresher."""
    age_seconds = (datetime.now(UTC) - post.updated_at).total_seconds()
    return age_seconds > time_limit_seconds


def stage_time_limit_seconds(status: PostStatus) -> int:
    """each stage against its own hard limit (resolve's in `worker.celery_app`,
    the media ones in `Settings`), so raising one never widens another's window."""
    if status is PostStatus.DOWNLOADING:
        return settings.download_task_time_limit_seconds
    if status in (PostStatus.DOWNLOADED, PostStatus.RENDERING):
        return settings.render_task_time_limit_seconds
    return TASK_TIME_LIMIT_SECONDS


def split_links(text: str) -> list[str]:
    """only when *every* token is a link: prose around a link stays a single
    unrecognized submission."""
    tokens = text.split()
    if len(tokens) > 1 and all(is_safe_url(token) for token in tokens):
        return tokens
    return [text.strip()]


def reject_url(raw_url: str, classified: ClassifiedUrl) -> tuple[str, dict[str, object]] | None:
    """the three syntactic refusals as `(answer, log fields)`, so a pair is
    validated link by link rather than as one opaque blob."""
    # UNIQUE on a btree tops out near 2704 bytes and raises
    # ProgramLimitExceededError, which `except IntegrityError` misses.
    if not is_within_length_limit(raw_url) or not is_within_length_limit(classified.normalized_url):
        return (
            f"Посилання задовге (ліміт {MAX_NORMALIZED_URL_BYTES} байтів). "
            f"Надішліть коротке посилання на трек.",
            {
                "reason": "too_long",
                "raw_url_bytes": len(raw_url.encode("utf-8")),
                "normalized_url_bytes": len(classified.normalized_url.encode("utf-8")),
                "limit_bytes": MAX_NORMALIZED_URL_BYTES,
            },
        )

    if classified.kind is UrlKind.NOT_TRACK:
        reason = NOT_TRACK_REASON_UKRAINIAN.get(classified.not_track_reason or "", "не трек")
        return (
            f"Це посилання на {reason}, а не на трек. Надішліть пряме посилання на трек.",
            {
                "reason": "not_track",
                "not_track_reason": classified.not_track_reason,
                "normalized_url": classified.normalized_url,
            },
        )

    if classified.kind is UrlKind.UNKNOWN:
        # only the length: the raw text of an unrecognized message is not a
        # URL and has no business in the logs.
        return (
            "Не розпізнав це як посилання на трек.",
            {"reason": "unknown", "raw_text_length": len(raw_url)},
        )

    return None


def select_track_source(
    submitted_urls: list[str], classified_urls: list[ClassifiedUrl]
) -> tuple[str, ClassifiedUrl, ClassifiedUrl | None]:
    """a pair resolves from its platform half: the clip's own metadata is
    the raw YouTube caption."""
    clip = next((url for url in classified_urls if url.platform == CLIP_PLATFORM), None)

    for submitted_url, classified in zip(submitted_urls, classified_urls, strict=True):
        if classified.platform != CLIP_PLATFORM:
            return submitted_url, classified, clip

    return submitted_urls[0], classified_urls[0], clip


def seed_user_input_clip(track: Track, clip: ClassifiedUrl | None) -> None:
    """seeded now, so the clip survives a resolve that never completes; `manual`
    is the top tier, so every later resolve keeps it."""
    if clip is None:
        return

    # a clip sent by the admin outweighs an earlier removal of the same platform
    if CLIP_PLATFORM in (track.removed_platforms or []):
        track.removed_platforms = [
            platform for platform in track.removed_platforms if platform != CLIP_PLATFORM
        ]

    stored = track.platform_links or {}
    link = {"url": clip.normalized_url, "source": USER_INPUT_SOURCE, "match": "manual"}
    if stored.get(CLIP_PLATFORM) == link:
        return

    # reassigned, never mutated in place: the column is plain JSONB, so an
    # in-place edit would never be seen as dirty and would not be written.
    track.platform_links = {**stored, CLIP_PLATFORM: link}


async def get_or_create_track(
    session: AsyncSession,
    raw_url: str,
    classified: ClassifiedUrl,
    clip: ClassifiedUrl | None = None,
) -> tuple[Track, bool]:
    """race-safe on UNIQUE `source_url_normalized`: the INSERT loser re-reads the
    winner. Clip seeded before the flush: re-reading a server default is a lazy load."""
    existing = await select_track_by_url(session, classified.normalized_url)
    if existing is not None:
        seed_user_input_clip(existing, clip)
        return existing, True

    # `DBAPIError` is deliberately not caught here — the caller turns it into
    # an answer to the admin, and this function has no message of its own.
    track = Track(source_url=raw_url, source_url_normalized=classified.normalized_url)
    seed_user_input_clip(track, clip)

    session.add(track)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await select_track_by_url(session, classified.normalized_url)
        if existing is not None:
            seed_user_input_clip(existing, clip)
            return existing, True
        raise
    return track, False


async def select_track_by_url(session: AsyncSession, normalized_url: str) -> Track | None:
    return await session.scalar(select(Track).where(Track.source_url_normalized == normalized_url))


async def mark_dispatch_failure(
    session_factory: async_sessionmaker[AsyncSession],
    post_id: int,
    *,
    expected_status: PostStatus,
    error: str,
) -> None:
    """the post is committed as in-progress with no task to move it, so the
    bot's own "спробуйте /retry" advice would be false until it goes stale."""
    try:
        async with session_scope(session_factory) as session:
            post = await session.get(Post, post_id, with_for_update=True)
            if post is None or post.status is not expected_status:
                return

            set_post_status(post, PostStatus.FAILED, error=error)
    except Exception:
        logger.exception("dispatch_failure_status_write_failed", post_id=post_id)


async def dispatch_task(
    celery_client: Celery,
    celery_task_name: str,
    task_kwargs: dict[str, int],
    failure_event: str,
    **failure_fields: object,
) -> bool:
    """by name: `bot/` never imports `worker.tasks`. `send_task()` blocks, so it runs
    off-loop, or an unreachable Redis stalls this single-loop process."""
    try:
        await asyncio.to_thread(celery_client.send_task, celery_task_name, kwargs=task_kwargs)
    except Exception:
        logger.exception(failure_event, **failure_fields)
        return False
    return True


async def dispatch_resolve_task(celery_client: Celery, post_id: int) -> bool:
    return await dispatch_task(
        celery_client,
        RESOLVE_POST_TASK_NAME,
        {"post_id": post_id},
        "resolve_task_dispatch_failed",
        post_id=post_id,
    )


async def dispatch_media_task(celery_client: Celery, task_name: str, post_id: int) -> bool:
    return await dispatch_task(
        celery_client,
        task_name,
        {"post_id": post_id},
        "media_task_dispatch_failed",
        task_name=task_name,
        post_id=post_id,
    )


async def dispatch_publish_task(
    celery_client: Celery, post_id: int, published_by_user_id: int
) -> bool:
    """`published_by_user_id` is whoever tapped the button or ran `/retry`, not
    whoever submitted the link."""
    return await dispatch_task(
        celery_client,
        PUBLISH_POST_TASK_NAME,
        {"post_id": post_id, "published_by_user_id": published_by_user_id},
        "publish_task_dispatch_failed",
        post_id=post_id,
    )
