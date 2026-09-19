"""Celery task implementations — the `celery -A pisniar.worker.tasks worker`
entrypoint. Importing this reads/validates `Settings`, unlike
`worker.celery_app`, which `bot` (never this module) imports instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from celery.signals import task_postrun, task_prerun
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.config import Settings, get_settings, redaction_secrets
from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.logging import (
    bind_celery_task_id,
    bind_post_id,
    clear_context,
    configure_logging,
    get_logger,
)
from pisniar.post.caption import build_caption
from pisniar.post.render import render_html
from pisniar.resolve import merge
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.http import build_http_client
from pisniar.resolve.registry import ResolveContext
from pisniar.resolve.service import resolve_genres, resolve_track
from pisniar.resolve.types import ArtistInfo, PlatformLink, ResolvedTrack
from pisniar.resolve.urls import UrlKind, classify_url
from pisniar.telegram.client import build_bot
from pisniar.telegram.draft import deliver_draft
from pisniar.telegram.notifier import send_alert
from pisniar.worker.celery_app import PING_TASK_NAME, RESOLVE_POST_TASK_NAME, build_celery_app
from pisniar.worker.runner import run_async

settings = get_settings()
configure_logging(
    log_format=settings.log_format,
    log_level=settings.log_level,
    secrets=redaction_secrets(settings),
)

celery_app = build_celery_app(settings.redis_url)

logger = get_logger(__name__)


# Clears correlation ids around every task, not just `ping` — a stale
# post_id would otherwise leak from one task into the next.
@task_prerun.connect
def _clear_context_before_task(**kwargs: Any) -> None:
    clear_context()


@task_postrun.connect
def _clear_context_after_task(**kwargs: Any) -> None:
    clear_context()


@celery_app.task(name=PING_TASK_NAME, bind=True)
def ping(self: Any, post_id: int | None = None) -> None:
    """Diagnostic task: proves the worker executes, can reach Postgres, and
    can send a Telegram alert independently of the bot process."""
    bind_celery_task_id(self.request.id)
    if post_id is not None:
        bind_post_id(post_id)
    run_async(_ping(post_id))


async def _ping(post_id: int | None) -> None:
    logger.info("ping_task_started", post_id=post_id)

    engine = build_engine(settings.database_url, use_null_pool=True)
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))

        bot = build_bot(settings)
        try:
            delivered = await send_alert(
                bot, settings.alert_chat_id, f"Діагностичний пінг виконано, post_id={post_id}"
            )
        finally:
            await bot.session.close()
    finally:
        await engine.dispose()

    logger.info("ping_task_finished", post_id=post_id, alert_delivered=delivered)


@celery_app.task(name=RESOLVE_POST_TASK_NAME, bind=True)
def resolve_post(self: Any, post_id: int) -> None:
    """Resolves `post_id`'s track, builds and delivers the draft caption.
    Errors are terminal here — FAILED + alert + manual `/retry`,
    this task never asks Celery itself to retry."""
    bind_celery_task_id(self.request.id)
    bind_post_id(post_id)
    run_async(_resolve_post(post_id))


async def _resolve_post(post_id: int) -> None:
    engine = build_engine(settings.database_url, use_null_pool=True)
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            # Both rows locked for the whole task: the delivery guard and the
            # `platform_links` merge are read-then-write pairs (F-9/F-3).
            post = await session.get(Post, post_id, with_for_update=True)
            if post is None:
                logger.error("resolve_post_post_not_found", post_id=post_id)
                return
            track = await session.get(Track, post.track_id, with_for_update=True)
            assert track is not None

            if post.status is not PostStatus.RESOLVING:
                # Stale Celery redelivery: work already done (or post moved
                # on) — only idempotent delivery is still worth attempting.
                await _redeliver_if_resolved(session, post)
                return

            await _resolve_and_deliver(session_factory, session, post, track)
    finally:
        await engine.dispose()


async def _redeliver_if_resolved(session: AsyncSession, post: Post) -> None:
    if post.status is not PostStatus.RESOLVED:
        return

    # Read before any rollback below: that expires every ORM object, and the
    # next attribute access would be a lazy refresh -> MissingGreenlet.
    post_id = post.id
    bot = build_bot(settings)
    try:
        await deliver_draft(bot, post, post.created_by_user_id)
        await session.commit()
    except Exception as error:
        logger.exception("resolve_post_redelivery_failed", post_id=post_id)
        await session.rollback()

        # RESOLVED has no edge back to RESOLVING, so a silent failure here
        # would strand the post with no draft and no way out (F-14).
        await send_alert(
            bot,
            settings.alert_chat_id,
            f"Пост {post_id} уже в стані «resolved», але чернетку доставити не вдалося: {error}",
        )
    finally:
        await bot.session.close()


async def _resolve_and_deliver(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    post: Post,
    track: Track,
) -> None:
    classified = classify_url(track.source_url_normalized)
    if classified.kind is not UrlKind.TRACK:
        await _clean_up_non_track(session, post, track)
        return

    nothing_usable = False
    delivered = False
    http_client = build_http_client()
    try:
        context = _build_resolve_context(http_client, session_factory, settings)
        try:
            resolved = await resolve_track(context, classified)
        except Exception as error:
            await _fail_post(session, post, str(error))
            return

        if not resolved.title and not resolved.artists and not resolved.platform_links:
            nothing_usable = True
        else:
            genres = await resolve_genres(context, resolved)
            _persist_track(track, resolved)

            document, warnings = build_caption(
                artists=resolved.artists,
                title=resolved.title or "",
                year=resolved.year,
                release_type=resolved.release_type,
                release_title=resolved.release_title,
                album_id_spotify=resolved.album_id_spotify,
                genres=genres,
                platform_links=resolved.platform_links,
            )
            for warning in [*resolved.warnings, *warnings]:
                logger.warning("resolve_post_warning", post_id=post.id, warning=warning)

            post.caption = render_html(document)
            post.genres = [
                {"name": genre.name, "hashtag": genre.hashtag, "source": genre.source}
                for genre in genres
            ]

            # Delivery first, RESOLVED after (D-38): RESOLVED means "the
            # draft is in the admin's chat", and RESOLVING -> FAILED is legal
            # while RESOLVED -> FAILED deliberately is not.
            bot = build_bot(settings)
            try:
                await deliver_draft(bot, post, post.created_by_user_id)
            finally:
                await bot.session.close()

            set_post_status(post, PostStatus.RESOLVED)
            await session.commit()
            delivered = True
    except Exception as error:
        await _fail_post(session, post, str(error))
    finally:
        await http_client.aclose()

    # Both outside the `try`: they run after the post reached its final
    # state, and must not land in `_fail_post` on an already-gone row (F-12).
    if nothing_usable:
        await _clean_up_non_track(session, post, track)
    elif delivered:
        await _warn_about_isrc_duplicate(session, post, track)


def _build_resolve_context(
    http_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> ResolveContext:
    return ResolveContext(
        http_client=http_client,
        cache=SourceCache(
            session_factory=session_factory, ttl_seconds=settings.source_fetch_ttl_seconds
        ),
        spotify_client_id=settings.spotify_client_id,
        spotify_client_secret=settings.spotify_client_secret.get_secret_value(),
        itunes_country=settings.itunes_country,
        musicbrainz_contact=settings.musicbrainz_contact,
    )


def _persist_track(track: Track, resolved: ResolvedTrack) -> None:
    """Merges into the stored row instead of overwriting it (D-42): a
    `manual` link and an admin's `artists[].hashtag` are the only data here
    that a re-resolve can never reproduce on its own."""
    merge.merge_stored_platform_links(resolved, track.platform_links)
    merge.merge_stored_artists(resolved, track.artists)

    track.isrc = resolved.isrc
    track.title = resolved.title
    track.artists = [_artist_to_dict(artist) for artist in resolved.artists]
    track.year = resolved.year
    track.release_type = resolved.release_type
    track.release_title = resolved.release_title
    track.cover_url = resolved.cover_url
    track.platform_links = {
        platform: _platform_link_to_dict(link) for platform, link in resolved.platform_links.items()
    }
    track.resolved_at = datetime.now(UTC)


def _artist_to_dict(artist: ArtistInfo) -> dict[str, object]:
    return {"name": artist.name, "hashtag": artist.hashtag, "spotify_id": artist.spotify_id}


def _platform_link_to_dict(link: PlatformLink) -> dict[str, object]:
    return {"url": link.url, "source": link.source, "match": link.match.value}


async def _warn_about_isrc_duplicate(session: AsyncSession, post: Post, track: Track) -> None:
    """M23/D-41: the same recording arrived through a second platform's URL,
    which the `source_url_normalized` check alone can never notice."""
    if track.isrc is None:
        return

    duplicate = await session.scalar(
        select(Track)
        .where(Track.isrc == track.isrc, Track.id != track.id)
        .order_by(Track.id)
        .limit(1)
    )
    if duplicate is None:
        return

    logger.warning(
        "resolve_post_isrc_duplicate",
        post_id=post.id,
        track_id=track.id,
        duplicate_track_id=duplicate.id,
    )
    bot = build_bot(settings)
    try:
        await send_alert(
            bot,
            settings.alert_chat_id,
            f"Цей трек уже є в базі за ISRC {track.isrc}. "
            f"Новий пост {post.id}; попередній трек #{duplicate.id}: {duplicate.source_url}",
        )
    finally:
        await bot.session.close()


async def _fail_post(session: AsyncSession, post: Post, error_message: str) -> None:
    logger.error("resolve_post_failed", post_id=post.id, error=error_message)
    set_post_status(post, PostStatus.FAILED, error=error_message)
    await session.commit()

    bot = build_bot(settings)
    try:
        await send_alert(
            bot,
            settings.alert_chat_id,
            f"Не вдалося обробити пост {post.id}: {error_message}\n"
            f"Адмін може повторити командою /retry {post.id}",
        )
    finally:
        await bot.session.close()


async def _clean_up_non_track(session: AsyncSession, post: Post, track: Track) -> None:
    """A syntactically-track-shaped URL that resolved to nothing usable —
    remove the post, then the track if no other post still references it."""
    logger.warning("resolve_post_cleaned_up_non_track", post_id=post.id, track_id=track.id)
    post_id, track_id, source_url = post.id, track.id, track.source_url

    await session.delete(post)
    await session.commit()

    # Re-taken after that commit released it: the lock is what blocks a
    # concurrent `INSERT INTO post` this COUNT would otherwise not see.
    await session.execute(select(Track.id).where(Track.id == track_id).with_for_update())
    remaining_posts = await session.scalar(
        select(func.count()).select_from(Post).where(Post.track_id == track_id)
    )
    if remaining_posts == 0:
        await session.delete(track)
        try:
            await session.commit()
        except IntegrityError:
            # ON DELETE RESTRICT fired: the track is still needed by another
            # post. Expected outcome, not a failure of this task (F-12).
            await session.rollback()
            logger.info("resolve_post_track_still_referenced", track_id=track_id)

    bot = build_bot(settings)
    try:
        await send_alert(
            bot,
            settings.alert_chat_id,
            f"Не вдалося розпізнати трек за посиланням, запис видалено. "
            f"post_id={post_id}, url={source_url}",
        )
    finally:
        await bot.session.close()
