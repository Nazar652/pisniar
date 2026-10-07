"""0004_step4a_media_schema

The media schema: `track.duration_ms`, `post.failed_from_status` and the two
artifact tables, `audio_asset` per **track** and `render` per **post**. Every
new column is nullable or in a new table, so nothing is backfilled.
`ck_post_failed_from_status_valid` is hand-written, like every CHECK on an existing table.

Revision ID: 199284e533b4
Revises: 4f5a52e4998d
Create Date: 2026-09-20 11:18:27.369691

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "199284e533b4"
down_revision: str | Sequence[str] | None = "4f5a52e4998d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_STATUSES_SQL = (
    "'draft', 'resolving', 'resolved', 'downloading', 'downloaded', "
    "'editing', 'rendering', 'rendered', 'publishing', 'published', 'failed', "
    "'archived'"
)


def upgrade() -> None:
    """Upgrade schema."""
    # CASCADE, unlike `post.track_id`'s RESTRICT: an artifact is not a reason
    # to refuse deleting its track, and the file is cleaned up after the commit.
    op.create_table(
        "audio_asset",
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
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("ytdlp_version", sa.Text(), nullable=False),
        # A zero-byte or zero-length artifact is the shape a silent ffmpeg or
        # yt-dlp failure takes; the row for one must not be insertable at all.
        sa.CheckConstraint("duration_ms > 0", name=op.f("ck_audio_asset_duration_positive")),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_audio_asset_size_positive")),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["track.id"],
            name=op.f("fk_audio_asset_track_id_track"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audio_asset")),
        # The database half of the download idempotency guard — the atomic
        # `os.replace` on the volume is the other half.
        sa.UniqueConstraint("track_id", "video_id", name=op.f("uq_audio_asset_track_id_video_id")),
    )
    op.create_table(
        "render",
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
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("params_hash", sa.Text(), nullable=False),
        # NULL for a render without lyrics. It is an input to `params_hash`, so
        # the column is what keeps that hash auditable.
        sa.Column("text_version", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("duration_ms > 0", name=op.f("ck_render_duration_positive")),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_render_size_positive")),
        sa.ForeignKeyConstraint(
            ["post_id"], ["post.id"], name=op.f("fk_render_post_id_post"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_render")),
        sa.UniqueConstraint("post_id", "params_hash", name=op.f("uq_render_post_id_params_hash")),
    )
    op.add_column("post", sa.Column("failed_from_status", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_post_failed_from_status_valid"),
        "post",
        f"failed_from_status IS NULL OR failed_from_status IN ({_VALID_STATUSES_SQL})",
    )
    op.add_column("track", sa.Column("duration_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("track", "duration_ms")
    op.drop_constraint(op.f("ck_post_failed_from_status_valid"), "post", type_="check")
    op.drop_column("post", "failed_from_status")
    op.drop_table("render")
    op.drop_table("audio_asset")
