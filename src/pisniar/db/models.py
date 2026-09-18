"""ORM models for `track` and `post` — the step 1 schema (D-4: minimum for step 2).

`audio_asset`, `lyric`, `render` are intentionally absent; they arrive in
their own migrations once step 4 actually needs them. `post_status_history`
is intentionally absent too (D-9) — transition history lives in structured
logs (`post_id` bound via `pisniar.logging`), not in a table, until/unless
that stops being enough.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, SmallInteger, Text, text
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
    """Round-trips `PostStatus` through the `status` `TEXT` column.

    The column itself stays plain `TEXT` + a named `CHECK` (contract D-6: no
    native Postgres enum — `ALTER TYPE ... ADD VALUE` doesn't play well with
    Alembic downgrades, and the set of statuses will keep growing). This
    decorator is what makes `Mapped[PostStatus]` an honest annotation instead
    of a decorative one: without it, a `Post` loaded from the database has
    `post.status` as a plain `str`, and `post.status is PostStatus.PUBLISHED`
    (the comparison `pisniar.db.states.set_post_status` uses) is always
    `False` — a straight path to a duplicate publish on step 3.
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
    odesli_raw: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    odesli_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # `passive_deletes="all"`: without it, SQLAlchemy "helpfully" issues
    # `UPDATE post SET track_id = NULL` before deleting a `Track` with
    # referencing posts, which never lets `ON DELETE RESTRICT` fire at all —
    # the delete then fails on `track_id`'s `NOT NULL` instead, pointing at
    # the wrong constraint. `track_id` stays `NOT NULL`: that's the guard
    # that turns silent data corruption into a loud error in the first place.
    #
    # The same flag also disables all ORM-side cascade behaviour for this
    # relationship, including on the *in-Python* collection: today nothing
    # in this codebase calls `track.posts.remove(post)`, but if something
    # ever does, `passive_deletes="all"` means SQLAlchemy will not null out
    # or otherwise touch that post's `track_id` for you — removing an item
    # from `track.posts` becomes a silent no-op at the database level.
    posts: Mapped[list[Post]] = relationship(back_populates="track", passive_deletes="all")


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
        # Three-way: `(NULL, NULL)` (no fragment chosen yet) is valid and
        # deliberate. Everything else must be a real, well-formed fragment —
        # `num_nonnulls(...) <> 1` rejects a one-sided bound (`(5000, NULL)`),
        # and `fragment_start_ms >= 0` rejects a negative start, which would
        # otherwise pass silently and shift every lyric line forward per D-5's
        # `ass_t = lyric.start_ms - fragment_start_ms` — an error that is
        # invisible until someone watches the rendered video.
        CheckConstraint(
            "num_nonnulls(fragment_start_ms, fragment_end_ms) <> 1 "
            "AND (fragment_start_ms IS NULL OR fragment_start_ms >= 0) "
            "AND (fragment_end_ms IS NULL OR fragment_end_ms > fragment_start_ms)",
            name="fragment_bounds_valid",
        ),
        CheckConstraint("jsonb_typeof(genres) = 'array'", name="genres_is_array"),
        # Same one-sided-pair problem as the fragment bounds above, applied
        # to the channel reference: without this, `(NULL, 42)` passes both
        # the partial unique index below (its predicate only fires once
        # *both* columns are non-NULL) and any NOT NULL check, silently
        # leaving a post with half a channel reference.
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
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    track: Mapped[Track] = relationship(back_populates="posts")
