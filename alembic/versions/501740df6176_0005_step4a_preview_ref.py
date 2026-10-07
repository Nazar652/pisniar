"""0005 preview reference

The video preview's own chat/message reference, shaped like the draft pair:
`editMessageMedia` cannot turn the text draft into media, so the preview is a
separate message. Both columns are nullable, so nothing is backfilled.
`ck_post_preview_ref_complete` is hand-written, like every CHECK on an existing table.

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

PREVIEW_REFERENCE_IS_SET = sa.text("preview_chat_id IS NOT NULL AND preview_message_id IS NOT NULL")


def upgrade() -> None:
    """upgrade schema."""
    op.add_column("post", sa.Column("preview_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("post", sa.Column("preview_message_id", sa.BigInteger(), nullable=True))
    # same one-sided-pair problem as the draft and channel references: half a
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
        postgresql_where=PREVIEW_REFERENCE_IS_SET,
    )


def downgrade() -> None:
    """downgrade schema."""
    op.drop_index(
        "uq_post_preview_message", table_name="post", postgresql_where=PREVIEW_REFERENCE_IS_SET
    )
    op.drop_constraint(op.f("ck_post_preview_ref_complete"), "post", type_="check")
    op.drop_column("post", "preview_message_id")
    op.drop_column("post", "preview_chat_id")
