"""0002_resolve_pipeline

Irreversible by data: `downgrade()` drops `source_fetch`, destroying the
whole resolver cache, and `odesli_raw`/`odesli_fetched_at` come back empty.
Deployment order is always: stop `bot`/`worker` -> migrate (one-shot
container) -> start the new image; no rolling window exists (F-28).

Revision ID: d1faad3dd036
Revises: 080f73d1e7e5
Create Date: 2026-09-19 11:43:14.802516

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1faad3dd036"
down_revision: str | Sequence[str] | None = "080f73d1e7e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "source_fetch",
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
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_fetch")),
        sa.UniqueConstraint(
            "source", "request_key", name=op.f("uq_source_fetch_source_request_key")
        ),
    )
    # Read on every TTL check and by the future staleness sweeper (D-40).
    op.create_index(
        op.f("ix_source_fetch_fetched_at"), "source_fetch", ["fetched_at"], unique=False
    )
    op.add_column("post", sa.Column("draft_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("post", sa.Column("draft_message_id", sa.BigInteger(), nullable=True))
    # Same one-sided-pair CHECK as `channel_ref_complete` (D-5-style guard).
    op.create_check_constraint(
        op.f("ck_post_draft_ref_complete"),
        "post",
        "num_nonnulls(draft_chat_id, draft_message_id) <> 1",
    )
    op.create_index(
        "uq_post_draft_message",
        "post",
        ["draft_chat_id", "draft_message_id"],
        unique=True,
        postgresql_where=sa.text("draft_chat_id IS NOT NULL AND draft_message_id IS NOT NULL"),
    )
    op.add_column(
        "track", sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    # Non-unique: a repost a year later is a legitimate second row, the
    # index only powers the D-41 duplicate warning.
    op.create_index(op.f("ix_track_isrc"), "track", ["isrc"], unique=False)
    op.drop_column("track", "odesli_raw")
    op.drop_column("track", "odesli_fetched_at")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "track",
        sa.Column(
            "odesli_fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            autoincrement=False,
            nullable=True,
        ),
    )
    op.add_column(
        "track",
        sa.Column(
            "odesli_raw",
            postgresql.JSONB(astext_type=sa.Text()),
            autoincrement=False,
            nullable=True,
        ),
    )
    op.drop_index(op.f("ix_track_isrc"), table_name="track")
    op.drop_column("track", "resolved_at")
    op.drop_index(
        "uq_post_draft_message",
        table_name="post",
        postgresql_where=sa.text("draft_chat_id IS NOT NULL AND draft_message_id IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_post_draft_ref_complete"), "post", type_="check")
    op.drop_column("post", "draft_message_id")
    op.drop_column("post", "draft_chat_id")
    op.drop_index(op.f("ix_source_fetch_fetched_at"), table_name="source_fetch")
    op.drop_table("source_fetch")
