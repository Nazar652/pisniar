"""The two row locks every writer of post and track data takes, and in which mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pisniar.db.locks import lock_post_without_waiting, lock_track_for_write
from pisniar.db.models import Post, Track


async def test_the_post_is_locked_for_update_without_waiting() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value="row")

    assert await lock_post_without_waiting(session, 7) == "row"

    session.get.assert_awaited_once_with(Post, 7, with_for_update={"nowait": True})


async def test_the_track_is_locked_for_no_key_update() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=None)

    assert await lock_track_for_write(session, 3) is None

    session.get.assert_awaited_once_with(Track, 3, with_for_update={"key_share": True})
