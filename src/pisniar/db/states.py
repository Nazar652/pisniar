"""The post status state machine.

This is the single source of truth for legal `post.status` transitions.
`set_post_status()` is the only sanctioned way to change a post's status in
application code — it is where "no ad-hoc status strings" is enforced. The
database CHECK constraint (see the `0001_initial` migration) only guards
that `status` is one of these values; it cannot see the previous value, so
transition legality lives here, not in SQL.

The full machine is defined now, including states unreachable in step 1
(`downloading`, `rendering`, ...), so steps 2-3 extend behaviour rather than
inventing new status strings ad hoc.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pisniar.db.models import Post


class PostStatus(StrEnum):
    DRAFT = "draft"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    EDITING = "editing"
    RENDERING = "rendering"
    RENDERED = "rendered"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    ARCHIVED = "archived"


# RESOLVING/DOWNLOADING/RENDERING/PUBLISHING each carry a self-edge and a
# direct edge to ARCHIVED. Without the self-edge, a Celery redelivery after a
# worker died mid-task (OOM, SIGKILL, a Docker Desktop crash per D-15) calls
# `set_post_status(post, DOWNLOADING)` again on a post already sitting in
# DOWNLOADING and gets `IllegalStatusTransitionError` — with `task_acks_late`
# meaning at-least-once delivery, this is not a hypothetical. Without the
# ARCHIVED edge, there is no way out of a stuck in-progress post short of a
# manual `UPDATE` in psql, which bypasses `set_post_status()` entirely.
ALLOWED_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    PostStatus.DRAFT: frozenset({PostStatus.RESOLVING, PostStatus.ARCHIVED}),
    PostStatus.RESOLVING: frozenset(
        {PostStatus.RESOLVED, PostStatus.FAILED, PostStatus.RESOLVING, PostStatus.ARCHIVED}
    ),
    PostStatus.RESOLVED: frozenset(
        {
            PostStatus.PUBLISHING,
            PostStatus.EDITING,
            PostStatus.DOWNLOADING,
            PostStatus.ARCHIVED,
        }
    ),
    PostStatus.DOWNLOADING: frozenset(
        {PostStatus.DOWNLOADED, PostStatus.FAILED, PostStatus.DOWNLOADING, PostStatus.ARCHIVED}
    ),
    PostStatus.DOWNLOADED: frozenset({PostStatus.EDITING, PostStatus.RENDERING, PostStatus.FAILED}),
    PostStatus.EDITING: frozenset({PostStatus.RENDERING, PostStatus.RESOLVED, PostStatus.ARCHIVED}),
    PostStatus.RENDERING: frozenset(
        {PostStatus.RENDERED, PostStatus.FAILED, PostStatus.RENDERING, PostStatus.ARCHIVED}
    ),
    PostStatus.RENDERED: frozenset(
        {PostStatus.PUBLISHING, PostStatus.EDITING, PostStatus.ARCHIVED}
    ),
    PostStatus.PUBLISHING: frozenset(
        {PostStatus.PUBLISHED, PostStatus.FAILED, PostStatus.PUBLISHING, PostStatus.ARCHIVED}
    ),
    PostStatus.PUBLISHED: frozenset({PostStatus.ARCHIVED}),
    PostStatus.FAILED: frozenset(
        {
            PostStatus.RESOLVING,
            PostStatus.PUBLISHING,
            PostStatus.RENDERING,
            PostStatus.DOWNLOADING,
            PostStatus.ARCHIVED,
        }
    ),
    PostStatus.ARCHIVED: frozenset(),
}


class IllegalStatusTransitionError(ValueError):
    """Raised when `set_post_status()` is asked for a transition not in the table."""

    def __init__(self, current: PostStatus, requested: PostStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"illegal post status transition: {current} -> {requested}")


def set_post_status(post: Post, new_status: PostStatus, *, error: str | None = None) -> None:
    """Move `post.status` to `new_status`, or raise if the transition is illegal.

    Also owns `post.last_error`: moving into `FAILED` records `error` there.
    Moving out of `FAILED` into a working state clears it (a fresh retry
    should not carry a stale error message). Moving `FAILED -> ARCHIVED`
    deliberately leaves it untouched — per D-9, `last_error` on an archived
    post is the only trace of why it failed, since there is no
    `post_status_history` table.

    Treats `post.status is None` as `DRAFT`: `Base` is a plain
    `DeclarativeBase`, not `MappedAsDataclass`, so `mapped_column(default=...)`
    is a Core-level INSERT default — it is only applied at `flush()`, and a
    freshly constructed, not-yet-flushed `Post()` genuinely has `status is
    None` in Python before that. Without this, "create a post and
    immediately transition it" (the exact pattern step 2 needs) raises
    `ValueError: None is not a valid PostStatus` from `PostStatus(None)`
    instead of the intended `IllegalStatusTransitionError`.
    """
    current_status = PostStatus(post.status) if post.status is not None else PostStatus.DRAFT
    if new_status not in ALLOWED_TRANSITIONS[current_status]:
        raise IllegalStatusTransitionError(current_status, new_status)

    if new_status is PostStatus.FAILED:
        post.last_error = error
    elif current_status is PostStatus.FAILED and new_status is not PostStatus.ARCHIVED:
        post.last_error = None

    post.status = new_status
