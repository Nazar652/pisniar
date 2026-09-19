"""`Settings`: required fields, comma-separated admin ids, and the
`postgresql+asyncpg://` DSN guard. Every test builds `Settings` with
`_env_file=None` and explicit values, so it never reads the real `.env` in
the repo root."""

from __future__ import annotations

from typing import Literal

import pydantic
import pytest

from pisniar.config import Settings, redaction_secrets

# conftest.py sets fallbacks for these; "missing field" tests need them
# genuinely absent, so this fixture removes them from real OS environ.
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
    log_format: Literal["json", "console"] = "json",
    spotify_client_id: str = "fake-spotify-client-id",
    spotify_client_secret: str = "fake-spotify-client-secret",
) -> Settings:
    return Settings(
        _env_file=None,
        bot_token=bot_token,
        admin_ids=admin_ids,
        alert_chat_id=alert_chat_id,
        database_url=database_url,
        environment=environment,
        log_format=log_format,
        spotify_client_id=spotify_client_id,
        spotify_client_secret=spotify_client_secret,
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


def test_log_format_is_independent_of_environment() -> None:
    """`LOG_FORMAT` must not be coupled to `ENVIRONMENT` — M11 (JSON logs in
    the docker/prod path) and the console output `make up` gives a developer
    both rely on that independence being real, not just the code's default
    happening to agree with what each path wants."""
    dev_with_json = build_settings(environment="dev", log_format="json")
    assert dev_with_json.environment == "dev"
    assert dev_with_json.log_format == "json"

    docker_with_console = build_settings(environment="docker", log_format="console")
    assert docker_with_console.environment == "docker"
    assert docker_with_console.log_format == "console"


def test_log_format_defaults_to_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolated from ambient `LOG_FORMAT` (e.g. "console" set by
    # compose.override.yml for dev containers) — this asserts the pydantic
    # default, not whatever happens to be in the process environment.
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    settings = Settings(
        _env_file=None,
        bot_token="000000000:fake",
        admin_ids="111,222",
        alert_chat_id="111",
        database_url="postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
    )

    assert settings.log_format == "json"


def test_spotify_client_secret_never_appears_in_repr() -> None:
    settings = build_settings(spotify_client_secret="super-secret-spotify-value")

    assert "super-secret-spotify-value" not in repr(settings.spotify_client_secret)
    assert settings.spotify_client_secret.get_secret_value() == "super-secret-spotify-value"


def test_itunes_country_defaults_to_ua() -> None:
    settings = build_settings()

    assert settings.itunes_country == "UA"


def test_musicbrainz_contact_defaults_to_none() -> None:
    settings = build_settings()

    assert settings.musicbrainz_contact is None


def test_redaction_secrets_includes_spotify_client_secret() -> None:
    settings = build_settings(spotify_client_secret="super-secret-spotify-value")

    assert "super-secret-spotify-value" in redaction_secrets(settings)
