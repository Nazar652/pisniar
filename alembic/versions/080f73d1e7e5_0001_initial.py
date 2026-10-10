"""0001 initial

Revision ID: 080f73d1e7e5
Revises:
Create Date: 2026-09-18 21:01:14.648810

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "080f73d1e7e5"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """upgrade schema."""
    op.create_table(
        "track",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        # cache key: the Odesli-normalized URL, so the same track is never re-resolved twice.
        sa.Column("source_url_normalized", sa.Text(), nullable=False),
        sa.Column("isrc", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        # form: [{"name": str, "hashtag": str}, ...]
        sa.Column(
            "artists",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("year", sa.SmallInteger(), nullable=True),
        sa.Column("release_type", sa.Text(), nullable=True),
        sa.Column("release_title", sa.Text(), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column(
            "platform_links",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("odesli_raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("odesli_fetched_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("jsonb_typeof(artists) = 'array'", name=op.f("ck_track_artists_is_array")),
        sa.CheckConstraint(
            "jsonb_typeof(platform_links) = 'object'",
            name=op.f("ck_track_platform_links_is_object"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_track")),
        sa.UniqueConstraint("source_url_normalized", name=op.f("uq_track_source_url_normalized")),
    )
    op.create_table(
        "post",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("track_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column(
            "genres",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        # absolute ms from the start of the full track, so re-editing the
        # fragment bounds does not invalidate a synced lyric.
        sa.Column("fragment_start_ms", sa.BigInteger(), nullable=True),
        sa.Column("fragment_end_ms", sa.BigInteger(), nullable=True),
        sa.Column("channel_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_message_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'resolving', 'resolved', 'downloading', 'downloaded', "
            "'editing', 'rendering', 'rendered', 'publishing', 'published', 'failed', 'archived')",
            name=op.f("ck_post_status_valid"),
        ),
        # `(NULL, NULL)` means no fragment yet; a one-sided bound or a negative
        # start would silently shift every lyric line.
        sa.CheckConstraint(
            "num_nonnulls(fragment_start_ms, fragment_end_ms) <> 1 "
            "AND (fragment_start_ms IS NULL OR fragment_start_ms >= 0) "
            "AND (fragment_end_ms IS NULL OR fragment_end_ms > fragment_start_ms)",
            name=op.f("ck_post_fragment_bounds_valid"),
        ),
        sa.CheckConstraint("jsonb_typeof(genres) = 'array'", name=op.f("ck_post_genres_is_array")),
        # same one-sided-pair problem as the fragment bounds: without this,
        # `(NULL, 42)` passes both the partial unique index below and any NOT NULL check.
        sa.CheckConstraint(
            "num_nonnulls(channel_chat_id, channel_message_id) <> 1",
            name=op.f("ck_post_channel_ref_complete"),
        ),
        sa.ForeignKeyConstraint(["track_id"], ["track.id"], name=op.f("fk_post_track_id_track"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post")),
    )
    op.create_index(op.f("ix_post_status"), "post", ["status"], unique=False)
    op.create_index(op.f("ix_post_track_id"), "post", ["track_id"], unique=False)
    # partial: guards against a duplicate channel post once both id columns
    # are set; posts with either still NULL (not yet published) don't conflict.
    op.create_index(
        "uq_post_channel_message",
        "post",
        ["channel_chat_id", "channel_message_id"],
        unique=True,
        postgresql_where=sa.text("channel_chat_id IS NOT NULL AND channel_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    """downgrade schema."""
    op.drop_index(
        "uq_post_channel_message",
        table_name="post",
        postgresql_where=sa.text("channel_chat_id IS NOT NULL AND channel_message_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_post_track_id"), table_name="post")
    op.drop_index(op.f("ix_post_status"), table_name="post")
    op.drop_table("post")
    op.drop_table("track")
