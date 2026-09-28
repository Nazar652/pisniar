"""`Settings`: required fields, comma-separated admin ids, the
`postgresql+asyncpg://` DSN guard and the media/task-limit settings. Every test
builds `Settings` with `_env_file=None` and explicit values, so it never reads
the real `.env` in the repo root.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

import pydantic
import pytest

from pisniar.config import Settings, redaction_secrets
from pisniar.logging import REDACTED_PLACEHOLDER, configure_logging
from pisniar.worker.celery_app import TASK_SOFT_TIME_LIMIT_SECONDS, TASK_TIME_LIMIT_SECONDS

# conftest.py sets fallbacks for these; "missing field" tests need them
# genuinely absent, so this fixture removes them from real OS environ.
REQUIRED_ENV_VARS = ("BOT_TOKEN", "ADMIN_IDS", "ALERT_CHAT_ID", "CHANNEL_ID", "DATABASE_URL")


@pytest.fixture
def no_ambient_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def build_settings(
    *,
    bot_token: str = "000000000:fake",
    admin_ids: str = "111,222",
    alert_chat_id: str = "111",
    channel_id: str = "-1001234567890",
    database_url: str = "postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
    redis_url: str = "redis://redis:6379/0",
    environment: Literal["dev", "docker"] = "dev",
    log_format: Literal["json", "console"] = "json",
    spotify_client_id: str = "fake-spotify-client-id",
    spotify_client_secret: str = "fake-spotify-client-secret",
    **overrides: Any,
) -> Settings:
    return Settings(
        _env_file=None,
        bot_token=bot_token,
        admin_ids=admin_ids,
        alert_chat_id=alert_chat_id,
        channel_id=channel_id,
        database_url=database_url,
        redis_url=redis_url,
        environment=environment,
        log_format=log_format,
        spotify_client_id=spotify_client_id,
        spotify_client_secret=spotify_client_secret,
        **overrides,
    )


def test_missing_bot_token_raises(no_ambient_settings_env: None) -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        Settings(
            _env_file=None,
            admin_ids="111,222",
            alert_chat_id="111",
            channel_id="-1001234567890",
            database_url="postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
        )
    assert "bot_token" in str(excinfo.value)


def test_missing_admin_ids_raises(no_ambient_settings_env: None) -> None:
    with pytest.raises(pydantic.ValidationError):
        Settings(
            _env_file=None,
            bot_token="000000000:fake",
            alert_chat_id="111",
            channel_id="-1001234567890",
            database_url="postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
        )


def test_missing_channel_id_raises(no_ambient_settings_env: None) -> None:
    """No default and no fallback: a guessed publish target would mean a post
    in the wrong channel, and that is not recoverable."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        Settings(
            _env_file=None,
            bot_token="000000000:fake",
            admin_ids="111,222",
            alert_chat_id="111",
            database_url="postgresql+asyncpg://pisniar:pisniar@postgres:5432/pisniar",
        )
    assert "channel_id" in str(excinfo.value)


def test_channel_id_parses_as_a_negative_chat_id() -> None:
    settings = build_settings(channel_id="-1001234567890")

    assert settings.channel_id == -1001234567890


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
    """JSON logs in Docker and the console output `make up` gives both rely on
    `LOG_FORMAT` being truly independent of `ENVIRONMENT`."""
    dev_with_json = build_settings(environment="dev", log_format="json")
    assert dev_with_json.environment == "dev"
    assert dev_with_json.log_format == "json"

    docker_with_console = build_settings(environment="docker", log_format="console")
    assert docker_with_console.environment == "docker"
    assert docker_with_console.log_format == "console"


