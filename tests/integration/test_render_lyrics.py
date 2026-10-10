"""The render task's lyrics against a real Postgres, LRCLIB mocked at the transport.
While the request is in flight the database itself is asked whether any client
holds a transaction or a lock; stored lines never reach the network; and the render
row and the preview get what the video actually carries.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import AudioAsset, Lyric, Post, Render, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus, set_post_status
from pisniar.resolve.sources import lrclib, ytmusic_lyrics
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.worker import ffmpeg as media_tools
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.lyrics import LYRICS_ALREADY_STORED, LYRICS_REMOVED_BY_ADMIN, LYRICS_UNTIMED
from pisniar.worker.render import text_version_of

pytestmark = pytest.mark.integration

VIDEO_ID = "n4bPzdIs_nc"
COVER_URL = "https://cdn.test/cover.jpg"
DURATION_MS = 212_000
PREVIEW_MESSAGE_OFFSET = 3_000_000
SYNCED_LYRICS = "[00:01.00] Перший рядок\n[00:06.00] Другий рядок"

OTHER_TRANSACTIONS_SQL = """
    SELECT pid, state, left(query, 120) AS query
    FROM pg_stat_activity
    WHERE datname = current_database()
      AND pid <> pg_backend_pid()
      AND backend_type = 'client backend'
      AND xact_start IS NOT NULL
"""
OTHER_LOCKS_SQL = """
    SELECT l.pid, l.locktype, l.mode, l.relation::regclass::text AS relation
    FROM pg_locks l
    JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE a.datname = current_database()
      AND l.pid <> pg_backend_pid()
      AND a.backend_type = 'client backend'
"""


class DatabaseProbe:
    """a raw connection outside every engine the task builds, so it can see them all."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def snapshot(self) -> dict[str, list[dict[str, object]]]:
        transactions = await self.connection.fetch(OTHER_TRANSACTIONS_SQL)
        locks = await self.connection.fetch(OTHER_LOCKS_SQL)
        return {
            "transactions": [dict(row) for row in transactions],
            "locks": [dict(row) for row in locks],
        }


