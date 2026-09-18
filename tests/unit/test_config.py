"""`Settings`: required fields, comma-separated admin ids, and the
`postgresql+asyncpg://` DSN guard. Every test builds `Settings` with
`_env_file=None` and explicit values, so it never reads the real `.env` in
the repo root."""

from __future__ import annotations

from typing import Literal

import pydantic
import pytest

from pisniar.config import Settings

# `conftest.py` sets fallback values for these via `os.environ.setdefault()`
# so the rest of the app can be imported at all; the "missing field" tests
# below need to see them genuinely absent, so they remove them from the real
# OS environment for their duration — `_env_file=None` alone only disables
# the dotenv source, not the environment source.
REQUIRED_ENV_VARS = ("BOT_TOKEN", "ADMIN_IDS", "ALERT_CHAT_ID", "DATABASE_URL")


@pytest.fixture
def no_ambient_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def build_settings(
    *,
    bot_token: str = "000000000:fake",
    admin_ids: str = "111,222",
    alert_chat_id: str = "111",
    database_url: str = "postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
    environment: Literal["dev", "docker"] = "dev",
) -> Settings:
    return Settings(
        _env_file=None,
        bot_token=bot_token,
        admin_ids=admin_ids,
        alert_chat_id=alert_chat_id,
        database_url=database_url,
        environment=environment,
    )


def test_missing_bot_token_raises(no_ambient_settings_env: None) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        Settings(
            _env_file=None,
            admin_ids="111,222",
            alert_chat_id="111",
            database_url="postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
        )
    assert "bot_token" in str(excinfo.value)


def test_missing_admin_ids_raises(no_ambient_settings_env: None) -> None:
    with pytest.raises(pydantic.ValidationError):
        Settings(
            _env_file=None,
            bot_token="000000000:fake",
            alert_chat_id="111",
            database_url="postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
        )


def test_admin_ids_comma_separated_string_parses_to_int_list() -> None:
    settings = build_settings(admin_ids="111111111,222222222")

    assert settings.admin_ids == [111111111, 222222222]


def test_bot_token_never_appears_in_repr() -> None:
    settings = build_settings(bot_token="000000000:super-secret-value")

    assert "super-secret-value" not in repr(settings.bot_token)
    assert settings.bot_token.get_secret_value() == "000000000:super-secret-value"


def test_database_url_requires_asyncpg_driver() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        build_settings(database_url="postgresql://pisniar:pisniar@postgres:5432/pisniar")
    assert "asyncpg" in str(excinfo.value)


def test_environment_defaults_to_dev() -> None:
    settings = build_settings()

    assert settings.environment == "dev"
    assert settings.is_docker is False


def test_environment_docker_sets_is_docker() -> None:
    settings = build_settings(environment="docker")

    assert settings.is_docker is True
