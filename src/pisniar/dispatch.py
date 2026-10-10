"""Putting a task on the queue by name and failing a post whose task never got there.

A leaf shared by the bot and the Mini App: it imports neither of them, and no task function.
"""

from __future__ import annotations

import asyncio

from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import Post
from pisniar.db.session import session_scope
from pisniar.db.states import PostStatus, set_post_status
from pisniar.logging import get_logger

logger = get_logger(__name__)

RENDER_DISPATCH_FAILED_ERROR = "не вдалося поставити задачу рендера відео в чергу"


async def mark_dispatch_failure(
    session_factory: async_sessionmaker[AsyncSession],
    post_id: int,
    *,
    expected_status: PostStatus,
    error: str,
) -> None:
    """the post is committed as in-progress with no task to move it, so the
    bot's own "спробуйте /retry" advice would be false until it goes stale."""
    try:
        async with session_scope(session_factory) as session:
            post = await session.get(Post, post_id, with_for_update=True)
            if post is None or post.status is not expected_status:
                return

            set_post_status(post, PostStatus.FAILED, error=error)
    except Exception:
        logger.exception("dispatch_failure_status_write_failed", post_id=post_id)


async def dispatch_task(
    celery_client: Celery,
    celery_task_name: str,
    task_kwargs: dict[str, int],
    failure_event: str,
    **failure_fields: object,
) -> bool:
    """by name: the caller never imports `worker.tasks`. `send_task()` blocks, so it runs
    off-loop, or an unreachable Redis stalls this single-loop process."""
    try:
        await asyncio.to_thread(celery_client.send_task, celery_task_name, kwargs=task_kwargs)
    except Exception:
        logger.exception(failure_event, **failure_fields)
        return False
    return True
