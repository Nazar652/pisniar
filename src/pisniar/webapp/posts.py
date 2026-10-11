"""The post routes of the Mini App: the list, one post, its fragment, the edits and the facts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, cast

from aiogram import Bot
from celery import Celery
from fastapi import Depends, FastAPI, Path, Query, Request

from pisniar.db.states import PostStatus
from pisniar.logging import get_logger
from pisniar.webapp.applying import apply_caption, archive_post, edit_remaining
from pisniar.webapp.audio_preparation import (
    cancel_preparation,
    enqueue_audio_preparation,
    prepare_audio,
    release_draft_keyboard,
    restore_draft,
)
from pisniar.webapp.auth import AdminUser, WebappContext, require_admin
from pisniar.webapp.cover_publication import enqueue_cover_publication, start_cover_publication
from pisniar.webapp.database import read_from_database, write_to_database
from pisniar.webapp.editing import POST_NOT_FOUND_MESSAGE, preview_fields, save_fields
from pisniar.webapp.errors import ApiProblem, FieldsInvalidError
from pisniar.webapp.fragment_choice import (
    choose_fragment,
    enqueue_render,
    release_superseded_preview,
)
from pisniar.webapp.fragment_view import build_fragment_view, fetch_fragment_source
from pisniar.webapp.lyrics_editing import fetch_lyrics_view, replace_lyrics
from pisniar.webapp.lyrics_proposals import propose_plain_lyrics, propose_synced_lyrics
from pisniar.webapp.meta import build_meta
from pisniar.webapp.schemas import (
    POST_ID_MAX,
    ApiError,
    ApplyRequest,
    ApplyResponse,
    ArchiveResponse,
    AudioPreparationCancelled,
    AudioPreparationStarted,
    CancelPreparationRequest,
    CoverPublicationRequest,
    CoverPublicationStarted,
    FieldsInput,
    FieldsInvalid,
    FieldsUpdate,
    FragmentRenderStarted,
    FragmentRequest,
    FragmentView,
    LyricsProposal,
    LyricsSourceName,
    LyricsUpdate,
    LyricsView,
    MetaResponse,
    PostDetail,
    PostListResponse,
    PreviewResponse,
)
from pisniar.webapp.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    fetch_post_detail,
    fetch_post_page,
)
from pisniar.webapp.status import STATUS_LABELS

logger = get_logger(__name__)

TELEGRAM_UNAVAILABLE_MESSAGE = "Зв'язок із Telegram не налаштовано."
QUEUE_UNAVAILABLE_MESSAGE = "Зв'язок із чергою завдань не налаштовано."
READ_ERROR_RESPONSES: dict[int | str, dict[str, type[ApiError]]] = {
    401: {"model": ApiError},
    403: {"model": ApiError},
    404: {"model": ApiError},
    422: {"model": ApiError},
    503: {"model": ApiError},
}
EDIT_ERROR_RESPONSES: dict[int | str, dict[str, type[ApiError]]] = {
    **READ_ERROR_RESPONSES,
    409: {"model": ApiError},
    422: {"model": FieldsInvalid},
}
# the refusals no deeper layer logs; every other one is logged where it is decided
LOGGED_AT_THE_ROUTE = frozenset(
    {
        "post_not_found",
        "post_busy",
        "database_unavailable",
        "telegram_unavailable",
        "queue_unavailable",
    }
)


@asynccontextmanager
async def refusals_logged(
    event: str,
    user_id: int,
    post_id: int,
) -> AsyncIterator[None]:
    """one warning with the user and the post for a refusal that has no log line of its own."""
    try:
        yield
    except ApiProblem as problem:
        if problem.code in LOGGED_AT_THE_ROUTE:
            logger.warning(event, reason=problem.code, user_id=user_id, post_id=post_id)
        raise
    except FieldsInvalidError as invalid:
        fields = [error.field for error in invalid.errors]
        logger.warning(event, reason="invalid_fields", fields=fields, user_id=user_id, post_id=post_id)
        raise


async def list_posts(
    request: Request,
    before_id: Annotated[int | None, Query(ge=1, le=POST_ID_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PostListResponse:
    return await read_from_database(request, lambda session: fetch_post_page(session, before_id, limit))


async def read_post(request: Request, post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)]) -> PostDetail:
    detail = await read_from_database(request, lambda session: fetch_post_detail(session, post_id))
    if detail is None:
        raise ApiProblem(404, "post_not_found", "Пост не знайдено.")
    return detail


def telegram_bot_of(request: Request) -> Bot:
    bot = cast("Bot | None", request.app.state.bot)
    if bot is None:
        logger.error("webapp_telegram_not_configured")
        raise ApiProblem(503, "telegram_unavailable", TELEGRAM_UNAVAILABLE_MESSAGE)
    return bot


def celery_client_of(request: Request) -> Celery:
    celery_client = cast("Celery | None", request.app.state.celery_client)
    if celery_client is None:
        logger.error("webapp_queue_not_configured")
        raise ApiProblem(503, "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE)
    return celery_client


def current_year_of(request: Request) -> int:
    context = cast(WebappContext, request.app.state.context)
    return datetime.fromtimestamp(context.clock(), UTC).year


async def save_post_fields(
    request: Request,
    update: FieldsUpdate,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> PostDetail:
    year = current_year_of(request)
    async with refusals_logged("fields_save_refused", admin.user_id, post_id):
        saved = await write_to_database(
            request, lambda session: save_fields(session, post_id, update, year, admin.user_id)
        )
        if saved is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
        return saved


async def preview_post_fields(
    request: Request,
    update: FieldsInput,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> PreviewResponse:
    year = current_year_of(request)
    async with refusals_logged("fields_preview_refused", admin.user_id, post_id):
        preview = await read_from_database(
            request, lambda session: preview_fields(session, post_id, update, year, admin.user_id)
        )
        if preview is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
        return preview


async def apply_post_caption(
    request: Request,
    body: ApplyRequest,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> ApplyResponse:
    public_url = cast("str | None", request.app.state.webapp_public_url)
    async with refusals_logged("caption_apply_refused", admin.user_id, post_id):
        bot = telegram_bot_of(request)
        applied = await write_to_database(
            request,
            lambda session: apply_caption(
                session, bot, post_id, body.expected_caption_sha256, admin.user_id, public_url
            ),
        )
        if applied is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)

    return await edit_remaining(bot, applied, admin.user_id, post_id)


async def archive_published_post(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> ArchiveResponse:
    async with refusals_logged("post_archive_refused", admin.user_id, post_id):
        bot = telegram_bot_of(request)
        archived = await write_to_database(request, lambda session: archive_post(session, bot, post_id, admin.user_id))
        if archived is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
        return archived


async def read_fragment(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> FragmentView:
    context = cast(WebappContext, request.app.state.context)
    async with refusals_logged("fragment_view_refused", admin.user_id, post_id):
        source = await read_from_database(
            request, lambda session: fetch_fragment_source(session, post_id, admin.user_id)
        )
        if source is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
        return await asyncio.to_thread(build_fragment_view, context, source, admin.user_id)


async def start_fragment_render(
    request: Request,
    body: FragmentRequest,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> FragmentRenderStarted:
    logger.info(
        "fragment_render_requested",
        user_id=admin.user_id,
        post_id=post_id,
        start_ms=body.start_ms,
        end_ms=body.end_ms,
    )
    async with refusals_logged("fragment_render_refused", admin.user_id, post_id):
        bot = telegram_bot_of(request)
        celery_client = celery_client_of(request)
        choice = await write_to_database(
            request, lambda session: choose_fragment(session, post_id, body, admin.user_id)
        )
        if choice is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)

    await enqueue_render(celery_client, request.app.state.session_factory, choice, admin.user_id)
    await release_superseded_preview(bot, choice, admin.user_id)
    return FragmentRenderStarted(
        post_id=post_id,
        status_label=STATUS_LABELS[PostStatus.RENDERING],
        start_ms=choice.bounds.start_ms,
        end_ms=choice.bounds.end_ms,
    )


async def start_audio_preparation(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> AudioPreparationStarted:
    logger.info("audio_preparation_requested", user_id=admin.user_id, post_id=post_id)
    async with refusals_logged("audio_preparation_refused", admin.user_id, post_id):
        bot = telegram_bot_of(request)
        celery_client = celery_client_of(request)
        preparation = await write_to_database(request, lambda session: prepare_audio(session, post_id, admin.user_id))
        if preparation is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)

    await enqueue_audio_preparation(celery_client, request.app.state.session_factory, preparation, admin.user_id)
    await release_draft_keyboard(bot, preparation.draft, post_id, admin.user_id)
    return AudioPreparationStarted(post_id=post_id, status_label=STATUS_LABELS[PostStatus.DOWNLOADING])


async def cancel_audio_preparation(
    request: Request,
    body: CancelPreparationRequest,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> AudioPreparationCancelled:
    logger.info("audio_preparation_cancel_requested", user_id=admin.user_id, post_id=post_id)
    public_url = cast("str | None", request.app.state.webapp_public_url)
    async with refusals_logged("audio_preparation_cancel_refused", admin.user_id, post_id):
        bot = telegram_bot_of(request)
        cancelled = await write_to_database(
            request, lambda session: cancel_preparation(session, post_id, body, admin.user_id)
        )
        if cancelled is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)

    logger.info("audio_preparation_cancelled", user_id=admin.user_id, post_id=post_id)
    await restore_draft(bot, cancelled, public_url, admin.user_id)
    return AudioPreparationCancelled(post_id=post_id, status_label=STATUS_LABELS[PostStatus.RESOLVED])


async def publish_cover(
    request: Request,
    body: CoverPublicationRequest,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> CoverPublicationStarted:
    logger.info("cover_publication_requested", user_id=admin.user_id, post_id=post_id)
    async with refusals_logged("cover_publication_refused", admin.user_id, post_id):
        bot = telegram_bot_of(request)
        celery_client = celery_client_of(request)
        publication = await write_to_database(
            request,
            lambda session: start_cover_publication(session, post_id, body, admin.user_id),
        )
        if publication is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)

    await enqueue_cover_publication(celery_client, request.app.state.session_factory, publication, admin.user_id)
    await release_draft_keyboard(bot, publication.draft, post_id, admin.user_id)
    return CoverPublicationStarted(post_id=post_id, status_label=STATUS_LABELS[PostStatus.PUBLISHING])


async def read_lyrics(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> LyricsView:
    async with refusals_logged("lyrics_view_refused", admin.user_id, post_id):
        view = await read_from_database(request, lambda session: fetch_lyrics_view(session, post_id, admin.user_id))
        if view is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
        return view


async def save_lyrics(
    request: Request,
    update: LyricsUpdate,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> LyricsView:
    async with refusals_logged("lyrics_save_refused", admin.user_id, post_id):
        saved = await write_to_database(
            request, lambda session: replace_lyrics(session, post_id, update, admin.user_id)
        )
        if saved is None:
            raise ApiProblem(404, "post_not_found", POST_NOT_FOUND_MESSAGE)
        return saved


async def find_synced_lyrics(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> LyricsProposal:
    async with refusals_logged("lyrics_lookup_refused", admin.user_id, post_id):
        return await propose_synced_lyrics(request, post_id, admin.user_id)


async def find_plain_lyrics(
    request: Request,
    source: Annotated[LyricsSourceName, Query()],
    admin: Annotated[AdminUser, Depends(require_admin)],
    post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)],
) -> LyricsProposal:
    async with refusals_logged("lyrics_lookup_refused", admin.user_id, post_id):
        return await propose_plain_lyrics(request, post_id, source, admin.user_id)


async def read_meta() -> MetaResponse:
    return build_meta()


def register_post_routes(app: FastAPI) -> None:
    protected = [Depends(require_admin)]
    app.add_api_route(
        "/api/posts",
        list_posts,
        methods=["GET"],
        response_model=PostListResponse,
        responses=READ_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}",
        read_post,
        methods=["GET"],
        response_model=PostDetail,
        responses=READ_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/fragment",
        read_fragment,
        methods=["GET"],
        response_model=FragmentView,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/fragment",
        start_fragment_render,
        methods=["POST"],
        response_model=FragmentRenderStarted,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/fragment/audio",
        start_audio_preparation,
        methods=["POST"],
        response_model=AudioPreparationStarted,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/fragment/cancel",
        cancel_audio_preparation,
        methods=["POST"],
        response_model=AudioPreparationCancelled,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/publish-cover",
        publish_cover,
        methods=["POST"],
        response_model=CoverPublicationStarted,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/lyrics",
        read_lyrics,
        methods=["GET"],
        response_model=LyricsView,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/lyrics",
        save_lyrics,
        methods=["PUT"],
        response_model=LyricsView,
        responses=EDIT_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/posts/{post_id}/lyrics/synced",
        find_synced_lyrics,
        methods=["GET"],
        response_model=LyricsProposal,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/lyrics/plain",
        find_plain_lyrics,
        methods=["GET"],
        response_model=LyricsProposal,
        responses=EDIT_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/meta",
        read_meta,
        methods=["GET"],
        response_model=MetaResponse,
        responses=READ_ERROR_RESPONSES,
        dependencies=protected,
    )
    app.add_api_route(
        "/api/posts/{post_id}/fields",
        save_post_fields,
        methods=["PUT"],
        response_model=PostDetail,
        responses=EDIT_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/posts/{post_id}/preview",
        preview_post_fields,
        methods=["POST"],
        response_model=PreviewResponse,
        responses=EDIT_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/posts/{post_id}/apply",
        apply_post_caption,
        methods=["POST"],
        response_model=ApplyResponse,
        responses=EDIT_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/posts/{post_id}/archive",
        archive_published_post,
        methods=["POST"],
        response_model=ArchiveResponse,
        responses=EDIT_ERROR_RESPONSES,
    )
