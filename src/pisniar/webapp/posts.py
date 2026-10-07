"""The read routes of the Mini App: the list of posts, one post and the static facts."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Request

from pisniar.webapp.auth import require_admin
from pisniar.webapp.database import read_from_database
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.meta import build_meta
from pisniar.webapp.schemas import (
    POST_ID_MAX,
    ApiError,
    MetaResponse,
    PostDetail,
    PostListResponse,
)
from pisniar.webapp.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    fetch_post_detail,
    fetch_post_page,
)

READ_ERROR_RESPONSES: dict[int | str, dict[str, type[ApiError]]] = {
    401: {"model": ApiError},
    403: {"model": ApiError},
    404: {"model": ApiError},
    422: {"model": ApiError},
    503: {"model": ApiError},
}


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
        "/api/meta",
        read_meta,
        methods=["GET"],
        response_model=MetaResponse,
        responses=READ_ERROR_RESPONSES,
        dependencies=protected,
    )
