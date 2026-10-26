"""`db.previews`: the preview's recorded bounds against the post's. Unknown bounds are never stale."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.db.models import Post
from pisniar.db.previews import preview_fragment_bounds, preview_is_stale


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "preview_bounds", "expected"),
    [
        (0, 45_000, (0, 45_000), False),
        (0, 45_000, (0, 30_000), True),
        (5_000, 45_000, (0, 45_000), True),
        (0, 45_000, None, False),
        (None, None, (0, 45_000), False),
        (None, 45_000, (0, 45_000), False),
    ],
)
def test_a_preview_is_stale_only_when_its_known_bounds_differ(
    start_ms: int | None,
    end_ms: int | None,
    preview_bounds: tuple[int, int] | None,
    expected: bool,
) -> None:
    assert preview_is_stale(start_ms, end_ms, preview_bounds) is expected


def make_session(row: object) -> Any:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=row)))
    return session


async def test_a_post_without_a_preview_hash_asks_nothing() -> None:
    session = make_session(None)

    assert await preview_fragment_bounds(session, Post(id=1, track_id=1, created_by_user_id=1)) is None
    session.execute.assert_not_called()


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (SimpleNamespace(fragment_start_ms=1_000, fragment_end_ms=9_000), (1_000, 9_000)),
        (SimpleNamespace(fragment_start_ms=None, fragment_end_ms=None), None),
        (None, None),
    ],
    ids=["recorded", "legacy_null", "no_row"],
)
async def test_the_bounds_come_from_the_row_at_the_preview_hash(row: object, expected: tuple[int, int] | None) -> None:
    post = Post(id=1, track_id=1, created_by_user_id=1)
    post.preview_params_hash = "abc"

    assert await preview_fragment_bounds(make_session(row), post) == expected
