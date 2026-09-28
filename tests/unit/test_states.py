"""The post status state machine: every legal transition succeeds and
mutates `last_error`/`failed_from_status` correctly, every illegal one raises
and leaves the post untouched."""

from __future__ import annotations

import pytest

from pisniar.db.models import Post
from pisniar.db.states import (
    ALLOWED_TRANSITIONS,
    IllegalStatusTransitionError,
    PostStatus,
    set_post_status,
)


def make_post(
    status: PostStatus,
    *,
    last_error: str | None = None,
    failed_from_status: PostStatus | None = None,
) -> Post:
    """Unmapped: `set_post_status()` only mutates attributes in Python, so no
    session is needed."""
    return Post(
        track_id=1,
        created_by_user_id=1,
        status=status,
        last_error=last_error,
        failed_from_status=failed_from_status,
    )


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
    # Documents which statuses have a direct edge to ARCHIVED (DOWNLOADED does
    # not) so a change to the table is a conscious edit here too.
    statuses_with_direct_archive_edge = {
        status for status, allowed in ALLOWED_TRANSITIONS.items() if PostStatus.ARCHIVED in allowed
    }
    assert statuses_with_direct_archive_edge == {
        PostStatus.DRAFT,
        PostStatus.RESOLVING,
        PostStatus.RESOLVED,
        PostStatus.DOWNLOADING,
        PostStatus.EDITING,
        PostStatus.RENDERING,
        PostStatus.RENDERED,
        PostStatus.PUBLISHING,
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


def test_unflushed_post_with_no_status_set_is_treated_as_draft() -> None:
    """`mapped_column(default=...)` is a Core INSERT default, so `status` stays
    `None` until `flush()`; `Base` is a plain `DeclarativeBase`."""
    post = Post(track_id=1, created_by_user_id=1)
    assert post.status is None

    set_post_status(post, PostStatus.RESOLVING)

    assert post.status == PostStatus.RESOLVING


def test_unflushed_post_rejects_an_illegal_transition_from_the_implied_draft() -> None:
    post = Post(track_id=1, created_by_user_id=1)

    with pytest.raises(IllegalStatusTransitionError) as excinfo:
        set_post_status(post, PostStatus.RESOLVED)

    assert excinfo.value.current == PostStatus.DRAFT


def test_failed_to_archived_preserves_the_error() -> None:
    """`last_error` on an archived post is the only trace left of why it failed:
    there is no `post_status_history` table."""
    post = make_post(PostStatus.FAILED, last_error="odesli timed out")

    set_post_status(post, PostStatus.ARCHIVED)

    assert post.status == PostStatus.ARCHIVED
    assert post.last_error == "odesli timed out"


@pytest.mark.parametrize(
    "stage",
    [PostStatus.RESOLVING, PostStatus.DOWNLOADING, PostStatus.RENDERING, PostStatus.PUBLISHING],
)
def test_moving_into_failed_records_the_stage_it_failed_from(stage: PostStatus) -> None:
    """DOWNLOADING and RENDERING sit between the draft and publication, and every
    stage leaves `draft_message_id` set, so the draft alone cannot tell them apart."""
    post = make_post(stage)

    set_post_status(post, PostStatus.FAILED, error="ffmpeg exited 1")

    assert post.failed_from_status == stage


def test_leaving_failed_clears_the_stage() -> None:
    post = make_post(PostStatus.FAILED, failed_from_status=PostStatus.RENDERING)

    set_post_status(post, PostStatus.RENDERING)

    assert post.failed_from_status is None


def test_failed_to_archived_preserves_the_stage() -> None:
    """Same reason `last_error` survives archiving: with no
    `post_status_history` table, these two columns are the whole record."""
    post = make_post(PostStatus.FAILED, failed_from_status=PostStatus.DOWNLOADING)

    set_post_status(post, PostStatus.ARCHIVED)

    assert post.failed_from_status == PostStatus.DOWNLOADING


def test_a_non_failed_transition_never_sets_the_stage() -> None:
    post = make_post(PostStatus.RESOLVED)

    set_post_status(post, PostStatus.DOWNLOADING)

    assert post.failed_from_status is None


@pytest.mark.parametrize(
    "status",
    [PostStatus.RESOLVING, PostStatus.DOWNLOADING, PostStatus.RENDERING, PostStatus.PUBLISHING],
)
def test_self_edge_allows_an_at_least_once_redelivery(status: PostStatus) -> None:
    """A Celery redelivery (`task_acks_late`, a worker killed mid-task) sets the
    status the post is already in, and that must not raise."""
    post = make_post(status)

    set_post_status(post, status)

    assert post.status == status
