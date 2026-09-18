"""0001_initial

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
    """Upgrade schema."""
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
        # Cache key: the Odesli-normalized URL, so the same track is never
        # re-resolved twice.
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
        # Absolute milliseconds from the start of the *full* track (D-5), not
        # from the fragment itself. At render time:
        # ass_t = lyric.start_ms - post.fragment_start_ms. This keeps a
        # manually-synced lyric timing valid even if the fragment boundaries
        # are edited afterwards.
        sa.Column("fragment_start_ms", sa.BigInteger(), nullable=True),
        sa.Column("fragment_end_ms", sa.BigInteger(), nullable=True),
        sa.Column("channel_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_message_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'resolving', 'resolved', 'downloading', 'downloaded', "
            "'editing', 'rendering', 'rendered', 'publishing', 'published', 'failed', "
            "'archived')",
            name=op.f("ck_post_status_valid"),
        ),
        # NULL semantics: a Postgres CHECK only fires when both sides are
        # non-NULL, so a post with an unset fragment (both NULL) or with only
        # one bound set passes this constraint; the "both set" invariant is
        # not modeled here, since draft posts legitimately have neither yet.
        sa.CheckConstraint(
            "fragment_end_ms > fragment_start_ms", name=op.f("ck_post_fragment_bounds_valid")
        ),
        sa.ForeignKeyConstraint(
            ["track_id"], ["track.id"], name=op.f("fk_post_track_id_track"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post")),
    )
    op.create_index(op.f("ix_post_status"), "post", ["status"], unique=False)
    op.create_index(op.f("ix_post_track_id"), "post", ["track_id"], unique=False)
    # Partial unique index: guards against a duplicate channel post (step 3)
    # once `channel_message_id` is set; several posts with it still NULL
    # (not yet published) do not conflict with each other.
    op.create_index(
        "uq_post_channel_message",
        "post",
        ["channel_chat_id", "channel_message_id"],
        unique=True,
        postgresql_where=sa.text("channel_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_post_channel_message",
        table_name="post",
        postgresql_where=sa.text("channel_message_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_post_track_id"), table_name="post")
    op.drop_index(op.f("ix_post_status"), table_name="post")
    op.drop_table("post")
    op.drop_table("track")