@pytest_asyncio.fixture
async def probe(migrated_test_database_url: str) -> AsyncIterator[DatabaseProbe]:
    dsn = (
        make_url(migrated_test_database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    connection = await asyncpg.connect(dsn)
    try:
        yield DatabaseProbe(connection)
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def session_factory(
    migrated_test_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_test_database_url)
    yield build_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def alerts(
    monkeypatch: pytest.MonkeyPatch, migrated_test_database_url: str, tmp_path: Path
) -> AsyncMock:
    """nothing here may reach Telegram: `.env` carries the real token in Docker."""
    monkeypatch.setattr(worker_tasks.settings, "database_url", migrated_test_database_url)
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    monkeypatch.setattr(worker_tasks.settings, "media_min_free_bytes", 1)
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(lrclib.THROTTLE, "min_interval_seconds", 0.0)
    monkeypatch.setattr(lrclib, "RETRY_BASE_DELAY_SECONDS", 0.0)

    send_alert_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_tasks, "send_alert", send_alert_mock)
    return send_alert_mock


class StubRenderer:
    """ffmpeg's stand-in: the bot container has none, and the filtergraph has
    its own suite. It keeps the subtitle file's content while it still exists."""

    def __init__(self) -> None:
        self.subtitles: list[str | None] = []

    def __call__(
        self,
        parameters: worker_tasks.video_render.RenderParameters,
        output_path: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[media_tools.MediaProbe, object]:
        subtitles_path = parameters.subtitles_path
        self.subtitles.append(
            subtitles_path.read_text(encoding="utf-8") if subtitles_path is not None else None
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"rendered")
        probe = media_tools.MediaProbe(
            path=output_path,
            size_bytes=8,
            duration_ms=45_000,
            streams=(media_tools.StreamInfo("video", "h264", 720, 1280, "yuv420p", "tv"),),
        )
        return probe, worker_tasks.video_render.DEFAULT_QUALITY


class PreviewSpy:
    """replaces the Telegram upload and records what reached it."""

    def __init__(self) -> None:
        self.lyrics_reasons: list[str] = []

    async def __call__(self, post: Post, outcome: worker_tasks.RenderOutcome) -> None:
        self.lyrics_reasons.append(outcome.lyrics_reason)
        post.preview_chat_id = 111
        post.preview_message_id = post.id + PREVIEW_MESSAGE_OFFSET


LrclibHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def install_pipeline(
    monkeypatch: pytest.MonkeyPatch, lrclib_handler: LrclibHandler
) -> tuple[StubRenderer, PreviewSpy]:
    renderer = StubRenderer()
    preview = PreviewSpy()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lrclib.net":
            return await lrclib_handler(request)
        if str(request.url) == COVER_URL:
            return httpx.Response(200, content=b"\xff\xd8cover")
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(
        worker_tasks,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", renderer)
    monkeypatch.setattr(worker_tasks.video_render, "render_thumbnail", lambda *a, **k: None)
    # `ytmusicapi` has its own transport, so the mock transport above cannot see it; the cache
    # row is keyed by the video id every test shares, so the whole call is replaced.
    monkeypatch.setattr(
        ytmusic_lyrics, "fetch_lyrics", AsyncMock(return_value=LyricsResult(LyricsOutcome.ABSENT))
    )
    monkeypatch.setattr(worker_tasks, "queue_plain_lyrics_lookup", AsyncMock())
    monkeypatch.setattr(worker_tasks, "deliver_preview_for", preview)
    return renderer, preview


async def make_downloaded_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, str, str]:
    """a unique artist and title per test: the LRCLIB cache row lives for an hour."""
    marker = uuid.uuid4().hex
    artist = f"Виконавець {marker}"
    title = f"Покаяння {marker}"

    async with session_factory() as session:
        source_url = f"https://open.spotify.com/track/{marker}"
        track = Track(
            source_url=source_url,
            source_url_normalized=source_url,
            title=title,
            artists=[{"name": artist, "hashtag": None, "spotify_id": None}],
            duration_ms=DURATION_MS,
            cover_url=COVER_URL,
            platform_links={
                "youtubeMusic": {
                    "url": f"https://music.youtube.com/watch?v={VIDEO_ID}",
                    "source": "ytmusic",
                    "match": "exact_isrc",
                }
            },
        )
        session.add(track)
        await session.flush()

        session.add(
            AudioAsset(
                track_id=track.id,
                video_id=VIDEO_ID,
                path=f"/data/audio/{track.id}/{VIDEO_ID}.opus",
                size_bytes=3_400_000,
                duration_ms=DURATION_MS,
                ytdlp_version="test",
            )
        )

        post = Post(track_id=track.id, created_by_user_id=111, caption="caption")
        session.add(post)
        await session.flush()
        for status in (
            PostStatus.RESOLVING,
            PostStatus.RESOLVED,
            PostStatus.DOWNLOADING,
            PostStatus.DOWNLOADED,
        ):
            set_post_status(post, status)
        post.fragment_start_ms = 0
        post.fragment_end_ms = 45_000

        await session.commit()
        return post.id, track.id, artist, title


def lrclib_record(artist: str, title: str) -> dict[str, object]:
    return {
        "id": 1,
        "trackName": title,
        "artistName": artist,
        "albumName": "Альбом",
        "duration": DURATION_MS / 1000,
        "instrumental": False,
        "plainLyrics": "Перший рядок\nДругий рядок",
        "syncedLyrics": SYNCED_LYRICS,
    }


async def render_row_of(
    session_factory: async_sessionmaker[AsyncSession], post_id: int
) -> tuple[Post, Render]:
    async with session_factory() as session:
        post = await session.get(Post, post_id)
        render = await session.scalar(select(Render).where(Render.post_id == post_id))
        assert post is not None
        assert render is not None
        return post, render


async def lyric_count(session_factory: async_sessionmaker[AsyncSession], track_id: int) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Lyric).where(Lyric.track_id == track_id)
        )
        return count or 0


async def test_the_probe_sees_a_held_row_lock(
    session_factory: async_sessionmaker[AsyncSession], probe: DatabaseProbe
) -> None:
    """the assertion below is only as good as the probe: without this, "no
    transaction seen" could just as well mean "nothing can be seen"."""
    post_id, _, _, _ = await make_downloaded_post(session_factory)

    async with session_factory() as session:
        await session.get(Post, post_id, with_for_update=True)
        snapshot = await probe.snapshot()
        await session.rollback()

    assert snapshot["transactions"]
    assert any(lock["relation"] == "post" for lock in snapshot["locks"])


