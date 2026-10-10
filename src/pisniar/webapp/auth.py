"""`initData` validation, signed session and audio tokens and the `require_admin` dependency.

Each token kind has its own key derived from the bot token, so rotating the token invalidates every token and a token of
one kind never verifies as the other. Nothing here reads the database: a dead Postgres can never look like a 401.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl

from aiogram.utils.web_app import check_webapp_signature
from fastapi import Request

from pisniar.logging import get_logger
from pisniar.resolve.json_utils import as_int
from pisniar.resolve.matching import is_valid_youtube_video_id
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import INIT_DATA_MAX_LENGTH

logger = get_logger(__name__)

# the fields Telegram documents in `initData`; only these names are ever logged
INIT_DATA_FIELD_NAMES = frozenset(
    {
        "auth_date",
        "can_send_after",
        "chat",
        "chat_instance",
        "chat_type",
        "hash",
        "query_id",
        "receiver",
        "signature",
        "start_param",
        "user",
    }
)

SESSION_KEY_CONTEXT = b"pisniar-webapp-session-v1"
MEDIA_KEY_CONTEXT = b"pisniar-webapp-media-v1"
MEDIA_TOKEN_REFUSED_MESSAGE = "Посилання на аудіо недійсне або застаріло. Оновіть сторінку."
BEARER_PREFIX = "Bearer "
# room before `iat` for clock drift between the process that issued a token and this one.
TOKEN_IAT_SKEW_SECONDS = 60


class InitDataError(Exception):
    """`reason` is a short machine code; it is logged and never sent to the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AuthWindow:
    max_age_seconds: int
    max_future_seconds: int


@dataclass(frozen=True)
class WebappContext:
    """what the auth layer needs per process; stored on `app.state.context`."""

    bot_token: str
    admin_ids: frozenset[int]
    auth_window: AuthWindow
    session_ttl_seconds: int
    media_url_ttl_seconds: int
    media_root: Path
    clock: Callable[[], float]


@dataclass(frozen=True)
class AdminUser:
    user_id: int


@dataclass(frozen=True)
class MediaClaims:
    """what a signed audio URL grants: one admin, one post, one recording of one track."""

    user_id: int
    post_id: int
    track_id: int
    video_id: str


def init_data_field_names(init_data: str) -> tuple[list[str], int]:
    """the known field names present and how many unknown ones there are, never a value; a malformed string gives
    nothing, and a name outside the list is counted, not echoed."""
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return [], 0

    names = {name for name, value in pairs}
    return sorted(names & INIT_DATA_FIELD_NAMES), len(names - INIT_DATA_FIELD_NAMES)


def validate_init_data(
    init_data: str,
    bot_token: str,
    now: int,
    window: AuthWindow,
) -> int:
    """return the Telegram user id of a genuine, fresh `initData`, else raise `InitDataError`."""
    if len(init_data) > INIT_DATA_MAX_LENGTH:
        raise InitDataError("too_long")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError("malformed") from None

    names = [name for name, value in pairs]
    if len(set(names)) != len(names):
        raise InitDataError("duplicate_key")
    # aiogram drops blank values before it hashes; refusing them keeps both parsers on one view.
    if any(not value for name, value in pairs):
        raise InitDataError("malformed")
    fields = dict(pairs)
    if "hash" not in fields:
        raise InitDataError("missing_hash")

    if not check_webapp_signature(bot_token, init_data):
        raise InitDataError("bad_signature")

    auth_date = parse_integer(fields.get("auth_date"))
    if auth_date is None:
        raise InitDataError("bad_auth_date")
    if not now - window.max_age_seconds <= auth_date <= now + window.max_future_seconds:
        raise InitDataError("auth_date_out_of_window")

    return read_user_id(fields.get("user"))


def derived_key(bot_token: str, key_context: bytes) -> bytes:
    """one key per purpose: a token signed for one context verifies in no other."""
    return hmac.new(bot_token.encode(), key_context, hashlib.sha256).digest()


def sign_claims(
    claims: Mapping[str, object],
    bot_token: str,
    key_context: bytes,
) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(derived_key(bot_token, key_context), payload, hashlib.sha256).digest()
    return f"{encode_base64url(payload)}.{encode_base64url(signature)}"


