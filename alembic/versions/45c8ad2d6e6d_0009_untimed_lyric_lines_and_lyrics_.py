"""0009 untimed lyric lines and lyrics edited marker

`lyric.start_ms` may be NULL (a line not yet timed; the render never reads it) and
`track.lyrics_edited_at` marks a text a human saved, so no automatic source writes over it.

Revision ID: 45c8ad2d6e6d
Revises: ba0fdb351081
Create Date: 2026-10-10 18:28:02.849909

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "45c8ad2d6e6d"
down_revision: str | Sequence[str] | None = "ba0fdb351081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """upgrade schema."""
    op.alter_column("lyric", "start_ms", existing_type=sa.BIGINT(), nullable=True)
    # no backfill: NULL is true for every existing track, none was edited by hand.
    op.add_column(
        "track",
        sa.Column("lyrics_edited_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """downgrade schema. Lines not yet timed cannot survive a NOT NULL column."""
    op.drop_column("track", "lyrics_edited_at")
    op.execute(sa.text("DELETE FROM lyric WHERE start_ms IS NULL"))
    op.alter_column("lyric", "start_ms", existing_type=sa.BIGINT(), nullable=False)
