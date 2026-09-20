"""0003_step3_caption_columns

Three columns step 3's caption needs, all added while the tables are
practically empty (7 tracks, 12 posts): `track.album_id_spotify` (D-45,
previously alive only inside a rendered `href`), `track.languages` (D-61)
and `post.published_by_user_id` (D-62).

`languages` is filled for existing rows by its own `server_default`, a DDL
default rather than an `UPDATE` — so no row's `updated_at` goes stale
behind the ORM's back.

Revision ID: 4f5a52e4998d
Revises: d1faad3dd036
Create Date: 2026-09-19 21:06:21.324640

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4f5a52e4998d"
down_revision: str | Sequence[str] | None = "d1faad3dd036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Who approved and published, next to `created_by_user_id` (who
    # submitted the link). Written by the next sub-step, not by this one.
    op.add_column("post", sa.Column("published_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column("track", sa.Column("album_id_spotify", sa.Text(), nullable=True))
    op.add_column(
        "track",
        sa.Column(
            "languages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"українська\"]'::jsonb"),
            nullable=False,
        ),
    )
    # Autogenerate never emits CHECK constraints; same guard the other two
    # JSONB columns on this table already carry.
    op.create_check_constraint(
        op.f("ck_track_languages_is_array"), "track", "jsonb_typeof(languages) = 'array'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_track_languages_is_array"), "track", type_="check")
    op.drop_column("track", "languages")
    op.drop_column("track", "album_id_spotify")
    op.drop_column("post", "published_by_user_id")
