"""The post routes of the Mini App: the list, one post, its fragment, the edits and the facts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, cast

from aiogram import Bot
from fastapi import Depends, FastAPI, Path, Query, Request

from pisniar.logging import get_logger
from pisniar.webapp.applying import apply_caption, archive_post, edit_remaining
from pisniar.webapp.auth import AdminUser, WebappContext, require_admin
from pisniar.webapp.database import read_from_database, write_to_database
from pisniar.webapp.editing import POST_NOT_FOUND_MESSAGE, preview_fields, save_fields
from pisniar.webapp.errors import ApiProblem, FieldsInvalidError
from pisniar.webapp.fragment_view import build_fragment_view, fetch_fragment_source
from pisniar.webapp.meta import build_meta
from pisniar.webapp.schemas import (
    POST_ID_MAX,
    ApiError,
    ApplyRequest,
    ApplyResponse,
    ArchiveResponse,
    FieldsInput,
    FieldsInvalid,
    FieldsUpdate,
    FragmentView,
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

logger = get_logger(__name__)

TELEGRAM_UNAVAILABLE_MESSAGE = "Зв'язок із Telegram не налаштовано."
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
    {"post_not_found", "post_busy", "database_unavailable", "telegram_unavailable"}
)


@asynccontextmanager
async def refusals_logged(event: str, user_id: int, post_id: int) -> AsyncIterator[None]:
    """one warning with the user and the post for a refusal that has no log line of its own."""
    try:
        yield
    except ApiProblem as problem:
        if problem.code in LOGGED_AT_THE_ROUTE:
            logger.warning(event, reason=problem.code, user_id=user_id, post_id=post_id)
        raise
    except FieldsInvalidError as invalid:
        fields = [error.field for error in invalid.errors]
        logger.warning(
            event, reason="invalid_fields", fields=fields, user_id=user_id, post_id=post_id
        )
        raise


async def list_posts(
    request: Request,
    before_id: Annotated[int | None, Query(ge=1, le=POST_ID_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PostListResponse:
    return await read_from_database(
        request, lambda session: fetch_post_page(session, before_id, limit)
    )


async def read_post(
    request: Request, post_id: Annotated[int, Path(ge=1, le=POST_ID_MAX)]
) -> PostDetail:
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
        archived = await write_to_database(
            request, lambda session: archive_post(session, bot, post_id, admin.user_id)
        )
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
