"""Request and response models of the Mini App API; the source of the OpenAPI schema."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from pisniar.db.models import MAX_LYRIC_LINES

# real `initData` is under 2 KiB; the bound keeps a hostile body cheap to reject.
INIT_DATA_MAX_LENGTH = 8192

# the range of a Postgres BIGINT: a value beyond it is a bad request, not a database error
BIGINT_MAX = 9_223_372_036_854_775_807
BIGINT_MIN = -BIGINT_MAX - 1
POST_ID_MAX = BIGINT_MAX

# a whole number of ms that Postgres can hold; `bool` is refused
BigintMs = Annotated[StrictInt, Field(ge=BIGINT_MIN, le=BIGINT_MAX)]


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


class FragmentAvailability(BaseModel):
    """whether the fragment can be chosen now, or the audio can be prepared for it, judged from the status alone; at
    most one of the first three flags is true."""

    start_ms: int | None
    end_ms: int | None
    choosable: bool
    preparable: bool
    preparing: bool
    cancellable: bool
    reason: str | None


class CoverPublicationAvailability(BaseModel):
    """whether the cover can be published without a video, from the status and the preview."""

    allowed: bool
    reason: str | None


class CoverPublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_post_updated_at: datetime


class CoverPublicationStarted(BaseModel):
    post_id: int
    status_label: str


class CancelPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_post_updated_at: datetime


class AudioPreparationCancelled(BaseModel):
    post_id: int
    status_label: str


class FragmentPeaks(BaseModel):
    peaks_per_second: int
    values: list[int]


class FragmentView(BaseModel):
    """the editor's data: the bounds of the current video, the audio and the waveform to draw."""

    post_updated_at: datetime
    start_ms: int
    end_ms: int
    audio_duration_ms: int
    max_length_ms: int
    peaks: FragmentPeaks | None
    audio_url: str
    audio_url_expires_at: datetime


class FragmentRequest(BaseModel):
    """the bounds the admin chose, in whole ms from the start of the full track, and the version of the post the editor
    was opened with."""

    model_config = ConfigDict(extra="forbid")

    start_ms: BigintMs
    end_ms: BigintMs
    expected_post_updated_at: datetime


class FragmentRenderStarted(BaseModel):
    post_id: int
    status_label: str
    start_ms: int
    end_ms: int


class AudioPreparationStarted(BaseModel):
    post_id: int
    status_label: str


class PostDetail(BaseModel):
    id: int
    has_video: bool
    status_label: str
    editable: bool
    not_editable_reason: str | None
    fragment: FragmentAvailability
    cover_publication: CoverPublicationAvailability
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


class ApplyRequest(BaseModel):
    """the hash of the caption the editor showed: the caption is rebuilt and compared before anything is sent."""

    model_config = ConfigDict(extra="forbid")

    expected_caption_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApplyResponse(BaseModel):
    caption_sha256: str
    result: Literal["edited", "not_modified", "stored_only"]
    other_messages_failed: list[str]


class ArchiveResponse(BaseModel):
    post_id: int
    status_label: str


class LyricLineView(BaseModel):
    """one stored line; `start_ms` is `None` for a line not yet timed."""

    text: str
    start_ms: int | None
    source: str


class LyricsView(BaseModel):
    """the track's text as stored. `lyrics_version` is a digest of it, sent back with a save."""

    lines: list[LyricLineView]
    lyrics_version: str
    edited_by_admin: bool
    other_post_ids: list[int]
    max_lines: int
    max_line_length: int
    max_total_characters: int


class LyricLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    start_ms: BigintMs | None


class LyricsUpdate(BaseModel):
    """the whole text the editor shows, replacing the stored one, and the version it was opened
    with. An empty list removes the text."""

    model_config = ConfigDict(extra="forbid")

    lines: list[LyricLineInput] = Field(max_length=MAX_LYRIC_LINES)
    expected_lyrics_version: str = Field(pattern=r"^[0-9a-f]{64}$")


LyricsSourceName = Literal["ytmusic", "lrclib", "genius"]


class ProposedLine(BaseModel):
    text: str
    start_ms: int | None


class LyricsProposal(BaseModel):
    """text found by a lookup, written nowhere: the editor shows it and the admin may replace the
    buffer with it. `source` is the one that answered."""

    kind: Literal["synced", "plain"]
    source: LyricsSourceName
    lines: list[ProposedLine]
    dropped_line_count: int
    truncated_line_count: int
