"""Publishing the cover without a video from the Mini App. The database part locks the post, checks the verdict, the
version the editor opened and that the stored caption is the rebuilt one, and writes `PUBLISHING`; the queue and the
draft's keyboard come after the commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.locks import lock_post_without_waiting
from pisniar.db.models import Track
from pisniar.db.states import PostStatus, set_post_status
from pisniar.dispatch import PUBLISH_DISPATCH_FAILED_ERROR, dispatch_task, mark_dispatch_failure
from pisniar.logging import get_logger
from pisniar.webapp.applying import CAPTION_UNAVAILABLE_MESSAGE
from pisniar.webapp.audio_preparation import draft_of
from pisniar.webapp.editing import STALE_DATA_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import CoverPublicationRequest
from pisniar.webapp.service import rebuild_caption
from pisniar.webapp.status import cover_publication_of
from pisniar.worker.celery_app import PUBLISH_POST_TASK_NAME

logger = get_logger(__name__)

CAPTION_NOT_APPLIED_MESSAGE = "Зміни в редакторі ще не застосовано до чернетки. Натисніть «Зберегти», потім публікуйте."
DISPATCH_FAILED_MESSAGE = "Не вдалося поставити публікацію в чергу. Спробуйте /retry {post_id} у чаті з ботом."


@dataclass(frozen=True)
class StartedCoverPublication:
    """what the committed request leaves for the steps after it: plain values, no ORM row."""

    post_id: int
    draft: tuple[int, int] | None


def refuse(
    status_code: int,
    code: str,
    message: str,
    user_id: int,
    post_id: int,
) -> NoReturn:
    logger.warning("cover_publication_refused", reason=code, user_id=user_id, post_id=post_id)
    raise ApiProblem(status_code, code, message)


async def start_cover_publication(
    session: AsyncSession,
    post_id: int,
    request: CoverPublicationRequest,
    user_id: int,
) -> StartedCoverPublication | None:
    """the caller commits. `None` for a missing post or track; every refusal leaves the rows untouched. The track is
    read without a lock: the caption is only compared."""
    post = await lock_post_without_waiting(session, post_id)
    if post is None:
        return None

    verdict = cover_publication_of(post.status, post.failed_from_status, post.preview_message_id is not None)
    if not verdict.allowed:
        assert verdict.reason is not None
        refuse(409, "not_publishable", verdict.reason, user_id, post_id)
    if post.updated_at != request.expected_post_updated_at:
        refuse(409, "stale_data", STALE_DATA_MESSAGE, user_id, post_id)

    track = await session.get(Track, post.track_id)
    if track is None:
        return None

    _, html = rebuild_caption(post, track)
    if not html:
        refuse(409, "caption_unavailable", CAPTION_UNAVAILABLE_MESSAGE, user_id, post_id)
    if html != post.caption:
        refuse(409, "caption_not_applied", CAPTION_NOT_APPLIED_MESSAGE, user_id, post_id)

    set_post_status(post, PostStatus.PUBLISHING)
    return StartedCoverPublication(post_id, draft_of(post))


async def enqueue_cover_publication(
    celery_client: Celery,
    session_factory: async_sessionmaker[AsyncSession],
    publication: StartedCoverPublication,
    user_id: int,
) -> None:
    """a queue that refuses the task fails the post, or it would sit in `publishing` with nothing to move it."""
    sent = await dispatch_task(
        celery_client,
        PUBLISH_POST_TASK_NAME,
        {"post_id": publication.post_id, "published_by_user_id": user_id},
        "cover_publication_dispatch_failed",
        user_id=user_id,
        post_id=publication.post_id,
    )
    if not sent:
        await mark_dispatch_failure(
            session_factory,
            publication.post_id,
            expected_status=PostStatus.PUBLISHING,
            error=PUBLISH_DISPATCH_FAILED_ERROR,
        )
        refuse(
            503,
            "dispatch_failed",
            DISPATCH_FAILED_MESSAGE.format(post_id=publication.post_id),
            user_id,
            publication.post_id,
        )

    logger.info("cover_publication_dispatched", user_id=user_id, post_id=publication.post_id)
