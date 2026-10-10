"""The Mini App server end to end over `TestClient`: every route is classified, every
non-public route refuses an anonymous, expired or non-admin caller, and the headers and
the CSP are pinned verbatim.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import structlog.testing
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support import ADMIN_ID, BOT_TOKEN, NOW, make_init_data

from pisniar.config import Settings
from pisniar.webapp.app import (
    CONTENT_SECURITY_POLICY,
    IMMUTABLE_CACHE,
    MAX_BODY_BYTES,
    create_webapp,
)
from pisniar.webapp.auth import issue_session_token, require_admin

PUBLIC_ROUTES = {
    ("GET", "/"),
    ("GET", "/assets/{asset_path:path}"),
    ("POST", "/api/session"),
}
SIGNED_ROUTES = {("GET", "/api/posts/{post_id}/audio")}
EXPECTED_CSP = (
    "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self' 'sha256-Y/8t1dAKa3ay+RZEiEFEDr8dFiQhpaFvYXXUfT3p6ZU='; "
    "img-src 'self'; media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org"
)


def route_table(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    table = []
    for route in app.routes:
        assert isinstance(route, APIRoute), f"{route!r} is not an APIRoute"
        for method in sorted((route.methods or set()) - {"HEAD"}):
            table.append((method, route.path, route))
    return table


def protected_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    return [entry for entry in route_table(app) if entry[:2] not in PUBLIC_ROUTES | SIGNED_ROUTES]


def protected_method_paths(app: FastAPI) -> list[tuple[str, str]]:
    return [(method, path) for method, path, route in protected_routes(app)]


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def bearer(user_id: int, now: int = NOW, ttl: int = 3600, token: str = BOT_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_session_token(user_id, token, now, ttl)}"}


def test_the_public_routes_are_exactly_the_three_known_ones(webapp: FastAPI) -> None:
    app = webapp
    public = {entry[:2] for entry in route_table(app)} & PUBLIC_ROUTES
    assert public == PUBLIC_ROUTES


def test_every_route_declares_a_response_model_unless_it_is_a_static_one(webapp: FastAPI) -> None:
    app = webapp
    for method, path, route in route_table(app):
        if (method, path) in (PUBLIC_ROUTES - {("POST", "/api/session")}) | SIGNED_ROUTES:
            continue
        assert route.response_model is not None, f"{method} {path} has no response_model"


def test_exactly_one_route_is_signed_and_it_is_hidden_from_the_schema(webapp: FastAPI) -> None:
    unguarded = {
        (method, path)
        for method, path, route in route_table(webapp)
        if require_admin not in [dependency.call for dependency in route.dependant.dependencies]
    }

    assert unguarded == PUBLIC_ROUTES | SIGNED_ROUTES
    signed = [
        route for method, path, route in route_table(webapp) if (method, path) in SIGNED_ROUTES
    ]
    assert [route.include_in_schema for route in signed] == [False]


def test_every_protected_route_depends_on_require_admin(webapp: FastAPI) -> None:
    app = webapp
    assert protected_routes(app), "the table has nothing to protect: the test proves nothing"
    for method, path, route in protected_routes(app):
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert require_admin in calls, f"{method} {path} is not behind require_admin"


def test_every_protected_route_answers_401_without_a_valid_token(
    client: TestClient, webapp: FastAPI
) -> None:
    expired = bearer(ADMIN_ID, now=NOW - 7200)
    forged = bearer(ADMIN_ID, token="999:other")
    cases: list[dict[str, str]] = [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer not-a-token"},
        expired,
        forged,
    ]
    for method, path in protected_method_paths(webapp):
        for headers in cases:
            response = client.request(method, concrete(path), headers=headers)
            assert response.status_code == 401, (method, path, headers)
            assert response.json()["code"] == "unauthorized"


def test_every_protected_route_answers_403_to_a_valid_non_admin(
    client: TestClient, webapp: FastAPI
) -> None:
    for method, path in protected_method_paths(webapp):
        response = client.request(method, concrete(path), headers=bearer(999))
        assert response.status_code == 403, (method, path)
        assert response.json()["code"] == "forbidden"


def test_an_admin_removed_after_the_token_was_issued_is_refused_at_once(
    settings: Settings, clock: Callable[[], float]
) -> None:
    # the token is valid; only the admin list differs, and it is read on every request.
    narrowed = settings.model_copy(update={"admin_ids": [222]})
    narrowed_client = TestClient(create_webapp(narrowed, clock=clock))
    assert narrowed_client.get("/api/me", headers=bearer(ADMIN_ID)).status_code == 403


def test_an_admin_reads_their_own_identity(client: TestClient) -> None:
    response = client.get("/api/me", headers=bearer(ADMIN_ID))
    assert response.status_code == 200
    assert response.json() == {"user_id": ADMIN_ID}


def test_a_session_is_issued_for_genuine_init_data_of_an_admin(client: TestClient) -> None:
    response = client.post("/api/session", json={"init_data": make_init_data()})

    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == 3600
    assert client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"}).json() == {
        "user_id": ADMIN_ID
    }


def test_a_session_is_refused_with_403_for_genuine_init_data_of_a_stranger(
    client: TestClient,
) -> None:
    response = client.post("/api/session", json={"init_data": make_init_data(user_id=999)})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.parametrize(
    "init_data",
    [
        make_init_data(bot_token="999:other"),
        make_init_data(auth_date=NOW - 10_000),
        make_init_data(user_id="111"),
        "garbage",
    ],
)
def test_a_session_is_refused_with_401_for_bad_init_data(
    client: TestClient, init_data: str
) -> None:
    response = client.post("/api/session", json={"init_data": init_data})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_init_data"


def test_a_refused_session_logs_a_reason_and_no_init_data(client: TestClient) -> None:
    init_data = make_init_data(bot_token="999:other")
    with structlog.testing.capture_logs() as captured:
        client.post("/api/session", json={"init_data": init_data})

    rejections = [entry for entry in captured if entry["event"] == "webapp_auth_rejected"]
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "bad_signature"
    assert init_data not in json.dumps(captured, default=str)


def test_a_malformed_body_is_422_and_never_echoes_the_input(client: TestClient) -> None:
    secret_looking = "SECRET-INIT-DATA-VALUE"
    response = client.post("/api/session", json={"init_data": secret_looking, "extra": 1})

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "Некоректний запит."}
    assert secret_looking not in response.text


def test_the_validation_error_of_a_protected_route_comes_after_authorization(
    client: TestClient,
) -> None:
    response = client.post("/api/session", content=b"not json", headers={"Content-Length": "8"})
    assert response.status_code == 422


def test_a_body_without_a_content_length_is_refused(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"init_data": "x"}'

    response = client.post("/api/session", content=chunks())
    assert response.status_code == 411


def test_an_oversized_body_is_refused_with_413(client: TestClient) -> None:
    response = client.post("/api/session", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.json()["code"] == "body_too_large"


def test_an_unknown_path_is_a_json_404_with_headers(client: TestClient) -> None:
    response = client.get("/api/nothing-here")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Не знайдено."}
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP


def test_the_csp_constant_is_the_contract_text() -> None:
    assert CONTENT_SECURITY_POLICY == EXPECTED_CSP
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY


def test_index_is_503_while_the_frontend_is_not_built(
    client_for: Callable[..., TestClient], tmp_path: Path
) -> None:
    response = client_for(tmp_path).get("/")
    assert response.status_code == 503
    assert response.json()["code"] == "frontend_missing"
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP


def test_index_is_served_with_the_headers_pinned(
    client_for: Callable[..., TestClient], tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")

    response = client_for(tmp_path).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "X-Frame-Options" not in response.headers


def test_an_asset_is_served_immutable(
    client_for: Callable[..., TestClient], tmp_path: Path
) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abc123.js").write_text("export {}", encoding="utf-8")

    response = client_for(tmp_path).get("/assets/app-abc123.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == IMMUTABLE_CACHE
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP


@pytest.mark.parametrize(
    "path",
    ["/assets/missing.js", "/assets/../secret.txt", "/assets/%2e%2e/secret.txt", "/assets/%00"],
)
def test_an_asset_path_cannot_leave_the_assets_directory(
    client_for: Callable[..., TestClient], tmp_path: Path, path: str
) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "secret.txt").write_text("hidden", encoding="utf-8")

    response = client_for(tmp_path).get(path)

    assert response.status_code == 404
    assert "hidden" not in response.text


def test_the_api_responses_are_not_cacheable(client: TestClient) -> None:
    response = client.get("/api/me", headers=bearer(ADMIN_ID))
    assert response.headers["Cache-Control"] == "no-store"
