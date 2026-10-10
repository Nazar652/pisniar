"""The signed audio route: the token in the query is the credential, the file is found only from the ids inside it, and
Starlette's `FileResponse` answers Range requests. Nothing here reads the database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, cast

from fastapi import FastAPI, Query, Request, Response
from fastapi import Path as PathParameter
from fastapi.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

from pisniar.logging import get_logger
from pisniar.webapp.auth import WebappContext, authorize_media_request
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import POST_ID_MAX
from pisniar.worker.storage import AUDIO_DIRECTORY_NAME, find_existing_audio

logger = get_logger(__name__)

# the container decides the type, never the sniffed bytes; anything else is refused
AUDIO_CONTENT_TYPES = {
    "webm": "audio/webm",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
}
TOKEN_MAX_LENGTH = 512
AUDIO_GONE_MESSAGE = "Аудіо більше не доступне. Оновіть сторінку."
AUDIO_FORMAT_MESSAGE = "Цей формат аудіо не можна відтворити в редакторі."


class LoggedAudioResponse(FileResponse):
    """logs the final HTTP status of the one response it sends; never the URL."""

    def __init__(
        self,
        path: Path,
        media_type: str,
        user_id: int,
        post_id: int,
    ) -> None:
        super().__init__(path, media_type=media_type)
        self.user_id = user_id
        self.post_id = post_id

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        ranged = any(name == b"range" for name, _ in scope["headers"])

        async def send_and_log(message: Message) -> None:
            if message["type"] == "http.response.start":
                logger.info(
                    "fragment_audio_served",
                    post_id=self.post_id,
                    user_id=self.user_id,
                    http_status=message["status"],
                    ranged=ranged,
                )
            await send(message)

        await super().__call__(scope, receive, send_and_log)


def audio_content_type(path: Path) -> str | None:
    return AUDIO_CONTENT_TYPES.get(path.suffix.removeprefix(".").lower())


def locate_audio_file(
    media_root: Path,
    track_id: int,
    video_id: str,
) -> Path | None:
    """the audio file of the ids, or `None` if it is gone or resolves outside `audio/`."""
    found = find_existing_audio(media_root, track_id, video_id)
    if found is None:
        return None

    resolved = found.resolve()
    if not resolved.is_relative_to((media_root / AUDIO_DIRECTORY_NAME).resolve()):
        logger.error("fragment_audio_outside_volume", track_id=track_id)
        return None
    return resolved


async def stream_audio(
    request: Request,
    post_id: Annotated[int, PathParameter(ge=1, le=POST_ID_MAX)],
    token: Annotated[str, Query(min_length=1, max_length=TOKEN_MAX_LENGTH)],
) -> Response:
    claims = authorize_media_request(request, token, post_id)
    context = cast(WebappContext, request.app.state.context)

    audio_path = await asyncio.to_thread(locate_audio_file, context.media_root, claims.track_id, claims.video_id)
    if audio_path is None:
        logger.warning("fragment_audio_gone", post_id=post_id, user_id=claims.user_id)
        raise ApiProblem(404, "audio_gone", AUDIO_GONE_MESSAGE)

    content_type = audio_content_type(audio_path)
    if content_type is None:
        logger.warning(
            "fragment_audio_format_unsupported",
            post_id=post_id,
            user_id=claims.user_id,
            extension=audio_path.suffix,
        )
        raise ApiProblem(409, "audio_format_unsupported", AUDIO_FORMAT_MESSAGE)

    return LoggedAudioResponse(audio_path, content_type, claims.user_id, post_id)


def register_audio_route(app: FastAPI) -> None:
    app.add_api_route("/api/posts/{post_id}/audio", stream_audio, methods=["GET"], include_in_schema=False)
