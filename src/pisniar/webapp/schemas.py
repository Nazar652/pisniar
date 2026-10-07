"""Request and response models of the Mini App API; the source of the OpenAPI schema."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# real `initData` is under 2 KiB; the bound keeps a hostile body cheap to reject.
INIT_DATA_MAX_LENGTH = 8192


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_data: str = Field(min_length=1, max_length=INIT_DATA_MAX_LENGTH)


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    expires_in: int


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
