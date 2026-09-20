"""0005_step4a_preview_ref

The admin-facing video preview (AS11, a consequence of D-65): a separate
message from the text draft, because `editMessageMedia` cannot turn a text
message into a media one — so it needs its own chat/message reference,
shaped exactly like `draft_chat_id`/`draft_message_id`.

A separate revision rather than an addition to `0004` (D-81): `0004` is
already applied to a database holding 23 posts, and columns appended to an
applied revision never reach it while `alembic check` starts reporting drift.

Both columns are nullable, so nothing is backfilled and no row's `updated_at`
goes stale behind the ORM.

`ck_post_preview_ref_complete` is written by hand: autogenerate never emits
CHECK constraints on an existing table.

Revision ID: 501740df6176
Revises: 199284e533b4
Create Date: 2026-09-20 12:19:28.279865

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "501740df6176"
down_revision: str | Sequence[str] | None = "199284e533b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIEW_REFERENCE_IS_SET = sa.text(
    "preview_chat_id IS NOT NULL AND preview_message_id IS NOT NULL"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("post", sa.Column("preview_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("post", sa.Column("preview_message_id", sa.BigInteger(), nullable=True))
    # Same one-sided-pair problem as the draft and channel references: half a
    # reference is a message the bot can neither edit nor find again.
    op.create_check_constraint(
        op.f("ck_post_preview_ref_complete"),
        "post",
        "num_nonnulls(preview_chat_id, preview_message_id) <> 1",
    )
    op.create_index(
        "uq_post_preview_message",
        "post",
        ["preview_chat_id", "preview_message_id"],
        unique=True,
        postgresql_where=_PREVIEW_REFERENCE_IS_SET,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_post_preview_message", table_name="post", postgresql_where=_PREVIEW_REFERENCE_IS_SET
    )
    op.drop_constraint(op.f("ck_post_preview_ref_complete"), "post", type_="check")
    op.drop_column("post", "preview_message_id")
    op.drop_column("post", "preview_chat_id")
