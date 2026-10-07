"""Celery task implementations — the `celery -A pisniar.worker.tasks worker`
entrypoint. Importing this reads/validates `Settings`, unlike
`worker.celery_app`, which `bot` (never this module) imports instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_postrun, task_prerun
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.channel import warn_if_alerts_share_the_channel
from pisniar.config import Settings, get_settings, redaction_secrets
from pisniar.db.models import MANUAL_FIELD_NAMES, AudioAsset, Post, Render, Track
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.logging import (
    bind_celery_task_id,
    bind_post_id,
    clear_context,
    configure_logging,
    get_logger,
    redact_secrets_in_text,
)
from pisniar.post.caption_input import build_caption_from_rows, genres_from_stored
from pisniar.post.render import render_html
from pisniar.resolve import merge
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.http import build_http_client
from pisniar.resolve.registry import ResolveContext
from pisniar.resolve.service import resolve_genres, resolve_track
from pisniar.resolve.types import (
    USER_INPUT_SOURCE,
    ArtistInfo,
    GenreInfo,
    PlatformLink,
    ResolvedTrack,
)
from pisniar.resolve.urls import UrlKind, classify_url
from pisniar.telegram.client import build_bot
from pisniar.telegram.draft import deliver_draft
from pisniar.telegram.notifier import send_alert
from pisniar.telegram.preview import deliver_preview, remove_draft_keyboard
from pisniar.telegram.publish import (
    channel_post_url,
    describe_publish_error,
    preflight_channel,
    publish_to_channel,
)
from pisniar.telegram.video import RenderedVideo
from pisniar.worker import ass as ass_subtitles
from pisniar.worker import download as audio_download
from pisniar.worker import ffmpeg as media_tools
from pisniar.worker import fragment as fragment_rule
from pisniar.worker import lyrics as worker_lyrics
from pisniar.worker import render as video_render
from pisniar.worker import storage as media_storage
from pisniar.worker.celery_app import (
    DOWNLOAD_POST_AUDIO_TASK_NAME,
    PING_TASK_NAME,
    PUBLISH_POST_TASK_NAME,
    RENDER_POST_VIDEO_TASK_NAME,
    RESOLVE_POST_TASK_NAME,
    build_celery_app,
)
from pisniar.worker.runner import run_async

settings = get_settings()
configure_logging(
    log_format=settings.log_format,
    log_level=settings.log_level,
    secrets=redaction_secrets(settings),
)

celery_app = build_celery_app(settings.redis_url)

logger = get_logger(__name__)

warn_if_alerts_share_the_channel(settings.channel_id, settings.alert_chat_id)

EMPTY_RE_RESOLVE_ERROR = (
    "Резолв не повернув нічого, але трек уже має збережені дані — "
    "нічого не видалено. Спробуйте /retry пізніше (кеш джерел живе годину)."
)

# the same guard fires for a never-resolved YouTube input, where the only
# stored link is the admin's own URL — calling that "збережені дані" lies.
EMPTY_FIRST_RESOLVE_ERROR = (
    "Резолв не повернув нічого. Збережено лише ваше власне посилання, "
    "тому запис не видалено. Спробуйте /retry пізніше (кеш джерел живе годину)."
)

MISSING_TITLE_ERROR = (
    "Резолв не дав назви треку, тому чернетку не доставлено — порожній пост "
    "не є продуктом. Спробуйте /retry пізніше (кеш джерел живе годину)."
)


# clears correlation ids around every task, not just `ping` — a stale
# post_id would otherwise leak from one task into the next.
@task_prerun.connect
def clear_context_before_task(**kwargs: Any) -> None:
    clear_context()


@task_postrun.connect
def clear_context_after_task(**kwargs: Any) -> None:
    clear_context()


@celery_app.task(name=PING_TASK_NAME, bind=True)
def ping(self: Any, post_id: int | None = None) -> None:
    """diagnostic task: proves the worker executes, can reach Postgres, and
    can send a Telegram alert independently of the bot process."""
    bind_celery_task_id(self.request.id)
    if post_id is not None:
        bind_post_id(post_id)
    run_async(perform_ping(post_id))


async def perform_ping(post_id: int | None) -> None:
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
    """errors are terminal: FAILED, an alert and a manual `/retry`; this task
    never asks Celery to retry."""
    bind_celery_task_id(self.request.id)
    bind_post_id(post_id)
    run_async(perform_resolve_post(post_id))


async def perform_resolve_post(post_id: int) -> None:
    engine = build_engine(
        settings.database_url, use_null_pool=True, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            try:
                # locked for the whole task: the delivery guard is a read-then-write
                # pair. Wrapped because `lock_timeout` makes this raise, not wait.
                post = await session.get(Post, post_id, with_for_update=True)
            except DBAPIError as error:
                await session.rollback()
                await fail_post(session, post_id, f"пост заблоковано іншою задачею: {error}")
                return

            if post is None:
                logger.error("resolve_post_post_not_found", post_id=post_id)
                return

            # unlocked on purpose: the track lock is taken just before the write,
            # so a second post of the same track never waits out a whole resolve.
            track = await session.get(Track, post.track_id)
            assert track is not None

            if post.status is not PostStatus.RESOLVING:
                # stale Celery redelivery: work already done (or post moved
                # on) — only idempotent delivery is still worth attempting.
                await redeliver_if_resolved(session, post)
                return

            await resolve_and_deliver(session_factory, session, post, track)
    finally:
        await engine.dispose()


async def redeliver_if_resolved(session: AsyncSession, post: Post) -> None:
    if post.status is not PostStatus.RESOLVED:
        # logged rather than silent: silence is this system's main failure mode.
        logger.info(
            "resolve_post_task_arrived_at_settled_post",
            post_id=post.id,
            status=post.status.value,
        )
        return

    # read before any rollback below: that expires every ORM object, and the
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
        # would strand the post with no draft and no way out.
        await send_alert(
            bot,
            settings.alert_chat_id,
            redacted_text(
                f"Пост {post_id} уже в стані «resolved», але чернетку доставити не вдалося: {error}"
            ),
        )
    finally:
        await bot.session.close()


async def resolve_and_deliver(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    post: Post,
    track: Track,
) -> None:
    classified = classify_url(track.source_url_normalized)
    if classified.kind is not UrlKind.TRACK:
        await clean_up_non_track(session, post, track, [])
        return

    # read before anything can expire the ORM objects: a failed commit rolls
    # back, and `post.id` would then be a lazy refresh -> MissingGreenlet.
    post_id = post.id
    nothing_usable = False
    delivered = False
    source_failures: list[str] = []
    link_rejections: list[str] = []
    http_client = build_http_client()
    try:
        context = build_resolve_context(http_client, session_factory, settings)
        try:
            resolved = await resolve_track(context, classified)
        except Exception as error:
            await fail_post(session, post_id, str(error))
            return

        source_failures = resolved.source_failures
        link_rejections = resolved.link_rejections

        if not resolve_found_anything(resolved):
            if track.resolved_at is not None or track.platform_links:
                # one stale 404 is not proof the track is gone, and deleting
                # here would take the admin's manual data with it.
                empty_error = (
                    EMPTY_RE_RESOLVE_ERROR
                    if track.resolved_at is not None
                    else EMPTY_FIRST_RESOLVE_ERROR
                )
                await fail_post(
                    session, post_id, empty_error + source_failure_note(source_failures)
                )
                return
            nothing_usable = True
        elif not (resolved.title or track.title):
            # a draft with no track name would still mark the post RESOLVED.
            # The stored title counts: `persist_track` merges it back in.
            await fail_post(
                session, post_id, MISSING_TITLE_ERROR + source_failure_note(source_failures)
            )
            return
        else:
            genres = await resolve_genres(context, resolved)

            try:
                await persist_track_in_own_transaction(session_factory, track.id, resolved)
            except DBAPIError as error:
                await fail_post(session, post_id, f"трек заблоковано іншою задачею: {error}")
                return

            # plain re-read, no lock: the write above happened in another
            # session, and `warn_about_isrc_duplicate` reads `track.isrc`.
            await session.refresh(track)

            # `merge_stored_scalars` inside `persist_track` has filled this
            # from the stored row by now — that is what the guard above allows.
            caption_title = resolved.title
            assert caption_title is not None

            # one list for both sinks: `post.genres` keeps a stored value the
            # fresh resolve lost, and the caption must say what that column does.
            effective_genres = genres or genres_from_stored(post.genres)
            post.genres = [genre_to_dict(genre) for genre in effective_genres]

            # read back off the rows, the same way the Mini App rebuilds it
            rebuilt = build_caption_from_rows(
                title=caption_title,
                artists=track.artists,
                year=track.year,
                release_type=track.release_type,
                release_title=track.release_title,
                album_id_spotify=track.album_id_spotify,
                languages=track.languages,
                genres=post.genres,
                platform_links=track.platform_links,
            )
            for warning in [*resolved.warnings, *rebuilt.warnings]:
                logger.warning("resolve_post_warning", post_id=post.id, warning=warning)

            post.caption = render_html(rebuilt.document)

            # delivery first: RESOLVED means "the draft is in the admin's chat",
            # and only RESOLVING, not RESOLVED, can still move to FAILED.
            bot = build_bot(settings)
            try:
                await deliver_draft(bot, post, post.created_by_user_id)
            finally:
                await bot.session.close()

            set_post_status(post, PostStatus.RESOLVED)
            await session.commit()
            delivered = True
    except Exception as error:
        await fail_post(session, post_id, str(error) + source_failure_note(source_failures))
    finally:
        await http_client.aclose()

    # all outside the `try`: they run after the post reached its final
    # state, and must not land in `fail_post` on an already-gone row.
    if nothing_usable:
        await clean_up_non_track(session, post, track, source_failures)
    elif delivered:
        await warn_about_source_failures(post, source_failures, link_rejections)
        await warn_about_isrc_duplicate(session, post, track)


def resolve_found_anything(resolved: ResolvedTrack) -> bool:
    """a `user_input` link is the admin's own URL, put into the result before
    any network call — never evidence that a source answered."""
    resolved_links = [
        link for link in resolved.platform_links.values() if link.source != USER_INPUT_SOURCE
    ]
    return bool(resolved.title or resolved.artists or resolved_links)


def source_failure_note(source_failures: list[str]) -> str:
    if not source_failures:
        return ""
    return "\nДжерела, що не відповіли: " + "; ".join(source_failures)


def build_resolve_context(
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


async def persist_track_in_own_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    track_id: int,
    resolved: ResolvedTrack,
) -> None:
    """the main transaction stays open through delivery (up to 60 s), so the track
    lock lives in this short one. FOR NO KEY UPDATE, so `INSERT INTO post` passes."""
    async with session_factory() as track_session:
        try:
            track = await track_session.get(Track, track_id, with_for_update={"key_share": True})
            assert track is not None

            persist_track(track, resolved)
            await track_session.commit()
        except Exception:
            await track_session.rollback()
            raise


def persist_track(track: Track, resolved: ResolvedTrack) -> None:
    """merges into the stored row: a re-resolve cannot reproduce a `manual` link or
    an admin's hashtag, and a degraded one keeps the stored `title`/`artists`."""
    merge.merge_stored_platform_links(resolved, track.platform_links)
    merge.drop_removed_platforms(resolved, track.removed_platforms)

    # before `merge_stored_artists`, which then re-applies the hashtag
    # overrides onto the restored names — a no-op, since they came from there.
    merge.keep_stored_title_and_artists_after_source_failure(resolved, track.title, track.artists)

    merge.merge_stored_artists(resolved, track.artists)
    merge.merge_stored_scalars(resolved, stored_scalars(track))
    # last: it alone can restore a manually emptied field the steps above refilled
    merge.apply_manual_fields(
        resolved,
        {name: getattr(track, name) for name in MANUAL_FIELD_NAMES},
        track.manual_fields,
    )

    track.isrc = resolved.isrc
    track.title = resolved.title
    track.artists = [artist_to_dict(artist) for artist in resolved.artists]
    track.year = resolved.year
    track.release_type = resolved.release_type
    track.release_title = resolved.release_title
    track.album_id_spotify = resolved.album_id_spotify
    track.cover_url = resolved.cover_url
    track.duration_ms = resolved.duration_ms
    track.platform_links = {
        platform: platform_link_to_dict(link) for platform, link in resolved.platform_links.items()
    }
    track.resolved_at = datetime.now(UTC)


