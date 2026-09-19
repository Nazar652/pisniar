"""ORM models for `track`, `post`, and `source_fetch` (step 2 resolver cache).
`audio_asset`/`lyric`/`render`/`post_status_history` intentionally absent
(D-4, D-9) — see decision-log.md.
"""

from __future__ import annotations

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

    isrc: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    # form: [{"name": str, "hashtag": str}, ...]
    artists: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    year: Mapped[int | None] = mapped_column(SmallInteger)
    release_type: Mapped[str | None] = mapped_column(Text)
    release_title: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
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
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    track: Mapped[Track] = relationship(back_populates="posts")


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
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
