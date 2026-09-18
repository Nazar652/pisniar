"""ORM models for `track` and `post` — the step 1 schema (D-4: minimum for step 2).

`audio_asset`, `lyric`, `render` are intentionally absent; they arrive in
their own migrations once step 4 actually needs them. `post_status_history`
is intentionally absent too (D-9) — transition history lives in structured
logs (`post_id` bound via `pisniar.logging`), not in a table, until/unless
that stops being enough.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import Identity

from pisniar.db.base import Base
from pisniar.db.states import PostStatus

VALID_POST_STATUSES = tuple(status.value for status in PostStatus)
_VALID_POST_STATUSES_SQL_LIST = ", ".join(f"'{value}'" for value in VALID_POST_STATUSES)


class Track(Base):
    """A resolved (or not-yet-resolved) music track, keyed by its source URL."""

    __tablename__ = "track"

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
    odesli_raw: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    odesli_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    posts: Mapped[list[Post]] = relationship(back_populates="track")


class Post(Base):
    """A single channel post draft/pipeline instance for one `Track`.

    Several posts may reference the same track (no `UNIQUE (track_id)`, per
    the approved assumptions) — e.g. re-posting the same track later with a
    different fragment or caption.

    `fragment_start_ms` / `fragment_end_ms` are absolute milliseconds from the
    start of the *full* track (D-5), not from the fragment itself. At render
    time: `ass_t = lyric.start_ms - post.fragment_start_ms`. This keeps a
    manually-synced lyric timing valid even if the fragment boundaries are
    edited afterwards.
    """

    __tablename__ = "post"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_VALID_POST_STATUSES_SQL_LIST})",
            name="status_valid",
        ),
        CheckConstraint(
            "fragment_end_ms > fragment_start_ms",
            name="fragment_bounds_valid",
        ),
        Index(
            "uq_post_channel_message",
            "channel_chat_id",
            "channel_message_id",
            unique=True,
            postgresql_where=text("channel_message_id IS NOT NULL"),
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
        Text, nullable=False, server_default=PostStatus.DRAFT.value, index=True
    )
    caption: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    fragment_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    fragment_end_ms: Mapped[int | None] = mapped_column(BigInteger)

    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    track: Mapped[Track] = relationship(back_populates="posts")