def stored_scalars(track: Track) -> dict[str, object]:
    """`merge` stays free of ORM imports, so the row's scalars are handed
    over as a plain dict."""
    return {name: getattr(track, name) for name in merge.STORED_SCALAR_FIELDS}


def artist_to_dict(artist: ArtistInfo) -> dict[str, object]:
    return {"name": artist.name, "hashtag": artist.hashtag, "spotify_id": artist.spotify_id}


def genre_to_dict(genre: GenreInfo) -> dict[str, object]:
    return {"name": genre.name, "hashtag": genre.hashtag, "source": genre.source}


def platform_link_to_dict(link: PlatformLink) -> dict[str, object]:
    return {"url": link.url, "source": link.source, "match": link.match.value}


async def warn_about_isrc_duplicate(session: AsyncSession, post: Post, track: Track) -> None:
    """the same recording arrived through another platform's URL, which the
    `source_url_normalized` check alone can never notice."""
    if track.isrc is None:
        return

    duplicate = (
        await session.execute(
            select(Track.id, Track.source_url)
            .where(Track.isrc == track.isrc, Track.id != track.id)
            .order_by(Track.id)
            .limit(1)
        )
    ).first()

    # closed before Telegram: otherwise this SELECT's transaction stays open
    # through two `send_alert` calls.
    await session.commit()

    if duplicate is None:
        return
    duplicate_track_id, duplicate_source_url = duplicate

    logger.warning(
        "resolve_post_isrc_duplicate",
        post_id=post.id,
        track_id=track.id,
        duplicate_track_id=duplicate_track_id,
    )
    await alert_admin_and_draft_chat(
        post,
        f"Цей трек уже є в базі за ISRC {track.isrc}. "
        f"Новий пост {post.id}; попередній трек #{duplicate_track_id}: {duplicate_source_url}",
    )


