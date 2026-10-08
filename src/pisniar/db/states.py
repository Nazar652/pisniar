"""The post status state machine — single source of truth for legal
`post.status` transitions. The DB CHECK only guards valid values, not
transition legality (it can't see the previous value); that lives here, via
`set_post_status()`, the only sanctioned way to change a post's status.
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


# self-edges (Celery at-least-once redelivery) + ARCHIVED edges (manual
# escape hatch) on the four in-progress states.
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
        {PostStatus.PUBLISHING, PostStatus.EDITING, PostStatus.RENDERING, PostStatus.ARCHIVED}
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
    def __init__(self, current: PostStatus, requested: PostStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"illegal post status transition: {current} -> {requested}")


def set_post_status(post: Post, new_status: PostStatus, *, error: str | None = None) -> None:
    """FAILED -> ARCHIVED keeps `last_error`/`failed_from_status`.
    `post.status is None` (an unflushed `Post()`) counts as DRAFT."""
    current_status = PostStatus(post.status) if post.status is not None else PostStatus.DRAFT
    if new_status not in ALLOWED_TRANSITIONS[current_status]:
        raise IllegalStatusTransitionError(current_status, new_status)

    if new_status is PostStatus.FAILED:
        post.last_error = error
        post.failed_from_status = current_status
    elif current_status is PostStatus.FAILED and new_status is not PostStatus.ARCHIVED:
        post.last_error = None
        post.failed_from_status = None

    post.status = new_status
