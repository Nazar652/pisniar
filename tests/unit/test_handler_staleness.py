"""The `RESOLVING` window of `handlers.stage_has_gone_stale()`: the only exit from `RESOLVING` after a worker dies
mid-task. Pure clock arithmetic, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pisniar.bot.handlers import stage_has_gone_stale
from pisniar.db.models import Post
from pisniar.worker.celery_app import TASK_TIME_LIMIT_SECONDS


def make_post_updated_seconds_ago(seconds: float) -> Post:
    post = Post(track_id=1, created_by_user_id=111)
    post.updated_at = datetime.now(UTC) - timedelta(seconds=seconds)
    return post


def test_a_post_younger_than_the_celery_hard_limit_is_not_stale() -> None:
    """a live task is always fresher than the hard limit that kills it, so `/retry` can never race one into a second
    draft."""
    assert stage_has_gone_stale(make_post_updated_seconds_ago(5), TASK_TIME_LIMIT_SECONDS) is False
    assert (
        stage_has_gone_stale(make_post_updated_seconds_ago(TASK_TIME_LIMIT_SECONDS - 1), TASK_TIME_LIMIT_SECONDS)
        is False
    )


def test_a_post_older_than_the_celery_hard_limit_is_stale() -> None:
    """without this branch a `RESOLVING` post whose worker died could leave only through a manual `UPDATE` in psql."""
    assert (
        stage_has_gone_stale(make_post_updated_seconds_ago(TASK_TIME_LIMIT_SECONDS + 1), TASK_TIME_LIMIT_SECONDS)
        is True
    )