def verify_signed_claims(
    token: str,
    bot_token: str,
    key_context: bytes,
) -> dict[str, object]:
    """the claims of a correctly signed token; expiry and the fields are the caller's to check."""
    parts = token.split(".")
    if len(parts) != 2:
        raise InitDataError("token_malformed")

    payload = decode_base64url(parts[0])
    signature = decode_base64url(parts[1])
    if payload is None or signature is None:
        raise InitDataError("token_malformed")

    expected = hmac.new(derived_key(bot_token, key_context), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise InitDataError("token_bad_signature")

    try:
        claims = json.loads(payload)
    except ValueError:
        raise InitDataError("token_malformed") from None
    if not isinstance(claims, dict):
        raise InitDataError("token_malformed")
    return claims


def check_lifetime(claims: Mapping[str, object], now: int) -> None:
    issued_at = as_int(claims.get("iat"))
    expires_at = as_int(claims.get("exp"))
    if issued_at is None or expires_at is None:
        raise InitDataError("token_malformed")
    if expires_at <= now:
        raise InitDataError("token_expired")
    if issued_at > now + TOKEN_IAT_SKEW_SECONDS:
        raise InitDataError("token_from_future")


def issue_session_token(
    user_id: int,
    bot_token: str,
    now: int,
    ttl_seconds: int,
) -> str:
    claims = {"uid": user_id, "iat": now, "exp": now + ttl_seconds}
    return sign_claims(claims, bot_token, SESSION_KEY_CONTEXT)


def verify_session_token(
    token: str,
    bot_token: str,
    now: int,
) -> int:
    """return the user id of an unexpired, correctly signed token, else raise `InitDataError`."""
    claims = verify_signed_claims(token, bot_token, SESSION_KEY_CONTEXT)
    user_id = as_int(claims.get("uid"))
    if user_id is None:
        raise InitDataError("token_malformed")

    check_lifetime(claims, now)
    return user_id


def issue_media_token(
    claims: MediaClaims,
    bot_token: str,
    now: int,
    ttl_seconds: int,
) -> str:
    payload = {
        "uid": claims.user_id,
        "pid": claims.post_id,
        "tid": claims.track_id,
        "vid": claims.video_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return sign_claims(payload, bot_token, MEDIA_KEY_CONTEXT)


def verify_media_token(
    token: str,
    bot_token: str,
    now: int,
) -> MediaClaims:
    """the scope of an unexpired audio token, else `InitDataError`; no database is read."""
    claims = verify_signed_claims(token, bot_token, MEDIA_KEY_CONTEXT)
    user_id = as_int(claims.get("uid"))
    post_id = as_int(claims.get("pid"))
    track_id = as_int(claims.get("tid"))
    video_id = claims.get("vid")
    if user_id is None or post_id is None or track_id is None:
        raise InitDataError("token_malformed")
    if not isinstance(video_id, str) or not is_valid_youtube_video_id(video_id):
        raise InitDataError("token_bad_video_id")

    check_lifetime(claims, now)
    return MediaClaims(user_id=user_id, post_id=post_id, track_id=track_id, video_id=video_id)


def authorize_media_request(
    request: Request,
    token: str,
    post_id: int,
) -> MediaClaims:
    """the audio route's `require_admin`: the token is the credential, and the post in the path and the admin list are
    checked on every request."""
    context = cast(WebappContext, request.app.state.context)

    try:
        claims = verify_media_token(token, context.bot_token, int(context.clock()))
    except InitDataError as error:
        logger.warning("media_token_rejected", reason=error.reason, post_id=post_id)
        raise ApiProblem(401, "unauthorized", MEDIA_TOKEN_REFUSED_MESSAGE) from None

    if claims.post_id != post_id:
        logger.warning(
            "media_token_rejected",
            reason="post_mismatch",
            post_id=post_id,
            token_post_id=claims.post_id,
            user_id=claims.user_id,
        )
        raise ApiProblem(401, "unauthorized", MEDIA_TOKEN_REFUSED_MESSAGE)
    if claims.user_id not in context.admin_ids:
        logger.warning("media_token_rejected", reason="not_admin", post_id=post_id, user_id=claims.user_id)
        raise ApiProblem(403, "forbidden", "Доступ лише для адміністраторів.")
    return claims


def require_admin(request: Request) -> AdminUser:
    """dependency of every non-public route: a valid token, and an admin right now."""
    context = cast(WebappContext, request.app.state.context)

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith(BEARER_PREFIX):
        logger.warning("webapp_auth_rejected", reason="no_bearer_token")
        raise ApiProblem(401, "unauthorized", "Потрібна авторизація. Відкрийте застосунок з Telegram.")

    try:
        user_id = verify_session_token(authorization[len(BEARER_PREFIX) :], context.bot_token, int(context.clock()))
    except InitDataError as error:
        logger.warning("webapp_auth_rejected", reason=error.reason)
        raise ApiProblem(401, "unauthorized", "Сесія недійсна або завершилась. Відкрийте застосунок знову.") from None

    if user_id not in context.admin_ids:
        logger.warning("webapp_auth_rejected", reason="not_admin", user_id=user_id)
        raise ApiProblem(403, "forbidden", "Доступ лише для адміністраторів.")
    return AdminUser(user_id=user_id)


def read_user_id(raw_user: str | None) -> int:
    if raw_user is None:
        raise InitDataError("missing_user")

    try:
        user = json.loads(raw_user)
    except ValueError:
        raise InitDataError("bad_user") from None
    if not isinstance(user, dict):
        raise InitDataError("bad_user")

    user_id = as_int(user.get("id"))
    if user_id is None:
        raise InitDataError("bad_user")
    return user_id


def parse_integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def encode_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_base64url(text: str) -> bytes | None:
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
