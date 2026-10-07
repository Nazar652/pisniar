"""The two command-line tools of the Mini App: `dev_token` and `export_openapi`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import ADMIN_ID, BOT_TOKEN

from pisniar.config import Settings
from pisniar.webapp import dev_token, export_openapi
from pisniar.webapp.auth import verify_session_token


def test_dev_token_prints_a_verifiable_token_for_an_admin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dev_token, "get_settings", lambda: settings)
    monkeypatch.setattr(dev_token.time, "time", lambda: 1_800_000_000.0)

    exit_code = dev_token.main(["--user-id", str(ADMIN_ID)])

    captured = capsys.readouterr()
    assert exit_code == 0
    token = captured.out.strip()
    assert verify_session_token(token, BOT_TOKEN, 1_800_000_000) == ADMIN_ID
    assert captured.err == ""


def test_dev_token_refuses_an_id_outside_admin_ids(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dev_token, "get_settings", lambda: settings)

    exit_code = dev_token.main(["--user-id", "999"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert BOT_TOKEN not in captured.err


def test_dev_token_is_not_reachable_over_http(webapp_routes: list[str]) -> None:
    assert not any("dev" in path for path in webapp_routes)


def test_the_exported_schema_lists_the_api_and_hides_the_static_routes() -> None:
    schema = export_openapi.build_openapi_schema()

    assert set(schema["paths"]) == {
        "/api/session",
        "/api/me",
        "/api/posts",
        "/api/posts/{post_id}",
        "/api/meta",
    }
    assert {"ApiError", "SessionRequest", "SessionResponse", "MeResponse"} <= set(
        schema["components"]["schemas"]
    )


def test_the_exported_schema_is_stable_and_sorted() -> None:
    first = export_openapi.render_schema(export_openapi.build_openapi_schema())
    second = export_openapi.render_schema(export_openapi.build_openapi_schema())

    assert first == second
    assert first.endswith("\n")
    assert "\r" not in first
    assert list(json.loads(first)) == sorted(json.loads(first))


def test_export_writes_the_file_with_unix_newlines(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"

    assert export_openapi.main(["--output", str(target)]) == 0

    assert b"\r\n" not in target.read_bytes()
    assert json.loads(target.read_text(encoding="utf-8"))["info"]["title"] == "pisniar webapp"
