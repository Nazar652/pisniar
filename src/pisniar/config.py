"""Typed application configuration (pydantic-settings)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

WEBAPP_ORIGIN_ERROR = (
    "webapp_public_url must be a bare https://host origin: a DNS host name, no port, "
    "credentials, path, query or fragment"
)
DNS_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
DNS_HOST_PATTERN = re.compile(rf"^{DNS_LABEL}(\.{DNS_LABEL})*$")
HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def bare_https_origin(value: str) -> str:
    """the same text Telegram would accept as a `web_app` origin, or a `ValueError` that never echoes the value; the
    URL parser rejects spaces and stray characters in the host."""
    try:
        parts = urlsplit(value)
        parsed = HTTP_URL_ADAPTER.validate_python(value)
        has_port = parts.port is not None
    except (ValidationError, ValueError):
        raise ValueError(WEBAPP_ORIGIN_ERROR) from None

    has_extras = parts.path or parts.query or parts.fragment or parts.username or parts.password
    host = parsed.host or ""
    if parts.scheme != "https" or has_extras or has_port or not DNS_HOST_PATTERN.match(host):
        raise ValueError(WEBAPP_ORIGIN_ERROR)
    return value


class Settings(BaseSettings):
    """values that must never reach logs are `SecretStr`; `pisniar.logging` redacts them."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "docker"] = "dev"
    # independent of `environment` on purpose: "docker" doesn't always mean "read JSON logs" — `make up` still wants
    # console output while `environment` stays "dev".
    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"

    bot_token: SecretStr
    # `NoDecode`: without it, pydantic-settings JSON-decodes "111,222" before `split_admin_ids` below ever runs, and
    # that blows up as invalid JSON.
    admin_ids: Annotated[list[int], NoDecode] = Field(min_length=1)
    alert_chat_id: int
    # the publish target, and the only source of it. Numeric, not `@username`: a username can be reassigned to another
    # chat, an id cannot.
    channel_id: int

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    http_host: str = "0.0.0.0"
    http_port: int = 8080

    spotify_client_id: str
    spotify_client_secret: SecretStr
    # optional: the `musicbrainz` source stays disabled without a contact, per MusicBrainz's own policy.
    musicbrainz_contact: str | None = None
    # optional: sources refuse data-centre IPs, and httpx, requests and yt-dlp read this variable from the environment
    # themselves, while Telegram's aiohttp ignores it. It is a field only so that redaction knows the value.
    https_proxy: SecretStr | None = None
    itunes_country: str = "UA"
    # both a hit and a `{"found": false}` go stale after an hour, so `/retry` can recover a release song.link has not
    # indexed yet.
    source_fetch_ttl_seconds: int = 3600

    # mount point of the `media-data` volume inside the worker container, and the only root `worker/` ever builds a
    # media path under.
    media_root: Path = Path("/data")
    # checked with `shutil.disk_usage()` *before* a download starts: running out mid-render leaves a file that looks
    # finished.
    media_min_free_bytes: int = Field(default=2_000_000_000, gt=0)
    # per-task limits. The resolve limits in `worker.celery_app` also gate `/retry`'s "resolve has gone stale" window,
    # so they are never raised here.
    download_task_soft_time_limit_seconds: int = Field(default=120, gt=0)
    download_task_time_limit_seconds: int = Field(default=180, gt=0)
    # covers LRCLIB's worst case (~187 s of timeouts and retries) on top of a 120 s turntable render: a slow lyrics
    # source must end as a video without text.
    render_task_soft_time_limit_seconds: int = Field(default=600, gt=0)
    render_task_time_limit_seconds: int = Field(default=660, gt=0)
    # one search and one page from a plain-text source, each with a 30 s timeout.
    plain_lyrics_task_soft_time_limit_seconds: int = Field(default=60, gt=0)
    plain_lyrics_task_time_limit_seconds: int = Field(default=90, gt=0)

    # unset disables the Mini App entry points; `https` only, since Telegram refuses plain http.
    webapp_public_url: str | None = None
    webapp_port: int = Field(default=8081, gt=0, lt=65536)
    # absolute, like `media_root`: a relative path would resolve against the container's cwd.
    webapp_static_dir: Path = Path("/app/webapp-static")
    # `initData` older than this, or dated further ahead than the future window, is refused.
    webapp_auth_max_age_seconds: int = Field(default=600, gt=0)
    webapp_auth_max_future_seconds: int = Field(default=60, ge=0)
    webapp_session_ttl_seconds: int = Field(default=3600, gt=0)
    # how long the signed audio URL of the fragment editor stays valid.
    webapp_media_url_ttl_seconds: int = Field(default=900, gt=0)

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

    @field_validator("media_root", "webapp_static_dir")
    @classmethod
    def require_absolute_container_path(cls, value: Path, validation_info: ValidationInfo) -> Path:
        """a relative path would resolve against the container's `/app` cwd — media would land inside the image instead
        of on the volume."""
        if not value.as_posix().startswith("/"):
            raise ValueError(f"{validation_info.field_name} must be an absolute path inside the container")
        return value

    @field_validator("webapp_public_url", mode="before")
    @classmethod
    def normalise_webapp_public_url(cls, value: object) -> object:
        """an empty `WEBAPP_PUBLIC_URL=` in `.env` means "off", not an invalid URL."""
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        stripped = value.strip().rstrip("/")
        if not stripped:
            return None

        return bare_https_origin(stripped)

    @model_validator(mode="after")
    def require_soft_limit_below_hard_limit(self) -> Settings:
        """a soft limit at or above the hard one never fires, and the task is SIGKILLed with no chance to clean up its
        `.partial` files."""
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
            (
                "plain_lyrics",
                self.plain_lyrics_task_soft_time_limit_seconds,
                self.plain_lyrics_task_time_limit_seconds,
            ),
        )
        for task_name, soft_limit, hard_limit in limit_pairs:
            if soft_limit >= hard_limit:
                raise ValueError(
                    f"{task_name}_task_soft_time_limit_seconds must be below {task_name}_task_time_limit_seconds"
                )
        return self


def get_settings() -> Settings:
    """not cached: tests build fresh settings per case via `monkeypatch.setenv`."""
    return Settings()


def redaction_secrets(settings: Settings) -> list[str]:
    """tracebacks count too: `/readyz` logs with `exc_info=True`, and asyncpg or kombu connection errors can embed the
    DSN password in their message."""
    secrets = [
        settings.bot_token.get_secret_value(),
        settings.spotify_client_secret.get_secret_value(),
    ]
    password = make_url(settings.database_url).password
    if password:
        secrets.append(password)

    secrets.extend(url_password_forms(settings.redis_url))
    if settings.https_proxy is not None:
        proxy_url = settings.https_proxy.get_secret_value()
        secrets.append(proxy_url)
        secrets.extend(url_password_forms(proxy_url))
    return secrets


def url_password_forms(url: str) -> list[str]:
    """both forms: a leaked URL carries the percent-encoded password, while redis-py/kombu unquote it before it
    reaches a client-side message."""
    quoted_password = urlsplit(url).password
    if not quoted_password:
        return []

    unquoted_password = unquote(quoted_password)
    if unquoted_password == quoted_password:
        return [quoted_password]
    return [quoted_password, unquoted_password]
