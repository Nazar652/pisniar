"""0006_step4b_lyric

Step 4B's only schema change: one line of lyrics per row, keyed to a
**track** (precedent `audio_asset`, D-61) with `ON DELETE CASCADE` — stored
text is not a reason to refuse deleting a track.

`start_ms` is absolute from the start of the *full* track (D-5), so render
time computes `ass_t = lyric.start_ms - post.fragment_start_ms`. It is
`NOT NULL` here: step 7 drops that with `ALTER COLUMN ... DROP NOT NULL`,
which Postgres does instantly, and the render path already spells the
`start_ms IS NOT NULL` predicate out (seam §2.1.2).

There is no `end_ms` (D-4): the boundary is derived at render time from the
next line, a per-line ceiling and the fragment end.

A brand-new table, so no column is backfilled and no existing row's
`updated_at` goes stale behind the ORM. `post.render_params_hash` is
deliberately **not** part of this revision (D-87).

Revision ID: be8d6bff2a6c
Revises: 501740df6176
Create Date: 2026-09-20 13:49:42.508704

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "be8d6bff2a6c"
down_revision: str | Sequence[str] | None = "501740df6176"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "lyric",
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
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint("length(text) <= 500", name=op.f("ck_lyric_text_length_bounded")),
        sa.CheckConstraint("start_ms >= 0", name=op.f("ck_lyric_start_ms_non_negative")),
        sa.ForeignKeyConstraint(
            ["track_id"], ["track.id"], name=op.f("fk_lyric_track_id_track"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lyric")),
        sa.UniqueConstraint("track_id", "line_index", name=op.f("uq_lyric_track_id_line_index")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("lyric")
