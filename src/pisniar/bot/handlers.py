"""Command handlers.

Reachable only by admins — `AdminAllowlistMiddleware` has already filtered
everything else out by the time a handler runs.
"""

from __future__ import annotations

import uuid

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import Post, Track
from pisniar.db.session import session_scope
from pisniar.logging import bind_post_id, get_logger
from pisniar.worker.celery_app import PING_TASK_NAME

logger = get_logger(__name__)

router = Router(name="commands")


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    await message.answer("pisniar bot is up. Use /diag to run an end-to-end diagnostic.")


@router.message(Command("diag"))
async def handle_diag(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    celery_client: Celery,
) -> None:
    """End-to-end diagnostic: create a track+post, dispatch a Celery task, reply.

    Purely diagnostic dummy data — no track resolution, no real pipeline
    logic. Temporary by design (assumption A7): drop this handler once step 2
    has real handlers exercising the same path for real.
    """
    if message.from_user is None:
        return

    diag_marker = uuid.uuid4().hex
    source_url = f"diag://{diag_marker}"

    async with session_scope(session_factory) as session:
        track = Track(source_url=source_url, source_url_normalized=source_url)
        session.add(track)
        await session.flush()

        post = Post(track_id=track.id, created_by_user_id=message.from_user.id)
        session.add(post)
        await session.flush()

        track_id = track.id
        post_id = post.id

    bind_post_id(post_id)
    logger.info("diag_started", track_id=track_id, post_id=post_id)

    async_result = celery_client.send_task(PING_TASK_NAME, kwargs={"post_id": post_id})

    await message.answer(
        f"diag started\ntrack_id={track_id}\npost_id={post_id}\ncelery_task_id={async_result.id}"
    )