def test_log_format_defaults_to_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolated from an ambient `LOG_FORMAT` (compose.override.yml sets "console"):
    # this asserts the pydantic default.
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    settings = Settings(
        _env_file=None,
        bot_token="000000000:fake",
        admin_ids="111,222",
        alert_chat_id="111",
        channel_id="-1001234567890",
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


def test_media_root_defaults_to_the_mounted_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """`compose.yaml` mounts the named `media-data` volume at `/data` in the
    worker; a different default here would put the files back in the image."""
    monkeypatch.delenv("MEDIA_ROOT", raising=False)

    assert build_settings().media_root == Path("/data")


def test_media_root_must_be_absolute() -> None:
    """A relative root resolves against the worker's `/app` cwd — media would
    land inside the container's filesystem and disappear on the next build."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        build_settings(media_root="data/media")

    assert "absolute" in str(excinfo.value)


def test_media_min_free_bytes_must_be_positive() -> None:
    """Zero would make the pre-download space check pass on a full volume,
    which is the whole failure it exists to catch."""
    with pytest.raises(pydantic.ValidationError):
        build_settings(media_min_free_bytes=0)


def test_a_download_soft_limit_at_or_above_the_hard_limit_is_refused() -> None:
    """A soft limit that never fires means SIGKILL with no chance to remove
    the `.partial` file the task was writing."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        build_settings(
            download_task_soft_time_limit_seconds=300,
            download_task_time_limit_seconds=300,
        )

    assert "download" in str(excinfo.value)


def test_a_render_soft_limit_at_or_above_the_hard_limit_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        build_settings(
            render_task_soft_time_limit_seconds=420,
            render_task_time_limit_seconds=420,
        )

    assert "render" in str(excinfo.value)


def test_media_task_limits_are_separate_from_the_resolve_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TASK_TIME_LIMIT_SECONDS` is also how `/retry` decides a resolve has gone
    stale, so raising a render limit must never reach it."""
    for name in (
        "DOWNLOAD_TASK_SOFT_TIME_LIMIT_SECONDS",
        "DOWNLOAD_TASK_TIME_LIMIT_SECONDS",
        "RENDER_TASK_SOFT_TIME_LIMIT_SECONDS",
        "RENDER_TASK_TIME_LIMIT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = build_settings()

    assert (TASK_SOFT_TIME_LIMIT_SECONDS, TASK_TIME_LIMIT_SECONDS) == (300, 360)
    assert settings.download_task_time_limit_seconds == 180
    assert settings.render_task_soft_time_limit_seconds == 360
    assert settings.render_task_time_limit_seconds == 420


def test_redaction_secrets_includes_spotify_client_secret() -> None:
    settings = build_settings(spotify_client_secret="super-secret-spotify-value")

    assert "super-secret-spotify-value" in redaction_secrets(settings)


def test_redaction_secrets_includes_the_redis_password() -> None:
    """Without it the broker password leaks through kombu's traceback and through
    `readyz_redis_unavailable`, which logs with `exc_info=True`."""
    settings = build_settings(redis_url="redis://:super-secret-redis-value@redis:6379/0")

    assert "super-secret-redis-value" in redaction_secrets(settings)


def test_redaction_secrets_covers_a_percent_encoded_redis_password() -> None:
    """A leaked URL carries the encoded form; redis-py unquotes it before it
    reaches a client-side error message. Both can surface, so both go in."""
    settings = build_settings(redis_url="redis://:pa%40ss%2Fword@redis:6379/0")

    secrets = redaction_secrets(settings)

    assert "pa%40ss%2Fword" in secrets
    assert "pa@ss/word" in secrets


def test_redaction_secrets_has_nothing_to_add_for_a_passwordless_redis_url() -> None:
    settings = build_settings(redis_url="redis://redis:6379/0")

    assert "" not in redaction_secrets(settings)
    assert redaction_secrets(settings) == redaction_secrets(build_settings())


def test_the_redis_password_is_redacted_from_a_message_and_a_traceback(
    capsys: pytest.CaptureFixture[str],
    preserve_root_logging_state: None,
) -> None:
    """An event field and a rendered traceback: `format_exc_info` runs before the
    redaction processor, so the traceback is already a plain string."""
    settings = build_settings(redis_url="redis://:super-secret-redis-value@redis:6379/0")
    configure_logging(log_format="json", log_level="INFO", secrets=redaction_secrets(settings))

    try:
        raise ConnectionError("AUTH failed for redis://:super-secret-redis-value@redis:6379/0")
    except ConnectionError:
        logging.getLogger("pisniar.api.app").warning(
            "readyz_redis_unavailable for %s", settings.redis_url, exc_info=True
        )

    output = capsys.readouterr().out
    assert "super-secret-redis-value" not in output
    parsed = json.loads(output)
    assert REDACTED_PLACEHOLDER in parsed["event"]
    assert REDACTED_PLACEHOLDER in parsed["exception"]
