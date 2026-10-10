"""`/retry`'s stage routing and the staleness windows of the media in-progress
states. Pure functions over a `Post`, no database: the intent `/retry` picks is
decided here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pisniar.bot import handlers
from pisniar.bot.handlers import (
    RetryBranch,
    retry_branch_for_failed_post,
    stage_has_gone_stale,
    stage_time_limit_seconds,
)
from pisniar.db.models import Post
from pisniar.db.states import PostStatus, set_post_status
from pisniar.worker.celery_app import TASK_TIME_LIMIT_SECONDS


def failed_from(stage: PostStatus | None, *, with_draft: bool = True) -> Post:
    post = Post(track_id=1, created_by_user_id=111)
    post.status = PostStatus.FAILED
    post.failed_from_status = stage
    if with_draft:
        post.draft_chat_id, post.draft_message_id = 555, 41
    return post


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (PostStatus.RESOLVING, RetryBranch.RESOLVE),
        (PostStatus.DOWNLOADING, RetryBranch.DOWNLOAD),
        (PostStatus.DOWNLOADED, RetryBranch.RENDER),
        (PostStatus.EDITING, RetryBranch.RENDER),
        (PostStatus.RENDERING, RetryBranch.RENDER),
        (PostStatus.PUBLISHING, RetryBranch.REPUBLISH),
    ],
)
def test_the_branch_is_read_from_the_recorded_stage(
    stage: PostStatus, expected: RetryBranch
) -> None:
    assert retry_branch_for_failed_post(failed_from(stage)) is expected


def test_a_post_that_failed_while_downloading_is_never_republished() -> None:
    """the post has a draft, so the draft rule alone would hand it to `publish_post`
    and put the cover photo in the channel for a video that never rendered."""
    assert retry_branch_for_failed_post(failed_from(PostStatus.DOWNLOADING)) is not (
        RetryBranch.REPUBLISH
    )


def test_a_post_that_failed_while_rendering_is_never_republished() -> None:
    assert retry_branch_for_failed_post(failed_from(PostStatus.RENDERING)) is not (
        RetryBranch.REPUBLISH
    )


def test_rows_without_a_stored_stage_route_by_the_draft() -> None:
    """rows written before the column existed carry no stage, and for them the
    `draft_message_id` rule is still exactly right."""
    assert retry_branch_for_failed_post(failed_from(None)) is RetryBranch.REPUBLISH
    assert retry_branch_for_failed_post(failed_from(None, with_draft=False)) is RetryBranch.RESOLVE


def test_set_post_status_records_the_stage_every_branch_reads() -> None:
    """the routing above is only as good as the column behind it: without
    this write, every failed post would be routed by its draft alone."""
    post = Post(track_id=1, created_by_user_id=111)
    set_post_status(post, PostStatus.RESOLVING)
    set_post_status(post, PostStatus.RESOLVED)
    set_post_status(post, PostStatus.DOWNLOADING)

    set_post_status(post, PostStatus.FAILED, error="boom")

    assert post.failed_from_status is PostStatus.DOWNLOADING
    assert retry_branch_for_failed_post(post) is RetryBranch.DOWNLOAD


def test_every_branch_targets_a_legal_transition_out_of_failed() -> None:
    for branch in RetryBranch:
        post = failed_from(PostStatus.PUBLISHING)
        set_post_status(post, handlers.RETRY_TARGET_STATUS[branch])


def test_every_stale_stage_has_an_answer_a_limit_and_a_self_edge() -> None:
    """a stage with no exit strands a post; a stage with no sentence leaves the
    admin decoding a state name."""
    for status, branch in handlers.STALE_STAGE_BRANCH.items():
        assert status in handlers.STALE_STAGE_ANSWER
        assert status in handlers.STALE_STAGE_REFUSAL_REASON
        assert stage_time_limit_seconds(status) > 0

        post = Post(track_id=1, created_by_user_id=111)
        post.status = status
        set_post_status(post, handlers.RETRY_TARGET_STATUS[branch])


def test_the_bot_in_progress_sentences_promise_no_preview() -> None:
    """a prepare-path download or a post waiting for its choice is followed by no preview."""
    sentences = [
        handlers.MEDIA_IN_PROGRESS_ANSWER,
        handlers.STALE_STAGE_ANSWER[PostStatus.DOWNLOADING],
    ]
    assert all("прев" not in sentence for sentence in sentences)


def test_a_post_awaiting_a_human_is_not_a_stale_stage() -> None:
    """`editing` waits for the admin's fragment choice, not for a worker."""
    assert PostStatus.EDITING not in handlers.STALE_STAGE_BRANCH


def test_the_media_stages_are_measured_against_their_own_limits() -> None:
    """the resolve window is `worker.celery_app`'s constant and the media ones come
    from `Settings`, so raising one never widens another."""
    assert stage_time_limit_seconds(PostStatus.RESOLVING) == TASK_TIME_LIMIT_SECONDS
    assert (
        stage_time_limit_seconds(PostStatus.DOWNLOADING)
        == handlers.settings.download_task_time_limit_seconds
    )
    assert (
        stage_time_limit_seconds(PostStatus.RENDERING)
        == handlers.settings.render_task_time_limit_seconds
    )
    assert stage_time_limit_seconds(PostStatus.RESOLVING) != (
        stage_time_limit_seconds(PostStatus.RENDERING)
    )


def post_updated_seconds_ago(seconds: float) -> Post:
    post = Post(track_id=1, created_by_user_id=111)
    post.updated_at = datetime.now(UTC) - timedelta(seconds=seconds)
    return post


@pytest.mark.parametrize("status", [PostStatus.DOWNLOADING, PostStatus.RENDERING])
def test_a_stuck_media_stage_older_than_its_limit_has_an_exit(status: PostStatus) -> None:
    """otherwise a `downloading` post whose worker died could leave only through
    a manual `UPDATE` in psql."""
    limit = stage_time_limit_seconds(status)

    assert stage_has_gone_stale(post_updated_seconds_ago(limit + 1), limit) is True


@pytest.mark.parametrize("status", [PostStatus.DOWNLOADING, PostStatus.RENDERING])
def test_a_live_media_task_is_never_reclaimed(status: PostStatus) -> None:
    """celery's hard limit kills a live task at exactly this number, so a running
    one is always fresher."""
    limit = stage_time_limit_seconds(status)

    assert stage_has_gone_stale(post_updated_seconds_ago(5), limit) is False
    assert stage_has_gone_stale(post_updated_seconds_ago(limit - 1), limit) is False


@pytest.mark.parametrize("status", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
def test_the_render_stale_window_follows_the_render_hard_limit(status: PostStatus) -> None:
    """five minutes is inside the render hard limit: a render still waiting on
    LRCLIB must not be reclaimed under a live task."""
    post = Post(track_id=1, created_by_user_id=111)
    post.status = status
    post.updated_at = datetime.now(UTC) - timedelta(seconds=300)

    limit_seconds = stage_time_limit_seconds(status)

    assert limit_seconds == handlers.settings.render_task_time_limit_seconds == 420
    assert not stage_has_gone_stale(post, limit_seconds)
