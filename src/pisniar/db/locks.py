"""Row locks for the code that writes `post` and `track` from outside the worker's long tasks.

Every writer of track data takes the same weak lock the resolve takes, so a second post of the
same track can still be inserted while a writer holds it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Track


async def lock_post_without_waiting(session: AsyncSession, post_id: int) -> Post | None:
    """`FOR UPDATE NOWAIT`: a post a worker holds is refused at once instead of waited for."""
    return await session.get(Post, post_id, with_for_update={"nowait": True})


async def lock_track_for_write(session: AsyncSession, track_id: int) -> Track | None:
    """`FOR NO KEY UPDATE`, bounded by `lock_timeout`; a post of the track can still be inserted."""
    return await session.get(Track, track_id, with_for_update={"key_share": True})
