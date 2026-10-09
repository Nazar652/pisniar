"""The `/api` routes: the session exchange (public), the signed audio route and everything else
behind `require_admin`.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Request

from pisniar.logging import get_logger
from pisniar.webapp.audio import register_audio_route
from pisniar.webapp.auth import (
    AdminUser,
    InitDataError,
    WebappContext,
    init_data_field_names,
    issue_session_token,
    require_admin,
    validate_init_data,
)
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.posts import register_post_routes
from pisniar.webapp.schemas import ApiError, MeResponse, SessionRequest, SessionResponse

logger = get_logger(__name__)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiError},
    403: {"model": ApiError},
    422: {"model": ApiError},
}


async def create_session(body: SessionRequest, request: Request) -> SessionResponse:
    context = cast(WebappContext, request.app.state.context)
    now = int(context.clock())

    field_names, unknown_fields = init_data_field_names(body.init_data)
    logger.info(
        "webapp_init_data_received",
        fields=field_names,
        unknown_fields=unknown_fields,
        has_signature="signature" in field_names,
    )

    try:
        user_id = validate_init_data(body.init_data, context.bot_token, now, context.auth_window)
    except InitDataError as error:
        logger.warning("webapp_auth_rejected", reason=error.reason)
        raise ApiProblem(
            401, "invalid_init_data", "Дані Telegram недійсні. Відкрийте застосунок знову."
        ) from None

    if user_id not in context.admin_ids:
        logger.warning("webapp_auth_rejected", reason="not_admin", user_id=user_id)
        raise ApiProblem(403, "forbidden", "Доступ лише для адміністраторів.")

    token = issue_session_token(user_id, context.bot_token, now, context.session_ttl_seconds)
    logger.info("webapp_session_issued", user_id=user_id)
    return SessionResponse(token=token, expires_in=context.session_ttl_seconds)


async def read_me(admin: Annotated[AdminUser, Depends(require_admin)]) -> MeResponse:
    return MeResponse(user_id=admin.user_id)


def register_api_routes(app: FastAPI) -> None:
    """direct registration, not `include_router`: the route table stays a flat list of
    `APIRoute` that a test can enumerate."""
    app.add_api_route(
        "/api/session",
        create_session,
        methods=["POST"],
        response_model=SessionResponse,
        responses=ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/me", read_me, methods=["GET"], response_model=MeResponse, responses=ERROR_RESPONSES
    )
    register_post_routes(app)
    register_audio_route(app)
