"""Whether a post's preview was cut with the fragment the post holds now. A leaf: the bot and the Mini App read
the same rule here.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Render


async def preview_fragment_bounds(session: AsyncSession, post: Post) -> tuple[int, int] | None:
    """the bounds the post's preview was rendered with; `None` without a preview or for a row that predates them."""
    if post.preview_params_hash is None:
        return None
    row = (
        await session.execute(
            select(Render.fragment_start_ms, Render.fragment_end_ms).where(
                Render.post_id == post.id, Render.params_hash == post.preview_params_hash
            )
        )
    ).one_or_none()
    if row is None or row.fragment_start_ms is None or row.fragment_end_ms is None:
        return None
    return row.fragment_start_ms, row.fragment_end_ms


def preview_is_stale(
    start_ms: int | None,
    end_ms: int | None,
    preview_bounds: tuple[int, int] | None,
) -> bool:
    """true only when the preview's recorded bounds exist and differ from the post's; unknown is never stale."""
    if preview_bounds is None or start_ms is None or end_ms is None:
        return False
    return preview_bounds != (start_ms, end_ms)
