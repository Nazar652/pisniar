"""ORM models for `track`, `post`, `source_fetch` (step 2 resolver cache) and
step 4A's media artifacts, `audio_asset` (per track) and `render` (per post).
`lyric` (step 4B) and `post_status_history` (D-9) stay absent — see
decision-log.md.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import Identity
from sqlalchemy.types import TypeDecorator

from pisniar.db.base import Base
from pisniar.db.states import PostStatus

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect

VALID_POST_STATUSES = tuple(status.value for status in PostStatus)
_VALID_POST_STATUSES_SQL_LIST = ", ".join(f"'{value}'" for value in VALID_POST_STATUSES)

# D-61: language is a property of the recording, not of a publication, and
# no source reports it — so it is stored with a default and edited by hand.
DEFAULT_LANGUAGES = ["українська"]
_DEFAULT_LANGUAGES_SQL = f"'{json.dumps(DEFAULT_LANGUAGES, ensure_ascii=False)}'::jsonb"


class PostStatusType(TypeDecorator[PostStatus]):
    """Round-trips `PostStatus` through the `status` TEXT column (D-6: no
    native enum). Without it, `post.status` loads as plain `str` and
    `post.status is PostStatus.X` is always False — see decision-log.md.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: PostStatus | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return PostStatus(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> PostStatus | None:
        if value is None:
            return None
        return PostStatus(value)


class Track(Base):
    """A resolved (or not-yet-resolved) music track, keyed by its source URL."""

    __tablename__ = "track"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(artists) = 'array'", name="artists_is_array"),
        CheckConstraint("jsonb_typeof(platform_links) = 'object'", name="platform_links_is_object"),
        CheckConstraint("jsonb_typeof(languages) = 'array'", name="languages_is_array"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url_normalized: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Indexed for the D-41 duplicate warning: the same recording reaching
    # us through a second platform's URL is otherwise invisible.
    isrc: Mapped[str | None] = mapped_column(Text, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    # form: [{"name": str, "hashtag": str | None, "spotify_id": str | None}];
    # `hashtag` stays null until a human overrides it (D-29).
    artists: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    year: Mapped[int | None] = mapped_column(SmallInteger)
    release_type: Mapped[str | None] = mapped_column(Text)
    release_title: Mapped[str | None] = mapped_column(Text)
    # Persisted since D-45: it used to exist only inside the `href` of a
    # rendered caption, so no caption could be rebuilt without losing it.
    album_id_spotify: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    # Step 4 checks the downloaded audio against it, so it has to outlive the
    # resolve that found it — and to survive a `/retry` (`STORED_SCALAR_FIELDS`).
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    # form: ["українська", ...]; the first one is the primary language and
    # the order is the admin's (D-61). Only step 5's Mini App writes here.
    languages: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text(_DEFAULT_LANGUAGES_SQL)
    )
    platform_links: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # passive_deletes="all": without it, SQLAlchemy nulls track_id before
    # delete, so ON DELETE RESTRICT never fires. See decision-log.md.
    posts: Mapped[list[Post]] = relationship(back_populates="track", passive_deletes="all")


class Post(Base):
    """One channel post per `Track`, several allowed (no `UNIQUE (track_id)`).

    `fragment_*_ms` are absolute ms from the start of the *full* track (D-5):
    `ass_t = lyric.start_ms - post.fragment_start_ms` at render time.
    """

    __tablename__ = "post"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_VALID_POST_STATUSES_SQL_LIST})",
            name="status_valid",
        ),
        CheckConstraint(
            f"failed_from_status IS NULL OR failed_from_status IN "
            f"({_VALID_POST_STATUSES_SQL_LIST})",
            name="failed_from_status_valid",
        ),
        # (NULL, NULL) is valid (no fragment yet); num_nonnulls<>1 rejects a
        # one-sided bound; start>=0 rejects a negative shift per D-5.
        CheckConstraint(
            "num_nonnulls(fragment_start_ms, fragment_end_ms) <> 1 "
            "AND (fragment_start_ms IS NULL OR fragment_start_ms >= 0) "
            "AND (fragment_end_ms IS NULL OR fragment_end_ms > fragment_start_ms)",
            name="fragment_bounds_valid",
        ),
        CheckConstraint("jsonb_typeof(genres) = 'array'", name="genres_is_array"),
        # Same one-sided-pair problem as the fragment bounds above.
        CheckConstraint(
            "num_nonnulls(channel_chat_id, channel_message_id) <> 1",
            name="channel_ref_complete",
        ),
        Index(
            "uq_post_channel_message",
            "channel_chat_id",
            "channel_message_id",
            unique=True,
            postgresql_where=text("channel_chat_id IS NOT NULL AND channel_message_id IS NOT NULL"),
        ),
        # Same one-sided-pair problem, for the draft delivery (not channel
        # publication): `draft_message_id IS NULL` is the send condition.
        CheckConstraint(
            "num_nonnulls(draft_chat_id, draft_message_id) <> 1",
            name="draft_ref_complete",
        ),
        Index(
            "uq_post_draft_message",
            "draft_chat_id",
            "draft_message_id",
            unique=True,
            postgresql_where=text("draft_chat_id IS NOT NULL AND draft_message_id IS NOT NULL"),
        ),
        # The video preview is a *separate* message from the draft (D-81):
        # `editMessageMedia` cannot turn a text message into a media one.
        CheckConstraint(
            "num_nonnulls(preview_chat_id, preview_message_id) <> 1",
            name="preview_ref_complete",
        ),
        Index(
            "uq_post_preview_message",
            "preview_chat_id",
            "preview_message_id",
            unique=True,
            postgresql_where=text("preview_chat_id IS NOT NULL AND preview_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("track.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[PostStatus] = mapped_column(
        PostStatusType,
        nullable=False,
        default=PostStatus.DRAFT,
        server_default=PostStatus.DRAFT.value,
        index=True,
    )
    caption: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    fragment_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    fragment_end_ms: Mapped[int | None] = mapped_column(BigInteger)

    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # The admin chat the draft was delivered to; `draft_message_id IS NULL`
    # is delivery's own idempotency guard (Celery redelivery, D-28).
    draft_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    draft_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # The rendered video shown to the admin before publication (AS11/D-65);
    # `preview_message_id IS NULL` is that delivery's idempotency guard.
    preview_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    preview_message_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Which stage failed, written by `set_post_status()` next to `last_error` —
    # step 4 breaks the D-47 rule that inferred it from `draft_message_id`.
    failed_from_status: Mapped[PostStatus | None] = mapped_column(PostStatusType)
    # `created_by_user_id` is who *submitted* the link; this one is who
    # approved and published it (D-62) — normally two different admins.
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_by_user_id: Mapped[int | None] = mapped_column(BigInteger)

    track: Mapped[Track] = relationship(back_populates="posts")


class AudioAsset(Base):
    """One downloaded audio file, keyed to a **track** — several posts may
    share one since step 1. `UNIQUE (track_id, video_id)` is the database half
    of the idempotency guard; the atomic `os.replace` on disk is the other.
    """

    __tablename__ = "audio_asset"
    __table_args__ = (
        UniqueConstraint("track_id", "video_id"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("duration_ms > 0", name="duration_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    # CASCADE, unlike `post.track_id`: an artifact of a deleted track is not a
    # reason to refuse the delete, and a file without a row is the safe side.
    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("track.id", ondelete="CASCADE"), nullable=False
    )
    # From `platform_links["youtubeMusic"]` and never `["youtube"]` — stored so
    # a wrong download can be told apart from a wrong choice of source.
    video_id: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Measured by `ffprobe` on the file itself, never copied from the resolve:
    # that is what makes it evidence yt-dlp returned the track we asked for.
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # D-80: the extraction path is deprecated upstream, so "the extractor
    # broke" has to stay tellable from "this one track is gone".
    ytdlp_version: Mapped[str] = mapped_column(Text, nullable=False)


class Render(Base):
    """One rendered video, keyed to a **post** — the caption and the fragment
    bounds belong to the publication, not the recording. `params_hash` covers
    every input the frames depend on, `text_version` included.
    """

    __tablename__ = "render"
    __table_args__ = (
        UniqueConstraint("post_id", "params_hash"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("duration_ms > 0", name="duration_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("post.id", ondelete="CASCADE"), nullable=False
    )
    params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Null through the whole of 4A. It is an input to `params_hash`, so the
    # column is what keeps that hash auditable once 4B starts filling it.
    text_version: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # `ffprobe` of the output, against which the fragment length is asserted —
    # ffmpeg exiting 0 on an unusable file is step 4's main silent failure.
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SourceFetch(Base):
    """Cache of one external-source request, keyed by `(source, request_key)`
    (contract p.1). `payload` holds `{"found": bool, "data": ... | None}` —
    a negative result is cached too, so a retry never re-hits a miss.
    """

    __tablename__ = "source_fetch"
    __table_args__ = (UniqueConstraint("source", "request_key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # Written from the clock at fetch time, not `now()` (F-23), and indexed
    # for the TTL read plus the sweeper that will reap stale rows (D-40).
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
