"""Request and response models of the Mini App API; the source of the OpenAPI schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

# real `initData` is under 2 KiB; the bound keeps a hostile body cheap to reject.
INIT_DATA_MAX_LENGTH = 8192

# the largest value of a Postgres BIGINT: an id beyond it is a bad request, not a database error
POST_ID_MAX = 9_223_372_036_854_775_807


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


class PostSummary(BaseModel):
    id: int
    status_label: str
    title: str | None
    artists: list[str]
    created_at: datetime
    published_at: datetime | None
    editable: bool
    not_editable_reason: str | None
    has_video: bool


class PostListResponse(BaseModel):
    items: list[PostSummary]
    next_before_id: int | None


class ArtistDetail(BaseModel):
    name: str
    hashtag: str | None
    default_hashtag: str | None
    spotify_id: str | None


class GenreDetail(BaseModel):
    name: str
    hashtag: str | None
    default_hashtag: str | None
    source: str


class PlatformLinkDetail(BaseModel):
    url: str
    source: str
    match: str


class CaptionSegmentView(BaseModel):
    kind: Literal["text", "link", "line_break"]
    text: str | None
    href: str | None


class CaptionView(BaseModel):
    segments: list[CaptionSegmentView]
    visible_length: int
    limit: int
    dropped_genres: list[str]
    dropped_platforms: list[str]
    release_title_dropped: bool
    over_limit: bool
    warnings: list[str]
    sha256: str


class PostDetail(BaseModel):
    id: int
    status_label: str
    editable: bool
    not_editable_reason: str | None
    created_at: datetime
    published_at: datetime | None
    title: str | None
    artists: list[ArtistDetail]
    languages: list[str]
    genres: list[GenreDetail]
    year: int | None
    release_type: str | None
    release_title: str | None
    album_id_spotify: str | None
    album_url: str | None
    platform_links: dict[str, PlatformLinkDetail]
    manual_fields: list[str]
    removed_platforms: list[str]
    caption: CaptionView | None
    stored_caption_html: str | None
    stored_caption_matches: bool
    other_post_ids: list[int]
    expected_post_updated_at: datetime
    expected_track_updated_at: datetime


class PlatformRule(BaseModel):
    key: str
    display_name: str
    hosts: list[str]
    host_suffixes: list[str]
    requires_track_link: bool


class ReleaseTypeOption(BaseModel):
    value: str
    label: str


class GenreHint(BaseModel):
    name: str
    default_hashtag: str | None


class MetaResponse(BaseModel):
    platforms: list[PlatformRule]
    release_types: list[ReleaseTypeOption]
    languages: list[str]
    genres: list[GenreHint]
    manual_field_names: list[str]
    caption_limit: int


class ArtistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=200)
    hashtag: str | None = Field(default=None, max_length=64)


class GenreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=64)
    hashtag: str | None = Field(default=None, max_length=64)


class FieldsInput(BaseModel):
    """the full set of editable fields: what is missing from it is removed, not left alone."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    artists: list[ArtistInput] = Field(max_length=20)
    languages: list[str] = Field(max_length=10)
    genres: list[GenreInput] = Field(max_length=30)
    year: StrictInt | None = None
    release_type: str | None = Field(default=None, max_length=40)
    release_title: str | None = Field(default=None, max_length=300)
    album_link: str | None = Field(default=None, max_length=300)
    platform_links: dict[str, str] = Field(max_length=25)
    revert_to_automatic: list[str] = Field(default_factory=list, max_length=6)


class FieldsUpdate(FieldsInput):
    expected_post_updated_at: datetime
    expected_track_updated_at: datetime


class FieldError(BaseModel):
    field: str
    message: str


class FieldsInvalid(ApiError):
    errors: list[FieldError] = Field(default_factory=list)


class PreviewResponse(BaseModel):
    caption: CaptionView
    stored_caption_sha256: str | None
    differs_from_stored: bool
