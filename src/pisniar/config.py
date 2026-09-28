"""Typed application configuration (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Values that must never reach logs are `SecretStr`; `pisniar.logging` redacts them."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "docker"] = "dev"
    # Independent of `environment` on purpose: "docker" doesn't always mean
    # "read JSON logs" — `make up` still wants console output while `environment` stays "dev".
    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"

    bot_token: SecretStr
    # `NoDecode`: without it, pydantic-settings JSON-decodes "111,222" before
    # `split_admin_ids` below ever runs, and that blows up as invalid JSON.
    admin_ids: Annotated[list[int], NoDecode] = Field(min_length=1)
    alert_chat_id: int
    # The publish target, and the only source of it. Numeric, not
    # `@username`: a username can be reassigned to another chat, an id cannot.
    channel_id: int

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    http_host: str = "0.0.0.0"
    http_port: int = 8080

    spotify_client_id: str
    spotify_client_secret: SecretStr
    # Optional: the `musicbrainz` source stays disabled without a contact,
    # per MusicBrainz's own policy.
    musicbrainz_contact: str | None = None
    itunes_country: str = "UA"
    # Both a hit and a `{"found": false}` go stale after an hour, so `/retry`
    # can recover a release song.link has not indexed yet.
    source_fetch_ttl_seconds: int = 3600

    # Mount point of the `media-data` volume inside the worker container, and
    # the only root `worker/` ever builds a media path under.
    media_root: Path = Path("/data")
    # Checked with `shutil.disk_usage()` *before* a download starts: running
    # out mid-render leaves a file that looks finished.
    media_min_free_bytes: int = Field(default=2_000_000_000, gt=0)
    # Per-task limits. The resolve limits in `worker.celery_app` also gate
    # `/retry`'s "resolve has gone stale" window, so they are never raised here.
    download_task_soft_time_limit_seconds: int = Field(default=120, gt=0)
    download_task_time_limit_seconds: int = Field(default=180, gt=0)
    render_task_soft_time_limit_seconds: int = Field(default=180, gt=0)
    render_task_time_limit_seconds: int = Field(default=240, gt=0)

    @field_validator("admin_ids", mode="before")
    @classmethod
    def split_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def require_asyncpg_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// driver")
        return value

    @field_validator("media_root")
    @classmethod
    def require_absolute_media_root(cls, value: Path) -> Path:
        """A relative root would resolve against the worker's `/app` cwd —
        media would land inside the image instead of on the volume."""
        if not value.as_posix().startswith("/"):
            raise ValueError("media_root must be an absolute path inside the container")
        return value

    @model_validator(mode="after")
    def require_soft_limit_below_hard_limit(self) -> Settings:
        """A soft limit at or above the hard one never fires, and the task is
        SIGKILLed with no chance to clean up its `.partial` files."""
        limit_pairs = (
            (
                "download",
                self.download_task_soft_time_limit_seconds,
                self.download_task_time_limit_seconds,
            ),
            (
                "render",
                self.render_task_soft_time_limit_seconds,
                self.render_task_time_limit_seconds,
            ),
        )
        for task_name, soft_limit, hard_limit in limit_pairs:
            if soft_limit >= hard_limit:
                raise ValueError(
                    f"{task_name}_task_soft_time_limit_seconds must be below "
                    f"{task_name}_task_time_limit_seconds"
                )
        return self

    @property
    def is_docker(self) -> bool:
        return self.environment == "docker"


def get_settings() -> Settings:
    """Not cached: tests build fresh settings per case via `monkeypatch.setenv`."""
    return Settings()


def redaction_secrets(settings: Settings) -> list[str]:
    """Tracebacks count too: `/readyz` logs with `exc_info=True`, and asyncpg or
    kombu connection errors can embed the DSN password in their message."""
    secrets = [
        settings.bot_token.get_secret_value(),
        settings.spotify_client_secret.get_secret_value(),
    ]
    password = make_url(settings.database_url).password
    if password:
        secrets.append(password)

    secrets.extend(_redis_password_forms(settings.redis_url))
    return secrets


def _redis_password_forms(redis_url: str) -> list[str]:
    """Both forms: a leaked URL carries the percent-encoded password, while
    redis-py/kombu unquote it before it reaches a client-side message."""
    quoted_password = urlsplit(redis_url).password
    if not quoted_password:
        return []

    unquoted_password = unquote(quoted_password)
    if unquoted_password == quoted_password:
        return [quoted_password]
    return [quoted_password, unquoted_password]