async def test_no_transaction_and_no_lock_is_open_while_lrclib_is_asked(
    session_factory: async_sessionmaker[AsyncSession],
    probe: DatabaseProbe,
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, artist, title = await make_downloaded_post(session_factory)
    snapshots: list[dict[str, list[dict[str, object]]]] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        snapshots.append(await probe.snapshot())
        return httpx.Response(200, json=lrclib_record(artist, title))

    renderer, preview = install_pipeline(monkeypatch, answer)

    await worker_tasks.perform_render_post_video(post_id)

    assert snapshots, "LRCLIB was never asked"
    assert snapshots == [{"transactions": [], "locks": []}] * len(snapshots)
    alerts.assert_not_awaited()

    post, render = await render_row_of(session_factory, post_id)
    assert post.status is PostStatus.RENDERED
    assert renderer.subtitles[0] is not None
    assert render.text_version == text_version_of(renderer.subtitles[0])
    assert await lyric_count(session_factory, track_id) == 2
    assert preview.lyrics_reasons == ["lyrics_synced"]


async def test_stored_lines_are_rendered_without_asking_lrclib(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, _, _ = await make_downloaded_post(session_factory)
    async with session_factory() as session:
        session.add(
            Lyric(track_id=track_id, line_index=0, start_ms=1_000, text="Свій", source="manual")
        )
        await session.commit()

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("stored lines exist, LRCLIB must not be asked")

    renderer, preview = install_pipeline(monkeypatch, refuse)

    await worker_tasks.perform_render_post_video(post_id)

    alerts.assert_not_awaited()
    post, render = await render_row_of(session_factory, post_id)
    assert post.status is PostStatus.RENDERED
    assert render.text_version is not None
    assert renderer.subtitles[0] is not None
    assert "Свій" in renderer.subtitles[0]
    assert preview.lyrics_reasons == [LYRICS_ALREADY_STORED]


async def test_youtube_music_timed_text_is_stored_first_and_lrclib_is_not_asked(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, _, _ = await make_downloaded_post(session_factory)

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("YouTube Music had timed text, LRCLIB must not be asked")

    renderer, preview = install_pipeline(monkeypatch, refuse)
    youtube_answer = LyricsResult(
        outcome=LyricsOutcome.SYNCED,
        lines=(
            LyricsLine(start_ms=1_000, text="Перший рядок"),
            LyricsLine(start_ms=6_000, text=""),
            LyricsLine(start_ms=9_000, text="Другий рядок"),
        ),
    )
    monkeypatch.setattr(ytmusic_lyrics, "fetch_lyrics", AsyncMock(return_value=youtube_answer))

    await worker_tasks.perform_render_post_video(post_id)

    alerts.assert_not_awaited()
    async with session_factory() as session:
        stored = (
            await session.scalars(
                select(Lyric).where(Lyric.track_id == track_id).order_by(Lyric.line_index)
            )
        ).all()
    assert [(row.start_ms, row.text, row.source) for row in stored] == [
        (1_000, "Перший рядок", "ytmusic"),
        (6_000, "", "ytmusic"),
        (9_000, "Другий рядок", "ytmusic"),
    ]
    assert renderer.subtitles[0] is not None
    assert "Перший рядок" in renderer.subtitles[0]
    assert preview.lyrics_reasons == ["lyrics_synced"]


async def test_plain_text_found_while_rendering_is_stored_without_times_and_explained(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, artist, title = await make_downloaded_post(session_factory)

    async def plain_only(request: httpx.Request) -> httpx.Response:
        record = {**lrclib_record(artist, title), "syncedLyrics": ""}
        return httpx.Response(200, json=record)

    renderer, preview = install_pipeline(monkeypatch, plain_only)

    await worker_tasks.perform_render_post_video(post_id)

    async with session_factory() as session:
        stored = (
            await session.scalars(
                select(Lyric).where(Lyric.track_id == track_id).order_by(Lyric.line_index)
            )
        ).all()
    assert [(row.start_ms, row.text, row.source) for row in stored] == [
        (None, "Перший рядок", "lrclib"),
        (None, "Другий рядок", "lrclib"),
    ]
    assert renderer.subtitles == [None]
    assert preview.lyrics_reasons == [LYRICS_UNTIMED]
    alerts.assert_awaited_once_with(
        ANY,
        111,
        f"Пост {post_id}: Текст є, але ще не відстукано — відкрийте «Текст» у редакторі поста.",
        reply_to_message_id=post_id + PREVIEW_MESSAGE_OFFSET,
    )


async def test_a_removed_text_renders_without_text_and_never_asks_lrclib(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, _, _ = await make_downloaded_post(session_factory)
    async with session_factory() as session:
        track = await session.get(Track, track_id)
        assert track is not None
        track.lyrics_edited_at = datetime.now(UTC)
        await session.commit()

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the text was saved by a human, LRCLIB must not be asked")

    renderer, preview = install_pipeline(monkeypatch, refuse)

    await worker_tasks.perform_render_post_video(post_id)

    post, render = await render_row_of(session_factory, post_id)
    assert post.status is PostStatus.RENDERED
    assert render.text_version is None
    assert renderer.subtitles == [None]
    assert await lyric_count(session_factory, track_id) == 0
    assert preview.lyrics_reasons == [LYRICS_REMOVED_BY_ADMIN]
    alerts.assert_awaited_once_with(
        ANY,
        111,
        f"Пост {post_id}: Текст прибрано вручну — відео без тексту.",
        reply_to_message_id=post_id + PREVIEW_MESSAGE_OFFSET,
    )


async def test_untimed_lines_never_reach_the_subtitles_nor_lrclib(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, _, _ = await make_downloaded_post(session_factory)
    async with session_factory() as session:
        for line_index in range(2):
            session.add(
                Lyric(
                    track_id=track_id,
                    line_index=line_index,
                    start_ms=None,
                    text=f"Безчасовий {line_index}",
                    source="manual",
                )
            )
        await session.commit()

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("lines are stored, LRCLIB must not be asked")

    renderer, preview = install_pipeline(monkeypatch, refuse)

    await worker_tasks.perform_render_post_video(post_id)

    _, render = await render_row_of(session_factory, post_id)
    assert render.text_version is None
    assert renderer.subtitles == [None]
    assert await lyric_count(session_factory, track_id) == 2
    assert preview.lyrics_reasons == [LYRICS_UNTIMED]
    alerts.assert_awaited_once_with(
        ANY,
        111,
        f"Пост {post_id}: Текст є, але ще не відстукано — відкрийте «Текст» у редакторі поста.",
        reply_to_message_id=post_id + PREVIEW_MESSAGE_OFFSET,
    )


async def test_an_unavailable_lrclib_still_yields_a_video_without_text(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id, track_id, _, _ = await make_downloaded_post(session_factory)

    async def overloaded(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"name": "ServerOverloaded", "statusCode": 503})

    renderer, preview = install_pipeline(monkeypatch, overloaded)

    await worker_tasks.perform_render_post_video(post_id)

    post, render = await render_row_of(session_factory, post_id)
    assert post.status is PostStatus.RENDERED
    assert render.text_version is None
    assert renderer.subtitles == [None]
    assert await lyric_count(session_factory, track_id) == 0
    assert preview.lyrics_reasons == ["lyrics_source_unavailable"]

    # the only alert is the notice under the preview, not a failure.
    alerts.assert_awaited_once_with(
        ANY,
        111,
        f"Пост {post_id}: Джерело тексту не відповіло — відео зібрано без тексту.",
        reply_to_message_id=post_id + PREVIEW_MESSAGE_OFFSET,
    )


async def test_a_redelivered_render_explains_the_missing_text_only_once(
    session_factory: async_sessionmaker[AsyncSession],
    alerts: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """celery redelivers after the `RENDERED` commit when the worker dies before
    the ack; the second run must stop at the claim, before any notice."""
    post_id, _, _, _ = await make_downloaded_post(session_factory)

    async def not_found(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            404,
            json={"message": "Failed", "name": "TrackNotFound", "statusCode": 404},
        )

    install_pipeline(monkeypatch, not_found)

    await worker_tasks.perform_render_post_video(post_id)
    await worker_tasks.perform_render_post_video(post_id)

    alerts.assert_awaited_once_with(
        ANY,
        111,
        f"Пост {post_id}: Тексту цього треку немає ні на YouTube Music, ні в LRCLIB — "
        "відео без тексту.",
        reply_to_message_id=post_id + PREVIEW_MESSAGE_OFFSET,
    )

    async with session_factory() as session:
        renders = await session.scalar(
            select(func.count()).select_from(Render).where(Render.post_id == post_id)
        )
    assert renders == 1