async def warn_about_source_failures(
    post: Post, source_failures: list[str], link_rejections: list[str]
) -> None:
    """source isolation is acceptable only while the loss is audible, so a poorer
    post is reported to the admin, not only to the logs."""
    if not source_failures and not link_rejections:
        return

    lines = [f"Пост {post.id}: чернетку зібрано не повністю."]
    if source_failures:
        lines.append(source_failure_note(source_failures).strip())
        lines.append(
            "/retry зі стану «resolved» недоступний — щоб перезібрати, "
            "надішліть посилання ще раз (кеш джерел живе годину)."
        )
    if link_rejections:
        lines.append("Посилання відхилено: " + "; ".join(link_rejections))

    await alert_admin_and_draft_chat(post, redacted_text("\n".join(lines)))


async def alert_admin_and_draft_chat(post: Post, text_message: str) -> None:
    # also into the draft chat: `alert_chat_id` is only "usually one of the
    # admins" per `.env.example`, and the draft is already there.
    chat_ids = [settings.alert_chat_id]
    if post.draft_chat_id is not None and post.draft_chat_id != settings.alert_chat_id:
        chat_ids.append(post.draft_chat_id)

    bot = build_bot(settings)
    try:
        for chat_id in chat_ids:
            await send_alert(bot, chat_id, text_message)
    finally:
        await bot.session.close()


def redacted_text(text_value: str) -> str:
    """the log pipeline redacts its own event dicts; `send_alert` and
    `post.last_error` are sinks no processor ever sees."""
    return redact_secrets_in_text(text_value, redaction_secrets(settings))


async def fail_post(session: AsyncSession, post_id: int, error_message: str) -> None:
    """takes an id, never a live `Post`: its inputs must survive the rollback a
    failed commit performs, which expires every ORM object in the session."""
    error_message = redacted_text(error_message)
    logger.error("resolve_post_failed", post_id=post_id, error=error_message)

    status_written = True
    try:
        await write_failed_status(session, post_id, error_message)
    except Exception:
        logger.exception("resolve_post_failed_status_write_retried", post_id=post_id)
        try:
            await session.rollback()
            await write_failed_status(session, post_id, error_message)
        except Exception:
            # telegram does not depend on Postgres, so a database that is
            # down for good is when this alert is the only channel.
            logger.exception("resolve_post_failed_status_write_failed", post_id=post_id)
            status_written = False

    alert_text = (
        f"Не вдалося обробити пост {post_id}: {error_message}\n"
        f"Адмін може повторити командою /retry {post_id}"
    )
    if not status_written:
        alert_text += (
            "\nСтатус у базі зафіксувати не вдалося — пост міг лишитись у стані «resolving»."
        )

    bot = build_bot(settings)
    try:
        await send_alert(bot, settings.alert_chat_id, alert_text)
    finally:
        await bot.session.close()


async def write_failed_status(session: AsyncSession, post_id: int, error_message: str) -> None:
    post = await session.get(Post, post_id, with_for_update={"key_share": True})
    if post is None or post.status is PostStatus.FAILED:
        # no `FAILED -> FAILED` edge exists, and a second call into the
        # handler after a failed commit is exactly what would ask for one.
        return

    set_post_status(post, PostStatus.FAILED, error=error_message)
    await session.commit()


async def clean_up_non_track(
    session: AsyncSession, post: Post, track: Track, source_failures: list[str]
) -> None:
    """a track-shaped URL that resolved to nothing usable."""
    logger.warning("resolve_post_cleaned_up_non_track", post_id=post.id, track_id=track.id)
    post_id, source_url = post.id, track.source_url

    post_deleted, cleanup_note = await delete_post_and_orphan_track(session, post, post_id, track)

    head = (
        "Не вдалося розпізнати трек за посиланням, запис видалено."
        if post_deleted
        else "Не вдалося розпізнати трек за посиланням, але прибрати запис не вдалося."
    )
    alert_text = (
        f"{head} post_id={post_id}, url={source_url}"
        f"{cleanup_note}{source_failure_note(source_failures)}"
    )

    bot = build_bot(settings)
    try:
        await send_alert(bot, settings.alert_chat_id, alert_text)
    finally:
        await bot.session.close()


async def delete_post_and_orphan_track(
    session: AsyncSession, post: Post, post_id: int, track: Track
) -> tuple[bool, str]:
    """returns `(post_deleted, note)` instead of raising: like `fail_post`, this
    path must still reach Telegram when Postgres is gone."""
    track_id = track.id
    try:
        await session.delete(post)
        await session.commit()
    except Exception as error:
        logger.exception("resolve_post_cleanup_post_delete_failed", post_id=post_id)
        await rollback_quietly(session, post_id)
        return False, (
            f"\nЗапис у базі прибрати не вдалося — пост міг лишитись "
            f"у стані «resolving»: {redacted_text(str(error))}"
        )

    # after the commit: the reverse order leaves a surviving row pointing at a
    # deleted file, which is worse than an orphan file with no row.
    media_storage.remove_post_artifacts(settings.media_root, post_id)

    try:
        # re-taken after that commit released it: the lock blocks a concurrent
        # `INSERT INTO post` this COUNT would miss. FOR NO KEY UPDATE, as elsewhere.
        await session.execute(
            select(Track.id).where(Track.id == track_id).with_for_update(key_share=True)
        )
        remaining_posts = await session.scalar(
            select(func.count()).select_from(Post).where(Post.track_id == track_id)
        )
        if remaining_posts == 0:
            await session.delete(track)
            try:
                await session.commit()
            except IntegrityError:
                # ON DELETE RESTRICT fired: the track is still needed by
                # another post. Expected outcome, not a failure here.
                await session.rollback()
                logger.info("resolve_post_track_still_referenced", track_id=track_id)
            else:
                media_storage.remove_track_artifacts(settings.media_root, track_id)
        else:
            # committed before Telegram, so the row lock is never held across
            # network calls.
            await session.commit()
    except Exception as error:
        logger.exception("resolve_post_cleanup_track_failed", track_id=track_id)
        await rollback_quietly(session, post_id)
        return True, f"\nТрек #{track_id} прибрати не вдалося: {redacted_text(str(error))}"

    return True, ""


async def rollback_quietly(session: AsyncSession, post_id: int) -> None:
    """a rollback against a dead connection raises too, and here it would
    take the alert with it — the one channel that does not need Postgres."""
    try:
        await session.rollback()
    except Exception:
        logger.exception("resolve_post_cleanup_rollback_failed", post_id=post_id)


@celery_app.task(name=PUBLISH_POST_TASK_NAME, bind=True)
def publish_post(self: Any, post_id: int, published_by_user_id: int) -> None:
    """terminal on error, like `resolve_post`: FAILED, an alert and a manual
    retry, never a Celery retry."""
    bind_celery_task_id(self.request.id)
    bind_post_id(post_id)
    run_async(perform_publish_post(post_id, published_by_user_id))


