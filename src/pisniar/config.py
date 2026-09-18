"""Typed application configuration (pydantic-settings)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables / `.env`.

    All values that must never reach logs or the container image are typed
    as `SecretStr`; see `pisniar.logging` for the additional redaction
    processor that scrubs the raw secret value out of any log event too.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "docker"] = "dev"
    log_level: str = "INFO"

    bot_token: SecretStr
    # `NoDecode`: pydantic-settings otherwise JSON-decodes any "complex"
    # (list/dict) field read from an environment variable before validators
    # ever run, so a plain "111,222" from `.env` blows up as invalid JSON
    # before `split_admin_ids` below gets a chance to parse it.
    admin_ids: Annotated[list[int], NoDecode] = Field(min_length=1)
    alert_chat_id: int

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    http_host: str = "0.0.0.0"
    http_port: int = 8080

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
