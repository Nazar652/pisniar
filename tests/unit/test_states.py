"""The post status state machine: every legal transition succeeds and
mutates `last_error` correctly, every illegal one raises and leaves the post
untouched."""

from __future__ import annotations

import pytest

from pisniar.db.models import Post
from pisniar.db.states import (
    ALLOWED_TRANSITIONS,
    IllegalStatusTransitionError,
    PostStatus,
    set_post_status,
)


def make_post(status: PostStatus, *, last_error: str | None = None) -> Post:
    """A plain, unmapped `Post` instance: `set_post_status()` only mutates
    `.status` / `.last_error` in Python, so no DB session is needed to
    exercise it."""
    return Post(track_id=1, created_by_user_id=1, status=status, last_error=last_error)


ALL_TRANSITION_PAIRS = [
    (current, target) for current in PostStatus for target in PostStatus if current != target
]


@pytest.mark.parametrize(("current", "target"), ALL_TRANSITION_PAIRS)
def test_every_transition_pair_matches_the_allowed_table(
    current: PostStatus, target: PostStatus
) -> None:
    post = make_post(current)
    is_legal = target in ALLOWED_TRANSITIONS[current]

    if is_legal:
        set_post_status(post, target)
        assert post.status == target
    else:
        with pytest.raises(IllegalStatusTransitionError) as excinfo:
            set_post_status(post, target)
        assert excinfo.value.current == current
        assert excinfo.value.requested == target
        assert post.status == current, "an illegal transition must not mutate the post"


def test_archived_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[PostStatus.ARCHIVED] == frozenset()


def test_every_status_can_reach_archived_directly_or_not() -> None:
    # Not every status has a direct edge to ARCHIVED (e.g. DOWNLOADING,
    # RENDERING do not) — this documents that fact rather than asserting it,
    # so a future change to the table is a conscious edit here too.
    statuses_with_direct_archive_edge = {
        status for status, allowed in ALLOWED_TRANSITIONS.items() if PostStatus.ARCHIVED in allowed
    }
    assert statuses_with_direct_archive_edge == {
        PostStatus.DRAFT,
        PostStatus.RESOLVED,
        PostStatus.EDITING,
        PostStatus.RENDERED,
        PostStatus.PUBLISHED,
        PostStatus.FAILED,
    }


def test_moving_into_failed_records_the_error() -> None:
    post = make_post(PostStatus.RESOLVING)

    set_post_status(post, PostStatus.FAILED, error="odesli timed out")

    assert post.status == PostStatus.FAILED
    assert post.last_error == "odesli timed out"


def test_moving_into_failed_without_an_error_message_clears_it() -> None:
    post = make_post(PostStatus.RESOLVING, last_error="stale")

    set_post_status(post, PostStatus.FAILED)

    assert post.last_error is None


def test_leaving_failed_clears_the_error() -> None:
    post = make_post(PostStatus.FAILED, last_error="odesli timed out")

    set_post_status(post, PostStatus.RESOLVING)

    assert post.status == PostStatus.RESOLVING
    assert post.last_error is None


def test_a_non_failed_transition_never_sets_last_error() -> None:
    post = make_post(PostStatus.DRAFT)

    set_post_status(post, PostStatus.RESOLVING, error="ignored, not moving into FAILED")

    assert post.last_error is None