async def perform_publish_post(post_id: int, published_by_user_id: int) -> None:
    engine = build_engine(
        settings.database_url, use_null_pool=True, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            try:
                # barrier 1: this lock and the two guards under it are the
                # only real deduplication — see AGENTS.md on the other two.
                post = await session.get(Post, post_id, with_for_update=True)
            except DBAPIError as error:
                await session.rollback()
                await fail_publish(
                    session, post_id, None, f"пост заблоковано іншою задачею: {error}"
                )
                return

            if post is None:
                logger.error("publish_post_post_not_found", post_id=post_id)
                return

            if post.channel_message_id is not None:
                logger.info(
                    "publish_post_already_published",
                    post_id=post_id,
                    channel_message_id=post.channel_message_id,
                )
                return

            if post.status is not PostStatus.PUBLISHING:
                logger.info(
                    "publish_post_task_arrived_at_settled_post",
                    post_id=post_id,
                    status=post.status.value,
                )
                return

            track = await session.get(Track, post.track_id)
            assert track is not None

            await publish_and_confirm(session, post, track, published_by_user_id)
    finally:
        await engine.dispose()


def post_reached_rendered(post: Post) -> bool:
    """the status is already PUBLISHING here. `preview_message_id` is the durable
    trace of RENDERED: only the transaction that sets that status writes it."""
    return post.preview_message_id is not None


class RenderedVideoUnavailableError(RuntimeError):
    """loud on purpose: a `send_photo` fallback would publish a different post than
    the one the admin approved, and publication cannot be revoked."""


async def video_for_publication(session: AsyncSession, post: Post) -> RenderedVideo | None:
    """a file alone never qualifies: a failed attempt can leave one behind that
    the admin never previewed."""
    if not post_reached_rendered(post):
        return None

    render_row = (
        (
            await session.execute(
                select(Render).where(Render.post_id == post.id).order_by(Render.updated_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if render_row is None:
        raise RenderedVideoUnavailableError(
            f"пост {post.id} має прев'ю з відео, але рядка render для нього немає — "
            f"публікація фотографією підмінила б те, що схвалив адмін"
        )

    path = Path(render_row.path)
    if media_storage.existing_artifact(path) is None:
        raise RenderedVideoUnavailableError(
            f"файл відео {path} зник із тому — повторіть рендер через /retry {post.id}"
        )

    thumbnail = media_storage.existing_artifact(
        media_storage.thumbnail_file_path(settings.media_root, post.id, render_row.params_hash)
    )
    return RenderedVideo(
        path=path,
        width=video_render.VIDEO_WIDTH,
        height=video_render.VIDEO_HEIGHT,
        duration_ms=render_row.duration_ms,
        thumbnail_path=thumbnail,
    )


async def publish_and_confirm(
    session: AsyncSession, post: Post, track: Track, published_by_user_id: int
) -> None:
    # read before anything can expire the ORM objects: the failure path below
    # rolls back, and a later attribute access would be a lazy refresh.
    post_id = post.id
    draft_chat_id = post.draft_chat_id
    draft_message_id = post.draft_message_id
    preview_reference = (post.preview_chat_id, post.preview_message_id)
    cover_url = track.cover_url

    bot = build_bot(settings)
    try:
        try:
            chat = await preflight_channel(bot, settings.channel_id)

            # inside the try: a post previewed as a video whose file is gone
            # must fail loudly here, never quietly go out as a photo.
            video = await video_for_publication(session, post)

            # barrier 2: send, write, commit, with no network call between.
            # The crash window between send and commit is an accepted risk.
            await publish_to_channel(bot, post, settings.channel_id, cover_url, video)
            channel_message_id = post.channel_message_id
            assert channel_message_id is not None

            post.published_at = datetime.now(UTC)
            post.published_by_user_id = published_by_user_id
            set_post_status(post, PostStatus.PUBLISHED)
            await session.commit()
        except Exception as error:
            logger.exception("publish_post_send_failed", post_id=post_id)

            # rolled back before the handler runs: a half-written channel
            # reference under a `failed` status would be a lie about Telegram.
            try:
                await session.rollback()
            except Exception:
                logger.exception("publish_post_rollback_failed", post_id=post_id)

            await fail_publish(session, post_id, draft_chat_id, describe_publish_error(error))
            return

        logger.info(
            "publish_post_published",
            post_id=post_id,
            channel_message_id=channel_message_id,
            as_video=video is not None,
        )

        # only after the commit and only on success: Telegram holds the copy
        # now, and a rollback would orphan the `published` row.
        if video is not None:
            media_storage.remove_post_artifacts(settings.media_root, post_id)

        await confirm_publication(
            bot,
            post_id,
            draft_chat_id,
            draft_message_id,
            preview_reference,
            channel_post_url(chat, channel_message_id),
        )
    finally:
        await bot.session.close()


async def confirm_publication(
    bot: Bot,
    post_id: int,
    draft_chat_id: int | None,
    draft_message_id: int | None,
    preview_reference: tuple[int | None, int | None],
    post_url: str,
) -> None:
    """only the buttons go away; the channel reference stays for editing the
    caption. Both messages are cleared: the preview has a publish button too."""
    for chat_id, message_id in ((draft_chat_id, draft_message_id), preview_reference):
        if chat_id is None or message_id is None:
            continue

        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except TelegramAPIError:
            # the bot already removed it on the tap; this is the second pass,
            # for the paths (a retry, a redelivery) that never went through one.
            logger.warning(
                "publish_post_keyboard_removal_failed",
                post_id=post_id,
                message_id=message_id,
                exc_info=True,
            )

    chat_id = draft_chat_id if draft_chat_id is not None else settings.alert_chat_id
    await send_alert(bot, chat_id, f"Пост {post_id} опубліковано: {post_url}")


async def fail_publish(
    session: AsyncSession, post_id: int, draft_chat_id: int | None, error_message: str
) -> None:
    """the publish mirror of `fail_post`: plain values only, never a live
    `Post`, so it survives the rollback that expires every ORM object."""
    error_message = redacted_text(error_message)
    logger.error("publish_post_failed", post_id=post_id, error=error_message)

    status_written = True
    try:
        await write_failed_status(session, post_id, error_message)
    except Exception:
        logger.exception("publish_post_failed_status_write_retried", post_id=post_id)
        try:
            await session.rollback()
            await write_failed_status(session, post_id, error_message)
        except Exception:
            logger.exception("publish_post_failed_status_write_failed", post_id=post_id)
            status_written = False

    alert_text = (
        f"Не вдалося опублікувати пост {post_id}: {error_message}\n"
        f"Повторити публікацію: /retry {post_id}"
    )
    if not status_written:
        alert_text += (
            "\nСтатус у базі зафіксувати не вдалося — пост міг лишитись у стані «publishing»."
        )

    chat_ids = [settings.alert_chat_id]
    if draft_chat_id is not None and draft_chat_id != settings.alert_chat_id:
        chat_ids.append(draft_chat_id)

    bot = build_bot(settings)
    try:
        for chat_id in chat_ids:
            await send_alert(bot, chat_id, alert_text)
    finally:
        await bot.session.close()


# legal entries per `db.states` minus FAILED: the bot writes the target status
# before dispatching, so a task that finds FAILED is always a redelivery.
DOWNLOAD_ENTRY_STATUSES = frozenset({PostStatus.RESOLVED, PostStatus.DOWNLOADING})
RENDER_ENTRY_STATUSES = frozenset({PostStatus.DOWNLOADED, PostStatus.EDITING, PostStatus.RENDERING})

MEDIA_TASK_TIMEOUT_ERROR = (
    "перевищено м'який ліміт часу задачі — це ТАЙМАУТ, а не помилка обробки; "
    "діагностувати треба тривалість операції, а не її вміст"
)

RENDER_DISPATCH_FAILED_ERROR = (
    "аудіо завантажено, але задачу рендера не вдалося поставити в чергу — "
    "повторіть через /retry, завантаження вдруге не знадобиться"
)

UNKNOWN_YTDLP_VERSION = "unknown"


@dataclass(frozen=True)
class DownloadClaim:
    """plain values only: the post lock is released before yt-dlp starts, so
    nothing here may be an ORM object that would lazily refresh."""

    post_id: int
    track_id: int
    platform_links: dict[str, object]
    track_duration_ms: int | None


@dataclass(frozen=True)
class DownloadOutcome:
    video_id: str
    path: Path
    size_bytes: int
    duration_ms: int
    # none when a stored artifact was reused — the row keeps the version that
    # actually produced the file instead of the one running now.
    ytdlp_version: str | None


@dataclass(frozen=True)
class RenderClaim:
    post_id: int
    track_id: int
    video_id: str
    audio_path: str
    audio_duration_ms: int
    cover_url: str | None
    fragment_start_ms: int
    fragment_end_ms: int
    lyrics_query: worker_lyrics.LyricsQuery


@dataclass(frozen=True)
class RenderOutcome:
    params_hash: str
    path: Path
    size_bytes: int
    duration_ms: int
    text_version: str | None
    # carried to the preview, so the admin can be told why a video has no text.
    lyrics_reason: str
    # cosmetic and therefore optional: Telegram builds its own preview when
    # it is missing, so a failed thumbnail never fails a verified render.
    thumbnail_path: Path | None = None


@celery_app.task(
    name=DOWNLOAD_POST_AUDIO_TASK_NAME,
    bind=True,
    soft_time_limit=settings.download_task_soft_time_limit_seconds,
    time_limit=settings.download_task_time_limit_seconds,
)
def download_post_audio(self: Any, post_id: int) -> None:
    """own time limits: the resolve limits gate `/retry`, so they stay put."""
    bind_celery_task_id(self.request.id)
    bind_post_id(post_id)
    try:
        run_async(perform_download_post_audio(post_id))
    except SoftTimeLimitExceeded:
        # celery raises it in the main thread, i.e. out of the event loop
        # rather than out of any `await` inside it — so it is caught here too.
        logger.error("download_post_audio_soft_time_limit", post_id=post_id)
        run_async(fail_media_post_by_id(post_id, MEDIA_TASK_TIMEOUT_ERROR))


async def perform_download_post_audio(post_id: int) -> None:
    engine = build_engine(
        settings.database_url, use_null_pool=True, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    temporary_directory = media_storage.task_temporary_directory(settings.media_root, post_id)
    try:
        session_factory = build_session_factory(engine)

        claim = await claim_post_for_downloading(session_factory, post_id)
        if claim is None:
            return

        try:
            outcome = await asyncio.to_thread(
                download_audio_off_transaction, claim, temporary_directory
            )
        except SoftTimeLimitExceeded:
            logger.error("download_post_audio_soft_time_limit", post_id=post_id)
            await fail_media_post(session_factory, post_id, MEDIA_TASK_TIMEOUT_ERROR)
            return
        except Exception as error:
            logger.exception("download_post_audio_failed", post_id=post_id)
            await fail_media_post(session_factory, post_id, describe_media_error(error))
            return
        finally:
            media_storage.remove_tree_quietly(temporary_directory)

        await finish_download(session_factory, claim, outcome)
    finally:
        await engine.dispose()


async def claim_post_for_downloading(
    session_factory: async_sessionmaker[AsyncSession], post_id: int
) -> DownloadClaim | None:
    """transaction 1: lock, guard, status, commit — milliseconds, and the lock
    is gone long before yt-dlp is started."""
    async with session_factory() as session:
        try:
            post = await session.get(Post, post_id, with_for_update=True)
        except DBAPIError as error:
            await session.rollback()
            logger.error("download_post_audio_post_locked", post_id=post_id, error=str(error))
            return None

        if post is None:
            logger.error("download_post_audio_post_not_found", post_id=post_id)
            return None

        if post.status not in DOWNLOAD_ENTRY_STATUSES:
            logger.info(
                "download_post_audio_task_arrived_at_settled_post",
                post_id=post_id,
                status=post.status.value,
            )
            return None

        if post.preview_message_id is not None:
            logger.info(
                "download_post_audio_task_arrived_at_previewed_post",
                post_id=post_id,
                status=post.status.value,
            )
            return None

        track = await session.get(Track, post.track_id)
        assert track is not None

        claim = DownloadClaim(
            post_id=post_id,
            track_id=track.id,
            platform_links=dict(track.platform_links),
            track_duration_ms=track.duration_ms,
        )

        set_post_status(post, PostStatus.DOWNLOADING)
        await session.commit()

    logger.info("download_post_audio_claimed", post_id=post_id, track_id=claim.track_id)
    return claim


def download_audio_off_transaction(
    claim: DownloadClaim, temporary_directory: Path
) -> DownloadOutcome:
    """runs in a thread with no transaction and no lock open."""
    video_id = audio_download.youtube_music_video_id(claim.platform_links)

    reused = media_storage.find_existing_audio(settings.media_root, claim.track_id, video_id)
    if reused is not None:
        probe = audio_download.verify_downloaded_audio(reused, claim.track_duration_ms)
        logger.info(
            "download_post_audio_reused_artifact",
            post_id=claim.post_id,
            track_id=claim.track_id,
            path=str(reused),
        )
        return DownloadOutcome(
            video_id=video_id,
            path=reused,
            size_bytes=probe.size_bytes,
            duration_ms=probe.duration_ms,
            ytdlp_version=None,
        )

    media_storage.ensure_free_space(settings.media_root, settings.media_min_free_bytes)

    downloaded, ytdlp_version = audio_download.download_audio(
        video_id,
        temporary_directory,
        timeout_seconds=settings.download_task_soft_time_limit_seconds,
    )
    probe = audio_download.verify_downloaded_audio(downloaded, claim.track_duration_ms)

    final_path = media_storage.audio_file_path(
        settings.media_root, claim.track_id, video_id, downloaded.suffix.lstrip(".")
    )
    media_storage.publish_atomically(downloaded, final_path)

    return DownloadOutcome(
        video_id=video_id,
        path=final_path,
        size_bytes=probe.size_bytes,
        duration_ms=probe.duration_ms,
        ytdlp_version=ytdlp_version,
    )


async def finish_download(
    session_factory: async_sessionmaker[AsyncSession],
    claim: DownloadClaim,
    outcome: DownloadOutcome,
) -> None:
    async with session_factory() as session:
        try:
            post = await session.get(Post, claim.post_id, with_for_update=True)
        except DBAPIError as error:
            await session.rollback()
            logger.error(
                "download_post_audio_post_locked_on_finish",
                post_id=claim.post_id,
                error=str(error),
            )
            return

        if post is None or post.status is not PostStatus.DOWNLOADING:
            logger.info(
                "download_post_audio_finish_skipped",
                post_id=claim.post_id,
                status=None if post is None else post.status.value,
            )
            return

        await session.execute(upsert_audio_asset_statement(claim, outcome))

        bounds = fragment_bounds_for(post, outcome.duration_ms)
        post.fragment_start_ms = bounds.start_ms
        post.fragment_end_ms = bounds.end_ms

        set_post_status(post, PostStatus.DOWNLOADED)
        await session.commit()

    logger.info(
        "download_post_audio_finished",
        post_id=claim.post_id,
        track_id=claim.track_id,
        video_id=outcome.video_id,
        size_bytes=outcome.size_bytes,
        duration_ms=outcome.duration_ms,
        ytdlp_version=outcome.ytdlp_version,
        reused_artifact=outcome.ytdlp_version is None,
        fragment_start_ms=bounds.start_ms,
        fragment_end_ms=bounds.end_ms,
    )

    await chain_render_after_download(session_factory, claim.post_id)


async def chain_render_after_download(
    session_factory: async_sessionmaker[AsyncSession], post_id: int
) -> None:
    """after the commit, so a redelivered render never outruns the `DOWNLOADED` it
    needs. A failed dispatch fails the post: no task would move it otherwise."""
    try:
        await asyncio.to_thread(
            celery_app.send_task, RENDER_POST_VIDEO_TASK_NAME, kwargs={"post_id": post_id}
        )
    except Exception:
        logger.exception("render_task_dispatch_failed", post_id=post_id)
        await fail_media_post(session_factory, post_id, RENDER_DISPATCH_FAILED_ERROR)
        return

    logger.info("render_task_dispatched", post_id=post_id)


def upsert_audio_asset_statement(claim: DownloadClaim, outcome: DownloadOutcome) -> PostgresInsert:
    """`ON CONFLICT` rather than a read-modify-write: two posts of one track
    can finish downloading at the same moment, and neither may lose its row."""
    update_set: dict[str, Any] = {
        "path": str(outcome.path),
        "size_bytes": outcome.size_bytes,
        "duration_ms": outcome.duration_ms,
        # a Core statement never fires the ORM `onupdate` (AGENTS.md).
        "updated_at": func.now(),
    }
    if outcome.ytdlp_version is not None:
        update_set["ytdlp_version"] = outcome.ytdlp_version

    return (
        postgres_insert(AudioAsset)
        .values(
            track_id=claim.track_id,
            video_id=outcome.video_id,
            path=str(outcome.path),
            size_bytes=outcome.size_bytes,
            duration_ms=outcome.duration_ms,
            ytdlp_version=outcome.ytdlp_version or UNKNOWN_YTDLP_VERSION,
        )
        .on_conflict_do_update(
            index_elements=[AudioAsset.track_id, AudioAsset.video_id], set_=update_set
        )
    )


def fragment_bounds_for(post: Post, audio_duration_ms: int) -> fragment_rule.FragmentBounds:
    """stored bounds win while they fit the file: they may be the admin's choice,
    and overwriting them would silently undo it."""
    try:
        stored = fragment_rule.stored_bounds(post.fragment_start_ms, post.fragment_end_ms)
    except fragment_rule.FragmentBoundsError:
        stored = None

    if stored is not None and fragment_rule.bounds_within_track(stored, audio_duration_ms):
        return stored

    if stored is not None:
        logger.warning(
            "download_post_audio_stored_fragment_out_of_range",
            post_id=post.id,
            stored_start_ms=stored.start_ms,
            stored_end_ms=stored.end_ms,
            audio_duration_ms=audio_duration_ms,
        )
    return fragment_rule.choose_fragment_bounds(audio_duration_ms)


@celery_app.task(
    name=RENDER_POST_VIDEO_TASK_NAME,
    bind=True,
    soft_time_limit=settings.render_task_soft_time_limit_seconds,
    time_limit=settings.render_task_time_limit_seconds,
)
def render_post_video(self: Any, post_id: int) -> None:
    """terminal on error like every other task here: FAILED plus an alert,
    never a Celery retry."""
    bind_celery_task_id(self.request.id)
    bind_post_id(post_id)
    try:
        run_async(perform_render_post_video(post_id))
    except SoftTimeLimitExceeded:
        logger.error("render_post_video_soft_time_limit", post_id=post_id)
        run_async(fail_media_post_by_id(post_id, MEDIA_TASK_TIMEOUT_ERROR))


async def perform_render_post_video(post_id: int) -> None:
    engine = build_engine(
        settings.database_url, use_null_pool=True, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    temporary_directory = media_storage.task_temporary_directory(settings.media_root, post_id)
    try:
        session_factory = build_session_factory(engine)

        claim = await claim_post_for_rendering(session_factory, post_id)
        if claim is None:
            return

        try:
            outcome = await render_video_off_transaction(
                session_factory, claim, temporary_directory
            )
        except SoftTimeLimitExceeded:
            logger.error("render_post_video_soft_time_limit", post_id=post_id)
            await fail_media_post(session_factory, post_id, MEDIA_TASK_TIMEOUT_ERROR)
            return
        except Exception as error:
            logger.exception("render_post_video_failed", post_id=post_id)
            await fail_media_post(session_factory, post_id, describe_media_error(error))
            return
        finally:
            media_storage.remove_tree_quietly(temporary_directory)

        preview = await finish_render_or_fail(session_factory, claim, outcome)
        if preview is not None:
            await notify_about_missing_text(claim.post_id, outcome, preview)
    finally:
        await engine.dispose()


async def finish_render_or_fail(
    session_factory: async_sessionmaker[AsyncSession], claim: RenderClaim, outcome: RenderOutcome
) -> PreviewReference | None:
    """`RENDERED` has no edge to `FAILED`, so a preview delivery failure is
    reported from `RENDERING`, the state that can legally fail."""
    try:
        return await finish_render(session_factory, claim, outcome)
    except SoftTimeLimitExceeded:
        # before the blanket handler: Celery raises it as an ordinary
        # exception, and "помилка" would hide the truth, which is a timeout.
        logger.error("render_post_video_soft_time_limit", post_id=claim.post_id)
        await fail_media_post(session_factory, claim.post_id, MEDIA_TASK_TIMEOUT_ERROR)
    except Exception as error:
        logger.exception("render_post_video_finish_failed", post_id=claim.post_id)
        await fail_media_post(session_factory, claim.post_id, describe_media_error(error))
    return None


@dataclass(frozen=True)
class PreviewReference:
    chat_id: int
    message_id: int


async def notify_about_missing_text(
    post_id: int, outcome: RenderOutcome, preview: PreviewReference
) -> None:
    """sent after the `RENDERED` commit and never raising: a lost notice is a log
    line, never a failed post or a retried task. Not in the caption, which is verbatim."""
    notice = worker_lyrics.no_text_notice(post_id, outcome.lyrics_reason, outcome.text_version)
    if notice is None:
        logger.info(
            "render_post_video_no_text_notice_not_needed",
            post_id=post_id,
            text_version=outcome.text_version,
        )
        return

    try:
        bot = build_bot(settings)
        try:
            delivered = await send_alert(
                bot, preview.chat_id, notice, reply_to_message_id=preview.message_id
            )
        finally:
            await bot.session.close()
    except SoftTimeLimitExceeded:
        # the post is already `RENDERED`; the sync wrapper would try to fail it.
        logger.error("render_post_video_no_text_notice_timed_out", post_id=post_id)
        return
    except Exception:
        logger.warning("render_post_video_no_text_notice_failed", post_id=post_id, exc_info=True)
        return

    logger.info(
        "render_post_video_no_text_notice",
        post_id=post_id,
        lyrics_reason=outcome.lyrics_reason,
        delivered=delivered,
    )


async def claim_post_for_rendering(
    session_factory: async_sessionmaker[AsyncSession], post_id: int
) -> RenderClaim | None:
    """transaction 1 for the render. Everything ffmpeg needs is read out as
    plain values here, because the lock is gone by the time it runs."""
    async with session_factory() as session:
        try:
            post = await session.get(Post, post_id, with_for_update=True)
        except DBAPIError as error:
            await session.rollback()
            logger.error("render_post_video_post_locked", post_id=post_id, error=str(error))
            return None

        if post is None:
            logger.error("render_post_video_post_not_found", post_id=post_id)
            return None

        if post.status not in RENDER_ENTRY_STATUSES:
            logger.info(
                "render_post_video_task_arrived_at_settled_post",
                post_id=post_id,
                status=post.status.value,
            )
            return None

        # publication takes the newest `render` row, so it has to be the previewed one.
        if post.preview_message_id is not None:
            logger.info(
                "render_post_video_task_arrived_at_previewed_post",
                post_id=post_id,
                status=post.status.value,
            )
            return None

        track = await session.get(Track, post.track_id)
        assert track is not None

        preconditions = await read_render_preconditions(session, track, post)

        set_post_status(post, PostStatus.RENDERING)
        await session.commit()

    if preconditions.error is not None:
        # reported only after the status write: RESOLVED/DOWNLOADED have no
        # edge to FAILED, so a refusal has to be made from RENDERING.
        logger.error("render_post_video_preconditions_failed", post_id=post_id)
        await fail_media_post(session_factory, post_id, preconditions.error)
        return None

    assert preconditions.claim is not None
    logger.info("render_post_video_claimed", post_id=post_id, track_id=preconditions.claim.track_id)
    return preconditions.claim


@dataclass(frozen=True)
class RenderPreconditions:
    claim: RenderClaim | None
    error: str | None


async def read_render_preconditions(
    session: AsyncSession, track: Track, post: Post
) -> RenderPreconditions:
    """everything that can refuse the render, gathered while the lock is held
    but reported after it is released — see `claim_post_for_rendering`."""
    try:
        video_id = audio_download.youtube_music_video_id(dict(track.platform_links))
    except Exception as error:
        return RenderPreconditions(None, describe_media_error(error))

    asset = (
        await session.execute(
            select(AudioAsset).where(
                AudioAsset.track_id == track.id, AudioAsset.video_id == video_id
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        return RenderPreconditions(
            None, "для треку немає завантаженого аудіо — спершу має відпрацювати завантаження"
        )

    try:
        bounds = fragment_rule.stored_bounds(post.fragment_start_ms, post.fragment_end_ms)
    except fragment_rule.FragmentBoundsError as error:
        return RenderPreconditions(None, describe_media_error(error))
    if bounds is None:
        return RenderPreconditions(None, "у посту не записані межі фрагмента — рендерити нічого")

    return RenderPreconditions(
        RenderClaim(
            post_id=post.id,
            track_id=track.id,
            video_id=video_id,
            audio_path=asset.path,
            audio_duration_ms=asset.duration_ms,
            cover_url=track.cover_url,
            fragment_start_ms=bounds.start_ms,
            fragment_end_ms=bounds.end_ms,
            lyrics_query=worker_lyrics.LyricsQuery(
                track_id=track.id,
                artist_names=artist_names_from_stored(track.artists),
                title=track.title or "",
                duration_ms=track.duration_ms,
            ),
        ),
        None,
    )


def artist_names_from_stored(stored_artists: list[object]) -> tuple[str, ...]:
    names = (entry.get("name") for entry in stored_artists if isinstance(entry, dict))
    return tuple(name for name in names if isinstance(name, str) and name.strip())


async def render_video_off_transaction(
    session_factory: async_sessionmaker[AsyncSession], claim: RenderClaim, temporary_directory: Path
) -> RenderOutcome:
    """no transaction and no lock: LRCLIB and ffmpeg together run for minutes.
    The hash covers the subtitle file itself, so every input to the frames is in it."""
    lyrics = await prepare_lyrics_off_transaction(session_factory, claim)
    subtitles_text = ass_subtitles.build_ass_subtitles(
        lyrics.lines,
        fragment_start_ms=claim.fragment_start_ms,
        fragment_end_ms=claim.fragment_end_ms,
    )
    text_version = video_render.text_version_of(subtitles_text)
    lyrics_reason = worker_lyrics.reason_after_subtitles(
        claim.lyrics_query.track_id, lyrics, subtitles_text
    )

    params_hash = video_render.compute_params_hash(
        video_id=claim.video_id,
        audio_duration_ms=claim.audio_duration_ms,
        cover_url=claim.cover_url or "",
        fragment_start_ms=claim.fragment_start_ms,
        fragment_end_ms=claim.fragment_end_ms,
        text_version=text_version,
    )
    final_path = media_storage.render_file_path(settings.media_root, claim.post_id, params_hash)
    expected_duration_ms = claim.fragment_end_ms - claim.fragment_start_ms

    thumbnail_path = media_storage.thumbnail_file_path(
        settings.media_root, claim.post_id, params_hash
    )

    existing = media_storage.existing_artifact(final_path)
    if existing is not None:
        probe = await asyncio.to_thread(
            media_tools.verify_media,
            existing,
            require_video=True,
            require_audio=True,
            expected_duration_ms=expected_duration_ms,
            duration_tolerance_ms=video_render.RENDER_DURATION_TOLERANCE_MS,
        )
        logger.info(
            "render_post_video_reused_artifact",
            post_id=claim.post_id,
            track_id=claim.track_id,
            path=str(existing),
            params_hash=params_hash,
            text_version=text_version,
            lyrics_reason=lyrics_reason,
        )
        return RenderOutcome(
            params_hash=params_hash,
            path=existing,
            size_bytes=probe.size_bytes,
            duration_ms=probe.duration_ms,
            text_version=text_version,
            lyrics_reason=lyrics_reason,
            thumbnail_path=media_storage.existing_artifact(thumbnail_path),
        )

    media_storage.ensure_free_space(settings.media_root, settings.media_min_free_bytes)

    http_client = build_http_client()
    try:
        cover_path = await video_render.download_cover(
            http_client, claim.cover_url, temporary_directory
        )
    finally:
        await http_client.aclose()

    parameters = video_render.RenderParameters(
        audio_path=Path(claim.audio_path),
        cover_path=cover_path,
        fragment_start_ms=claim.fragment_start_ms,
        fragment_end_ms=claim.fragment_end_ms,
        subtitles_path=write_subtitles(claim.post_id, subtitles_text),
    )
    staging = media_storage.staging_path(temporary_directory, final_path)

    probe, quality = await asyncio.to_thread(
        video_render.render_fragment_within_size_limit,
        parameters,
        staging,
        timeout_seconds=settings.render_task_soft_time_limit_seconds,
    )

    # taken from the staged file before it is published, so the finished name
    # never appears next to a thumbnail of some earlier render.
    staged_thumbnail = await asyncio.to_thread(
        video_render.render_thumbnail,
        staging,
        media_storage.staging_path(temporary_directory, thumbnail_path),
    )

    media_storage.publish_atomically(staging, final_path)
    if staged_thumbnail is not None:
        media_storage.publish_atomically(staged_thumbnail, thumbnail_path)

    logger.info(
        "render_post_video_rendered",
        post_id=claim.post_id,
        track_id=claim.track_id,
        params_hash=params_hash,
        size_bytes=probe.size_bytes,
        duration_ms=probe.duration_ms,
        crf=quality.crf,
        maxrate=quality.maxrate,
        has_thumbnail=staged_thumbnail is not None,
        text_version=text_version,
        lyrics_reason=lyrics_reason,
    )
    return RenderOutcome(
        params_hash=params_hash,
        path=final_path,
        size_bytes=probe.size_bytes,
        duration_ms=probe.duration_ms,
        text_version=text_version,
        lyrics_reason=lyrics_reason,
        thumbnail_path=thumbnail_path if staged_thumbnail is not None else None,
    )


async def prepare_lyrics_off_transaction(
    session_factory: async_sessionmaker[AsyncSession], claim: RenderClaim
) -> worker_lyrics.PreparedLyrics:
    http_client = build_http_client()
    try:
        return await worker_lyrics.prepare_lyrics(
            session_factory,
            http_client,
            SourceCache(
                session_factory=session_factory, ttl_seconds=settings.source_fetch_ttl_seconds
            ),
            claim.lyrics_query,
        )
    finally:
        await http_client.aclose()


def write_subtitles(post_id: int, subtitles_text: str | None) -> Path | None:
    """`None` is «no text», never an empty file: the filtergraph then has no
    `subtitles=` step at all."""
    if subtitles_text is None:
        return None

    path = media_storage.subtitles_file_path(settings.media_root, post_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(subtitles_text, encoding="utf-8")
    return path


async def finish_render(
    session_factory: async_sessionmaker[AsyncSession], claim: RenderClaim, outcome: RenderOutcome
) -> PreviewReference | None:
    """transaction 2: the lock held across one Telegram upload is the delivery guard.
    Preview first, then `RENDERED`, so `RENDERED` means «прев'ю вже в чаті адміна»."""
    async with session_factory() as session:
        try:
            post = await session.get(Post, claim.post_id, with_for_update=True)
        except DBAPIError as error:
            await session.rollback()
            logger.error(
                "render_post_video_post_locked_on_finish", post_id=claim.post_id, error=str(error)
            )
            return None

        if post is None or post.status is not PostStatus.RENDERING:
            logger.info(
                "render_post_video_finish_skipped",
                post_id=claim.post_id,
                status=None if post is None else post.status.value,
            )
            return None

        await session.execute(upsert_render_statement(claim, outcome))

        await deliver_preview_for(post, outcome)
        preview_chat_id = post.preview_chat_id
        preview_message_id = post.preview_message_id
        assert preview_chat_id is not None and preview_message_id is not None

        # before the commit: a crash here leaves the preview in the chat, and
        # this line is then its only trace.
        logger.info(
            "render_post_video_preview_sent",
            post_id=claim.post_id,
            track_id=claim.track_id,
            preview_message_id=preview_message_id,
        )

        set_post_status(post, PostStatus.RENDERED)
        await session.commit()

    logger.info(
        "render_post_video_finished",
        post_id=claim.post_id,
        track_id=claim.track_id,
        params_hash=outcome.params_hash,
        size_bytes=outcome.size_bytes,
        duration_ms=outcome.duration_ms,
        preview_message_id=preview_message_id,
        text_version=outcome.text_version,
        lyrics_reason=outcome.lyrics_reason,
    )
    return PreviewReference(chat_id=preview_chat_id, message_id=preview_message_id)


def rendered_video(outcome: RenderOutcome) -> RenderedVideo:
    """the dimensions are the renderer's constants, not a probe of the file:
    `telegram/` never imports `worker/`, so they travel as data."""
    return RenderedVideo(
        path=outcome.path,
        width=video_render.VIDEO_WIDTH,
        height=video_render.VIDEO_HEIGHT,
        duration_ms=outcome.duration_ms,
        thumbnail_path=outcome.thumbnail_path,
    )


async def deliver_preview_for(post: Post, outcome: RenderOutcome) -> None:
    """publication cannot be revoked, so the admin first sees the exact file. The
    text draft loses its keyboard: its publish button would send the cover photo."""
    preview_chat_id = (
        post.draft_chat_id if post.draft_chat_id is not None else settings.alert_chat_id
    )

    bot = build_bot(settings)
    try:
        await deliver_preview(bot, post, preview_chat_id, rendered_video(outcome))
        await remove_draft_keyboard(bot, post)
    finally:
        await bot.session.close()


def upsert_render_statement(claim: RenderClaim, outcome: RenderOutcome) -> PostgresInsert:
    return (
        postgres_insert(Render)
        .values(
            post_id=claim.post_id,
            params_hash=outcome.params_hash,
            text_version=outcome.text_version,
            path=str(outcome.path),
            size_bytes=outcome.size_bytes,
            duration_ms=outcome.duration_ms,
        )
        .on_conflict_do_update(
            index_elements=[Render.post_id, Render.params_hash],
            set_={
                "path": str(outcome.path),
                "size_bytes": outcome.size_bytes,
                "duration_ms": outcome.duration_ms,
                "updated_at": func.now(),
            },
        )
    )


def describe_media_error(error: Exception) -> str:
    """the class name travels with the message: "екстрактор зламався" must never
    read like "цього треку немає"."""
    return f"{type(error).__name__}: {error}"


async def fail_media_post_by_id(post_id: int, error_message: str) -> None:
    """the soft-time-limit path owns no engine yet — it builds its own."""
    engine = build_engine(
        settings.database_url, use_null_pool=True, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    try:
        await fail_media_post(build_session_factory(engine), post_id, error_message)
    finally:
        await engine.dispose()


async def fail_media_post(
    session_factory: async_sessionmaker[AsyncSession], post_id: int, error_message: str
) -> None:
    """the media mirror of `fail_post`: its own session, because the caller's
    was committed and closed before the long stage ever started."""
    error_message = redacted_text(error_message)
    logger.error("media_task_failed", post_id=post_id, error=error_message)

    status_written = True
    async with session_factory() as session:
        try:
            await write_failed_status(session, post_id, error_message)
        except Exception:
            logger.exception("media_task_failed_status_write_retried", post_id=post_id)
            try:
                await session.rollback()
                await write_failed_status(session, post_id, error_message)
            except Exception:
                # telegram does not depend on Postgres: the alert goes out
                # either way, and it says the status may be wrong.
                logger.exception("media_task_failed_status_write_failed", post_id=post_id)
                status_written = False

    alert_text = f"Медіапайплайн не впорався з постом {post_id}: {error_message}"
    if not status_written:
        alert_text += "\nСтатус у базі зафіксувати не вдалося — пост міг лишитись у роботі."

    bot = build_bot(settings)
    try:
        await send_alert(bot, settings.alert_chat_id, alert_text)
    finally:
        await bot.session.close()
