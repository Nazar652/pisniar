"""The Mini App server: security headers, the built frontend and the `/api` router.

Public routes are exactly `GET /`, `GET /assets/*` and `POST /api/session`; the audio route is signed by its own token,
and every other route sits behind `require_admin`. The production CSP lives only here.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot
from celery import Celery
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.config import Settings
from pisniar.webapp.api import register_api_routes
from pisniar.webapp.auth import AuthWindow, WebappContext
from pisniar.webapp.errors import ApiProblem, error_response, register_error_handlers

# the hash allows the one `<style>` wavesurfer writes into a shadow root (at the fixed waveform
# height); it belongs to this version only
WAVESURFER_VERSION = "7.12.12"
WAVESURFER_STYLE_HASH = "sha256-Y/8t1dAKa3ay+RZEiEFEDr8dFiQhpaFvYXXUfT3p6ZU="
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self' https://telegram.org; "
    f"style-src 'self' '{WAVESURFER_STYLE_HASH}'; "
    "img-src 'self'; media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MAX_BODY_BYTES = 16 * 1024
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def create_webapp(
    settings: Settings,
    clock: Callable[[], float] = time.time,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    bot: Bot | None = None,
    celery_client: Celery | None = None,
) -> FastAPI:
    app = FastAPI(title="pisniar webapp", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.context = WebappContext(
        bot_token=settings.bot_token.get_secret_value(),
        admin_ids=frozenset(settings.admin_ids),
        auth_window=AuthWindow(
            max_age_seconds=settings.webapp_auth_max_age_seconds,
            max_future_seconds=settings.webapp_auth_max_future_seconds,
        ),
        session_ttl_seconds=settings.webapp_session_ttl_seconds,
        media_url_ttl_seconds=settings.webapp_media_url_ttl_seconds,
        media_root=settings.media_root,
        clock=clock,
    )
    app.state.session_factory = session_factory
    app.state.bot = bot
    app.state.celery_client = celery_client
    app.state.webapp_public_url = settings.webapp_public_url
    register_error_handlers(app)

    @app.middleware("http")
    async def harden(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rejection = reject_oversized_body(request)
        response = rejection if rejection is not None else await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    static_directory = settings.webapp_static_dir

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        index_file = static_directory / "index.html"
        if not index_file.is_file():
            return error_response(503, "frontend_missing", "Інтерфейс ще не зібрано.")
        return FileResponse(index_file, media_type="text/html; charset=utf-8")

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def asset(asset_path: str) -> Response:
        asset_file = resolve_asset_file(static_directory / "assets", asset_path)
        if asset_file is None:
            raise ApiProblem(404, "not_found", "Не знайдено.")
        return FileResponse(asset_file, headers={"Cache-Control": IMMUTABLE_CACHE})

    register_api_routes(app)
    return app


def reject_oversized_body(request: Request) -> Response | None:
    """a body is accepted only with an honest `Content-Length`: chunked uploads get no room."""
    if request.method not in BODY_METHODS:
        return None
    declared = request.headers.get("Content-Length")
    if declared is None or not declared.isdecimal():
        return error_response(411, "length_required", "Потрібен заголовок Content-Length.")
    if int(declared) > MAX_BODY_BYTES:
        return error_response(413, "body_too_large", "Запит завеликий.")
    return None


def resolve_asset_file(assets_root: Path, asset_path: str) -> Path | None:
    """the file under `assets_root` or `None`: `..` and symlinks must not leave the directory."""
    try:
        root = assets_root.resolve()
        candidate = (root / asset_path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate
