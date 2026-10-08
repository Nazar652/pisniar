"""The Mini App settings: a bare https origin or nothing, an absolute static directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pydantic
import pytest

from pisniar.config import Settings


def build(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="0:fake",
        admin_ids="1",
        alert_chat_id=1,
        channel_id=-1,
        database_url="postgresql+asyncpg://u:p@postgres:5432/db",
        spotify_client_id="id",
        spotify_client_secret="secret",
        **overrides,
    )


def test_defaults_leave_the_mini_app_off_and_the_windows_at_their_documented_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBAPP_PUBLIC_URL", raising=False)
    settings = build()

    assert settings.webapp_public_url is None
    assert settings.webapp_port == 8081
    assert settings.webapp_static_dir == Path("/app/webapp-static")
    assert settings.webapp_auth_max_age_seconds == 600
    assert settings.webapp_auth_max_future_seconds == 60
    assert settings.webapp_session_ttl_seconds == 3600


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://pisniar.example.ts.net", "https://pisniar.example.ts.net"),
        (" https://pisniar.example.ts.net/ ", "https://pisniar.example.ts.net"),
        ("https://pisniar.example.ts.net/", "https://pisniar.example.ts.net"),
        ("https://host", "https://host"),
        ("", None),
        ("   ", None),
    ],
)
def test_public_url_is_normalised(raw: str, expected: str | None) -> None:
    assert build(webapp_public_url=raw).webapp_public_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://pisniar.example.ts.net",
        "pisniar.example.ts.net",
        "https://pisniar.example.ts.net/app",
        "https://pisniar.example.ts.net/?x=1",
        "https://pisniar.example.ts.net/#frag",
        "https://user:pass@pisniar.example.ts.net",
        "ftp://x",
        "https://pisniar.example.ts.netdocker compose up -d --force-recreate bot worker webapp",
        "https://pisniar.example.ts.net docker",
        "https://pisniar example.ts.net",
        "https://pisniar.example.ts.net:8443",
        "https://pisniar.example.ts.net:abc",
        "https://pisniar_x.example.ts.net",
        "https://-bad.example.ts.net",
        "https://exa mple",
        "https://[::1",
        "https://",
    ],
)
def test_public_url_that_is_not_a_bare_https_origin_is_refused(raw: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="webapp_public_url"):
        build(webapp_public_url=raw)


def test_a_relative_static_dir_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError, match="webapp_static_dir"):
        build(webapp_static_dir="webapp-static")


@pytest.mark.parametrize("field", ["webapp_auth_max_age_seconds", "webapp_session_ttl_seconds"])
def test_a_non_positive_window_is_refused(field: str) -> None:
    with pytest.raises(pydantic.ValidationError, match=field):
        build(**{field: 0})


@pytest.mark.parametrize("port", [0, 65536])
def test_an_impossible_port_is_refused(port: int) -> None:
    with pytest.raises(pydantic.ValidationError, match="webapp_port"):
        build(webapp_port=port)
