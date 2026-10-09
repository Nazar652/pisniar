"""The signed claims and the audio token: a round trip, every refusal reason, and the two keys
that keep a session token and an audio token from standing in for each other.
"""

from __future__ import annotations

import base64
import json

import pytest
from support import ADMIN_ID, BOT_TOKEN, NOW

from pisniar.webapp.auth import (
    MEDIA_KEY_CONTEXT,
    SESSION_KEY_CONTEXT,
    InitDataError,
    MediaClaims,
    check_lifetime,
    derived_key,
    issue_media_token,
    issue_session_token,
    sign_claims,
    verify_media_token,
    verify_session_token,
    verify_signed_claims,
)

VIDEO_ID = "n4bPzdIs_nc"
CLAIMS = MediaClaims(user_id=ADMIN_ID, post_id=7, track_id=3, video_id=VIDEO_ID)
TTL = 900


def signed_media(claims: dict[str, object]) -> str:
    return sign_claims(claims, BOT_TOKEN, MEDIA_KEY_CONTEXT)


def valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "uid": ADMIN_ID,
        "pid": 7,
        "tid": 3,
        "vid": VIDEO_ID,
        "iat": NOW,
        "exp": NOW + TTL,
    }
    return {**payload, **changes}


def test_the_two_key_contexts_derive_different_keys() -> None:
    assert derived_key(BOT_TOKEN, SESSION_KEY_CONTEXT) != derived_key(BOT_TOKEN, MEDIA_KEY_CONTEXT)
    assert MEDIA_KEY_CONTEXT == b"pisniar-webapp-media-v1"


def test_signed_claims_round_trip_in_their_own_context() -> None:
    token = sign_claims({"a": 1}, BOT_TOKEN, MEDIA_KEY_CONTEXT)

    assert verify_signed_claims(token, BOT_TOKEN, MEDIA_KEY_CONTEXT) == {"a": 1}


def test_signed_claims_do_not_verify_in_another_context() -> None:
    token = sign_claims({"a": 1}, BOT_TOKEN, MEDIA_KEY_CONTEXT)

    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_signed_claims(token, BOT_TOKEN, SESSION_KEY_CONTEXT)


def test_check_lifetime_accepts_a_live_token_and_names_each_refusal() -> None:
    check_lifetime({"iat": NOW, "exp": NOW + 1}, NOW)

    with pytest.raises(InitDataError, match="token_expired"):
        check_lifetime({"iat": NOW - 10, "exp": NOW}, NOW)
    with pytest.raises(InitDataError, match="token_from_future"):
        check_lifetime({"iat": NOW + 3600, "exp": NOW + 7200}, NOW)
    with pytest.raises(InitDataError, match="token_malformed"):
        check_lifetime({"iat": NOW}, NOW)


def test_a_media_token_returns_the_scope_it_was_issued_for() -> None:
    token = issue_media_token(CLAIMS, BOT_TOKEN, NOW, TTL)

    assert verify_media_token(token, BOT_TOKEN, NOW) == CLAIMS
    assert verify_media_token(token, BOT_TOKEN, NOW + TTL - 1) == CLAIMS


def test_a_media_token_carries_exactly_the_documented_claims() -> None:
    token = issue_media_token(CLAIMS, BOT_TOKEN, NOW, TTL)

    claims = verify_signed_claims(token, BOT_TOKEN, MEDIA_KEY_CONTEXT)

    assert claims == valid_payload()
    assert len(token) < 512


def test_an_expired_media_token_is_refused() -> None:
    token = issue_media_token(CLAIMS, BOT_TOKEN, NOW, TTL)

    with pytest.raises(InitDataError, match="token_expired"):
        verify_media_token(token, BOT_TOKEN, NOW + TTL)


def test_a_media_token_signed_with_another_bot_token_is_refused() -> None:
    token = issue_media_token(CLAIMS, "999:other", NOW, TTL)

    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_media_token(token, BOT_TOKEN, NOW)


def test_a_session_token_is_not_accepted_as_a_media_token() -> None:
    token = issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)

    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_media_token(token, BOT_TOKEN, NOW)


def test_a_media_token_is_not_accepted_as_a_session_token() -> None:
    token = issue_media_token(CLAIMS, BOT_TOKEN, NOW, TTL)

    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_session_token(token, BOT_TOKEN, NOW)


@pytest.mark.parametrize(
    "video_id", ["../../etc/passwd", "short", "n4bPzdIs_n;", 12345678901, None]
)
def test_a_correctly_signed_token_with_an_invalid_video_id_is_refused(video_id: object) -> None:
    token = signed_media(valid_payload(vid=video_id))

    with pytest.raises(InitDataError, match="token_bad_video_id"):
        verify_media_token(token, BOT_TOKEN, NOW)


@pytest.mark.parametrize("missing", ["uid", "pid", "tid"])
def test_a_media_token_without_an_id_is_malformed(missing: str) -> None:
    payload = valid_payload()
    del payload[missing]

    with pytest.raises(InitDataError, match="token_malformed"):
        verify_media_token(signed_media(payload), BOT_TOKEN, NOW)


@pytest.mark.parametrize("field", ["uid", "pid", "tid"])
@pytest.mark.parametrize("value", ["7", True, 1.5])
def test_a_media_token_with_a_non_integer_id_is_malformed(field: str, value: object) -> None:
    with pytest.raises(InitDataError, match="token_malformed"):
        verify_media_token(signed_media(valid_payload(**{field: value})), BOT_TOKEN, NOW)


def test_a_token_with_a_swapped_payload_is_refused() -> None:
    token = issue_media_token(CLAIMS, BOT_TOKEN, NOW, TTL)
    signature = token.split(".")[1]
    forged_payload = json.dumps(valid_payload(pid=8)).encode()
    forged = base64.urlsafe_b64encode(forged_payload).rstrip(b"=").decode() + "." + signature

    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_media_token(forged, BOT_TOKEN, NOW)
