"""`validate_init_data` against generated signatures, and the session token: every refusal
reason is pinned, so a loosened check fails a named case.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from support import ADMIN_ID, BOT_TOKEN, NOW, make_init_data, sign_fields

from pisniar.webapp.auth import (
    SESSION_KEY_CONTEXT,
    AuthWindow,
    InitDataError,
    derived_key,
    issue_session_token,
    validate_init_data,
    verify_session_token,
)
from pisniar.webapp.schemas import INIT_DATA_MAX_LENGTH

WINDOW = AuthWindow(max_age_seconds=600, max_future_seconds=60)


def reason_of(init_data: str) -> str:
    with pytest.raises(InitDataError) as error:
        validate_init_data(init_data, BOT_TOKEN, NOW, WINDOW)
    return error.value.reason


def test_genuine_init_data_returns_the_user_id() -> None:
    assert validate_init_data(make_init_data(), BOT_TOKEN, NOW, WINDOW) == ADMIN_ID


def test_the_signature_field_is_part_of_the_hashed_string() -> None:
    # telegram hashes every field but `hash`, `signature` included.
    without = make_init_data(extra={"signature": "other-value"})
    assert validate_init_data(without, BOT_TOKEN, NOW, WINDOW) == ADMIN_ID


def test_a_wrong_hash_is_refused() -> None:
    tampered = make_init_data().replace("hash=", "hash=0")
    assert reason_of(tampered) == "bad_signature"


def test_a_substituted_field_is_refused() -> None:
    genuine = make_init_data(user_id=ADMIN_ID)
    forged = genuine.replace("%22id%22%3A+111", "%22id%22%3A+222")
    assert forged != genuine
    assert reason_of(forged) == "bad_signature"


def test_a_token_of_another_bot_is_refused() -> None:
    assert reason_of(make_init_data(bot_token="999:other")) == "bad_signature"


def test_a_missing_hash_is_refused() -> None:
    assert reason_of("auth_date=1&user=%7B%7D") == "missing_hash"


@pytest.mark.parametrize("auth_date", [NOW - 601, NOW + 61, 0])
def test_auth_date_outside_the_window_is_refused(auth_date: int) -> None:
    assert reason_of(make_init_data(auth_date=auth_date)) == "auth_date_out_of_window"


@pytest.mark.parametrize("auth_date", [NOW - 600, NOW + 60])
def test_the_window_edges_are_inclusive(auth_date: int) -> None:
    assert validate_init_data(make_init_data(auth_date=auth_date), BOT_TOKEN, NOW, WINDOW)


def test_a_non_numeric_auth_date_is_refused() -> None:
    fields = {"auth_date": "yesterday", "user": json.dumps({"id": ADMIN_ID})}
    assert reason_of(sign_fields(fields, BOT_TOKEN)) == "bad_auth_date"


def test_a_duplicated_key_is_refused_even_with_a_valid_signature() -> None:
    genuine = make_init_data()
    assert reason_of(genuine + "&query_id=AAH-second") == "duplicate_key"


def test_a_duplicated_hash_is_refused() -> None:
    genuine = make_init_data()
    hash_value = genuine.split("hash=")[1]
    assert reason_of(genuine + "&hash=" + hash_value) == "duplicate_key"


def test_a_missing_user_is_refused() -> None:
    fields = {"auth_date": str(NOW), "query_id": "x"}
    assert reason_of(sign_fields(fields, BOT_TOKEN)) == "missing_user"


@pytest.mark.parametrize("user_id", ["111", True, 1.5, None, [1]])
def test_a_user_id_that_is_not_an_integer_is_refused(user_id: object) -> None:
    assert reason_of(make_init_data(user_id=user_id)) == "bad_user"


@pytest.mark.parametrize("raw_user", ["not json", "[1]", "42", "null"])
def test_a_user_that_is_not_an_object_is_refused(raw_user: str) -> None:
    fields = {"auth_date": str(NOW), "user": raw_user}
    assert reason_of(sign_fields(fields, BOT_TOKEN)) == "bad_user"


def test_a_blank_value_is_refused() -> None:
    assert reason_of(make_init_data(extra={"chat_type": ""})) == "malformed"


@pytest.mark.parametrize("init_data", ["no-equals-sign", "&&"])
def test_a_malformed_query_string_is_refused(init_data: str) -> None:
    assert reason_of(init_data) == "malformed"


def test_an_empty_payload_is_refused() -> None:
    assert reason_of("") == "missing_hash"


def test_an_oversized_payload_is_refused_before_it_is_parsed() -> None:
    assert reason_of("a=" + "b" * INIT_DATA_MAX_LENGTH) == "too_long"


def test_the_admin_check_is_not_part_of_validation() -> None:
    assert validate_init_data(make_init_data(user_id=999), BOT_TOKEN, NOW, WINDOW) == 999


def test_a_session_token_round_trips() -> None:
    token = issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)
    assert verify_session_token(token, BOT_TOKEN, NOW + 10) == ADMIN_ID


def test_a_token_expires_exactly_at_its_ttl() -> None:
    token = issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)
    assert verify_session_token(token, BOT_TOKEN, NOW + 3599) == ADMIN_ID
    with pytest.raises(InitDataError, match="token_expired"):
        verify_session_token(token, BOT_TOKEN, NOW + 3600)


def test_a_token_signed_with_another_bot_token_is_refused() -> None:
    token = issue_session_token(ADMIN_ID, "999:other", NOW, 3600)
    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_session_token(token, BOT_TOKEN, NOW)


def test_a_token_with_a_swapped_payload_is_refused() -> None:
    token = issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)
    signature = token.split(".")[1]
    forged_payload = json.dumps({"uid": 7, "iat": NOW, "exp": NOW + 10**9}).encode()
    forged = base64.urlsafe_b64encode(forged_payload).rstrip(b"=").decode() + "." + signature
    with pytest.raises(InitDataError, match="token_bad_signature"):
        verify_session_token(forged, BOT_TOKEN, NOW)


def test_the_session_key_is_not_the_init_data_key() -> None:
    init_data_secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    session_key = derived_key(BOT_TOKEN, SESSION_KEY_CONTEXT)
    assert session_key != init_data_secret
    assert session_key != hashlib.sha256(BOT_TOKEN.encode()).digest()


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "!!!.???"])
def test_a_malformed_token_is_refused(token: str) -> None:
    with pytest.raises(InitDataError, match="token_malformed"):
        verify_session_token(token, BOT_TOKEN, NOW)


def signed_claims(claims: object) -> str:
    payload = json.dumps(claims).encode()
    signature = hmac.new(
        derived_key(BOT_TOKEN, SESSION_KEY_CONTEXT), payload, hashlib.sha256
    ).digest()
    encode = base64.urlsafe_b64encode
    return f"{encode(payload).rstrip(b'=').decode()}.{encode(signature).rstrip(b'=').decode()}"


@pytest.mark.parametrize(
    "claims",
    [
        [1, 2, 3],
        {"uid": "111", "iat": NOW, "exp": NOW + 60},
        {"uid": True, "iat": NOW, "exp": NOW + 60},
        {"uid": 1, "iat": NOW},
        {"uid": 1, "iat": NOW, "exp": "later"},
    ],
)
def test_correctly_signed_claims_of_the_wrong_shape_are_refused(claims: object) -> None:
    with pytest.raises(InitDataError, match="token_malformed"):
        verify_session_token(signed_claims(claims), BOT_TOKEN, NOW)


def test_a_token_issued_in_the_future_is_refused() -> None:
    token = signed_claims({"uid": 1, "iat": NOW + 3600, "exp": NOW + 7200})
    with pytest.raises(InitDataError, match="token_from_future"):
        verify_session_token(token, BOT_TOKEN, NOW)
