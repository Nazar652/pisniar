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


ALLOWED_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    PostStatus.DRAFT: frozenset({PostStatus.RESOLVING, PostStatus.ARCHIVED}),
    PostStatus.RESOLVING: frozenset({PostStatus.RESOLVED, PostStatus.FAILED}),
    PostStatus.RESOLVED: frozenset(
        {
            PostStatus.PUBLISHING,
            PostStatus.EDITING,
            PostStatus.DOWNLOADING,
            PostStatus.ARCHIVED,
        }
    ),
    PostStatus.DOWNLOADING: frozenset({PostStatus.DOWNLOADED, PostStatus.FAILED}),
    PostStatus.DOWNLOADED: frozenset({PostStatus.EDITING, PostStatus.RENDERING, PostStatus.FAILED}),
    PostStatus.EDITING: frozenset({PostStatus.RENDERING, PostStatus.RESOLVED, PostStatus.ARCHIVED}),
    PostStatus.RENDERING: frozenset({PostStatus.RENDERED, PostStatus.FAILED}),
    PostStatus.RENDERED: frozenset(
        {PostStatus.PUBLISHING, PostStatus.EDITING, PostStatus.ARCHIVED}
    ),
    PostStatus.PUBLISHING: frozenset({PostStatus.PUBLISHED, PostStatus.FAILED}),
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

    Also owns `post.last_error`: moving into `FAILED` records `error` there;
    moving anywhere else clears it, so a stale error message never lingers
    once the post has moved past the failure it described.
    """
    current_status = PostStatus(post.status)
    if new_status not in ALLOWED_TRANSITIONS[current_status]:
        raise IllegalStatusTransitionError(current_status, new_status)

    post.last_error = error if new_status is PostStatus.FAILED else None
    post.status = new_status
