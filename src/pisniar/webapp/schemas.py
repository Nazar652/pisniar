"""Request and response models of the Mini App API; the source of the OpenAPI schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
