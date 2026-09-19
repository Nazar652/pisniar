"""Typed application configuration (pydantic-settings)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Configuration loaded from environment variables / `.env`.

    Values that must never reach logs are typed `SecretStr`; see
    `pisniar.logging` for the redaction processor that scrubs them from log events too.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "docker"] = "dev"
    # Independent of `environment` on purpose (D-23): "docker" doesn't always mean
    # "read JSON logs" — `make up` still wants console output while `environment` stays "dev".
    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"

    bot_token: SecretStr
    # `NoDecode`: without it, pydantic-settings JSON-decodes "111,222" before
    # `split_admin_ids` below ever runs, and that blows up as invalid JSON (D-23).
    admin_ids: Annotated[list[int], NoDecode] = Field(min_length=1)
    alert_chat_id: int

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    http_host: str = "0.0.0.0"
    http_port: int = 8080

    spotify_client_id: str
    spotify_client_secret: SecretStr
    # Absent until the user adds it (D-30/gate 2.0.4): the `musicbrainz`
    # source stays disabled without a contact, per MusicBrainz's own policy.
    musicbrainz_contact: str | None = None
    itunes_country: str = "UA"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def split_admin_ids(cls, value: object) -> object:
        """Allow a comma-separated string in `.env` (nicer than raw JSON)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def require_asyncpg_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// driver")
        return value

    @property
    def is_docker(self) -> bool:
        return self.environment == "docker"


def get_settings() -> Settings:
    """Build `Settings` from the environment.

    Not cached on purpose: tests construct fresh settings per case via
    `monkeypatch.setenv`, and process startup only calls this once anyway.
    """
    return Settings()


def redaction_secrets(settings: Settings) -> list[str]:
    """Every raw value that must never appear in a log line — including a
    traceback. `/readyz`'s Postgres check logs with `exc_info=True`, and an
    asyncpg connection error can embed the DSN (password included) in its
    message, so the bot token alone is not enough here.
    """
    secrets = [
        settings.bot_token.get_secret_value(),
        settings.spotify_client_secret.get_secret_value(),
    ]
    password = make_url(settings.database_url).password
    if password:
        secrets.append(password)
    return secrets
