"""Applying a caption and archiving through the Mini App against a real Postgres with a fake Telegram: the order of
Telegram and the database, which locks are held during the Telegram call, and what each answer leaves behind. The real
channel is never touched.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
import structlog.testing
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import EditMessageCaption
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from pisniar.config import Settings
from pisniar.db.models import Post, Render, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import issue_session_token

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 111
NOW = 1_800_000_000
WEBAPP_URL = "https://pisniar.example.ts.net"
ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"
SPOTIFY_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
MUSIC_URL = "https://music.youtube.com/watch?v=n4bPzdIs_nc"
METHOD = EditMessageCaption(chat_id=1, message_id=2, caption="x")
NOT_MODIFIED = "Bad Request: message is not modified: specified new message content is the same"
NOT_FOUND = "Bad Request: message to edit not found"
CHANNEL_CHAT_ID = -1_009_000_000_000
DRAFT_CHAT_ID = ADMIN_ID
TRACK_LOCK_SQL = "SELECT id FROM track WHERE id = :id FOR NO KEY UPDATE NOWAIT"
POST_LOCK_SQL = "SELECT id FROM post WHERE id = :id FOR UPDATE NOWAIT"
TRACK_RELATION_MODES_SQL = """
    SELECT l.mode FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.relation = 'track'::regclass AND l.pid <> pg_backend_pid()
      AND a.datname = current_database() AND a.state LIKE 'idle in transaction%'
"""


@dataclass
class FakeBot:
    """records every edit; `answers` maps a method name to the error it raises, and `hook` runs inside the call, while
    the caller's transaction is open."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    answers: dict[str, Exception] = field(default_factory=dict)
    hook: Callable[[], Awaitable[None]] | None = None

    async def handle(self, method: str, arguments: dict[str, Any]) -> bool:
        self.calls.append((method, arguments))
        if self.hook is not None:
            await self.hook()
        if method in self.answers:
            raise self.answers[method]
        return True

    async def edit_message_text(self, **arguments: Any) -> bool:
        return await self.handle("edit_message_text", arguments)

    async def edit_message_caption(self, **arguments: Any) -> bool:
        return await self.handle("edit_message_caption", arguments)

    async def edit_message_reply_markup(self, **arguments: Any) -> bool:
        return await self.handle("edit_message_reply_markup", arguments)


@dataclass
class Environment:
    http: httpx.AsyncClient
    engine: AsyncEngine
    session_factory: async_sessionmaker
    bot: FakeBot

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)}"}

    async def sha_of(self, post_id: int) -> str:
        response = await self.http.get(f"/api/posts/{post_id}", headers=self.headers())
        assert response.status_code == 200
        return response.json()["caption"]["sha256"]

    async def apply(self, post_id: int, sha: str | None = None) -> httpx.Response:
        body = {"expected_caption_sha256": sha or await self.sha_of(post_id)}
        return await self.http.post(f"/api/posts/{post_id}/apply", json=body, headers=self.headers())

    async def archive(self, post_id: int) -> httpx.Response:
        return await self.http.post(f"/api/posts/{post_id}/archive", headers=self.headers())

    async def caption_of(self, post_id: int) -> str | None:
        async with self.session_factory() as session:
            return await session.scalar(select(Post.caption).where(Post.id == post_id))

    async def status_of(self, post_id: int) -> PostStatus:
        async with self.session_factory() as session:
            status = await session.scalar(select(Post.status).where(Post.id == post_id))
            assert status is not None
            return PostStatus(status)


@dataclass
class Seeded:
    track_id: int
    resolved_id: int
    rendered_id: int
    published_id: int
    failed_id: int


@pytest_asyncio.fixture
async def environment(migrated_test_database_url: str, test_lock_timeout_ms: int) -> AsyncIterator[Environment]:
    settings = Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        admin_ids=str(ADMIN_ID),
        alert_chat_id=1,
        channel_id=-1,
        database_url=migrated_test_database_url,
        spotify_client_id="id",
        spotify_client_secret="secret",
        webapp_public_url=WEBAPP_URL,
    )
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=test_lock_timeout_ms)
    session_factory = build_session_factory(engine)
    bot = FakeBot()
    app = create_webapp(settings, clock=lambda: float(NOW), session_factory=session_factory, bot=cast(Bot, bot))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield Environment(http=http, engine=engine, session_factory=session_factory, bot=bot)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(environment: Environment) -> AsyncIterator[Seeded]:
    marker = uuid.uuid4().hex
    base = int(marker[:8], 16) + 10_000_000
    async with environment.session_factory() as session:
        track = Track(
            source_url=f"test://{marker}",
            source_url_normalized=f"test://{marker}",
            title="Тема",
            artists=[{"name": "Артист", "hashtag": None, "spotify_id": "a1"}],
            languages=["українська"],
            year=2019,
            release_type="album",
            release_title="Альбом",
            album_id_spotify=ALBUM_ID,
            platform_links={
                "spotify": {"url": SPOTIFY_URL, "source": "spotify", "match": "exact_isrc"},
                "youtubeMusic": {"url": MUSIC_URL, "source": "ytmusic", "match": "confirmed"},
            },
        )
        session.add(track)
        await session.flush()

        resolved = Post(
            track_id=track.id,
            created_by_user_id=1,
            status=PostStatus.RESOLVED,
            draft_chat_id=DRAFT_CHAT_ID,
            draft_message_id=base,
        )
        rendered = Post(
            track_id=track.id,
            created_by_user_id=1,
            status=PostStatus.RENDERED,
            draft_chat_id=DRAFT_CHAT_ID,
            draft_message_id=base + 1,
            preview_chat_id=DRAFT_CHAT_ID,
            preview_message_id=base + 2,
            preview_params_hash="a" * 64,
        )
        published = Post(
            track_id=track.id,
            created_by_user_id=1,
            status=PostStatus.PUBLISHED,
            draft_chat_id=DRAFT_CHAT_ID,
            draft_message_id=base + 3,
            channel_chat_id=CHANNEL_CHAT_ID,
            channel_message_id=base + 4,
            channel_message_kind="video",
        )
        failed = Post(
            track_id=track.id,
            created_by_user_id=1,
            status=PostStatus.FAILED,
            failed_from_status=PostStatus.RENDERING,
            draft_chat_id=DRAFT_CHAT_ID,
            draft_message_id=base + 5,
        )
        session.add_all([resolved, rendered, published, failed])
        await session.flush()
        session.add(
            Render(
                post_id=rendered.id,
                params_hash="f" * 64,
                text_version=None,
                path="/data/render/x.mp4",
                size_bytes=1000,
                duration_ms=45_000,
            )
        )
        await session.commit()
        result = Seeded(
            track_id=track.id,
            resolved_id=resolved.id,
            rendered_id=rendered.id,
            published_id=published.id,
            failed_id=failed.id,
        )

    yield result

    async with environment.session_factory() as session:
        await session.execute(delete(Post).where(Post.track_id == result.track_id))
        await session.execute(delete(Track).where(Track.id == result.track_id))
        await session.commit()


async def lock_state(
    environment: Environment,
    seeded: Seeded,
    post_id: int,
) -> dict[str, Any]:
    """what a second connection sees while the first one is inside its Telegram call."""
    state: dict[str, Any] = {}
    async with environment.session_factory() as probe:
        try:
            await probe.execute(text(TRACK_LOCK_SQL), {"id": seeded.track_id})
            state["track_lockable"] = True
        except DBAPIError:
            state["track_lockable"] = False
    async with environment.session_factory() as probe:
        try:
            await probe.execute(text(POST_LOCK_SQL), {"id": post_id})
            state["post_lockable"] = True
        except DBAPIError as error:
            state["post_lockable"] = False
            state["post_sqlstate"] = getattr(error.orig, "sqlstate", None)
    async with environment.session_factory() as probe:
        rows = await probe.execute(text(TRACK_RELATION_MODES_SQL))
        state["track_modes"] = sorted({row.mode for row in rows})
    return state


async def test_a_resolved_post_edits_its_draft_then_stores_the_caption(
    environment: Environment, seeded: Seeded
) -> None:
    seen: list[str | None] = []

    async def hook() -> None:
        seen.append(await environment.caption_of(seeded.resolved_id))

    environment.bot.hook = hook

    response = await environment.apply(seeded.resolved_id)

    assert response.status_code == 200
    assert response.json()["result"] == "edited"
    assert response.json()["other_messages_failed"] == []
    assert seen == [None]
    stored = await environment.caption_of(seeded.resolved_id)
    assert stored is not None
    method, arguments = environment.bot.calls[0]
    assert method == "edit_message_text"
    assert arguments["text"] == stored
    assert arguments["parse_mode"] == "HTML"
    assert arguments["request_timeout"] == 15
    assert arguments["chat_id"] == DRAFT_CHAT_ID
    assert arguments["reply_markup"] is not None


async def test_a_refused_edit_leaves_the_stored_caption_alone(environment: Environment, seeded: Seeded) -> None:
    environment.bot.answers["edit_message_text"] = TelegramNetworkError(method=METHOD, message="timeout")

    response = await environment.apply(seeded.resolved_id)

    assert response.status_code == 502
    assert await environment.caption_of(seeded.resolved_id) is None


async def test_not_modified_is_a_success_and_the_caption_is_stored(environment: Environment, seeded: Seeded) -> None:
    environment.bot.answers["edit_message_text"] = TelegramBadRequest(method=METHOD, message=NOT_MODIFIED)

    response = await environment.apply(seeded.resolved_id)

    assert (response.status_code, response.json()["result"]) == (200, "not_modified")
    assert await environment.caption_of(seeded.resolved_id) is not None


async def test_a_deleted_message_is_a_409_and_nothing_is_stored(environment: Environment, seeded: Seeded) -> None:
    environment.bot.answers["edit_message_text"] = TelegramBadRequest(method=METHOD, message=NOT_FOUND)

    response = await environment.apply(seeded.resolved_id)

    assert (response.status_code, response.json()["code"]) == (409, "channel_message_missing")
    assert await environment.caption_of(seeded.resolved_id) is None


async def test_a_rate_limit_names_the_wait_and_stores_nothing(environment: Environment, seeded: Seeded) -> None:
    environment.bot.answers["edit_message_text"] = TelegramRetryAfter(
        method=METHOD, message="Too Many Requests", retry_after=17
    )

    response = await environment.apply(seeded.resolved_id)

    assert response.status_code == 429
    assert "17 с" in response.json()["message"]
    assert await environment.caption_of(seeded.resolved_id) is None


async def test_a_stale_hash_is_a_409_and_telegram_is_not_called(environment: Environment, seeded: Seeded) -> None:
    response = await environment.apply(seeded.resolved_id, "0" * 64)

    assert (response.status_code, response.json()["code"]) == (409, "caption_changed")
    assert environment.bot.calls == []
    assert await environment.caption_of(seeded.resolved_id) is None


async def test_a_caption_over_the_limit_is_a_422_with_the_number(environment: Environment, seeded: Seeded) -> None:
    async with environment.session_factory() as session:
        track = await session.get(Track, seeded.track_id)
        assert track is not None
        track.title = "я" * 1200
        await session.commit()

    response = await environment.apply(seeded.resolved_id)

    assert (response.status_code, response.json()["code"]) == (422, "caption_too_long")
    assert re.search(r"\d{4} символів із 1024", response.json()["message"])
    assert environment.bot.calls == []


async def test_no_track_lock_is_held_during_the_telegram_call_but_the_post_is(
    environment: Environment, seeded: Seeded
) -> None:
    states: list[dict[str, Any]] = []

    async def hook() -> None:
        states.append(await lock_state(environment, seeded, seeded.resolved_id))

    environment.bot.hook = hook

    response = await environment.apply(seeded.resolved_id)

    assert response.status_code == 200
    assert states[0]["track_lockable"] is True
    assert states[0]["post_lockable"] is False
    assert states[0]["post_sqlstate"] == "55P03"
    assert set(states[0]["track_modes"]) <= {"AccessShareLock"}


async def test_no_track_lock_is_held_while_the_channel_is_probed(environment: Environment, seeded: Seeded) -> None:
    states: list[dict[str, Any]] = []

    async def hook() -> None:
        states.append(await lock_state(environment, seeded, seeded.published_id))

    environment.bot.hook = hook
    environment.bot.answers["edit_message_reply_markup"] = TelegramBadRequest(method=METHOD, message=NOT_FOUND)

    response = await environment.archive(seeded.published_id)

    assert response.status_code == 200
    assert states[0]["track_lockable"] is True
    assert states[0]["post_lockable"] is False
    assert set(states[0]["track_modes"]) <= {"AccessShareLock"}


async def test_a_post_locked_elsewhere_is_a_409_and_telegram_is_not_called(
    environment: Environment, seeded: Seeded
) -> None:
    sha = await environment.sha_of(seeded.resolved_id)
    async with environment.session_factory() as holder:
        await holder.execute(select(Post).where(Post.id == seeded.resolved_id).with_for_update())
        response = await environment.apply(seeded.resolved_id, sha)

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert environment.bot.calls == []


async def test_a_rendered_post_edits_the_preview_then_the_bare_draft_and_keeps_its_render(
    environment: Environment, seeded: Seeded
) -> None:
    async def renders() -> list[tuple[Any, ...]]:
        async with environment.session_factory() as session:
            rows = await session.execute(
                select(
                    Render.id,
                    Render.params_hash,
                    Render.text_version,
                    Render.path,
                    Render.size_bytes,
                    Render.duration_ms,
                    Render.updated_at,
                ).where(Render.post_id == seeded.rendered_id)
            )
            return [tuple(row) for row in rows]

    before = await renders()

    response = await environment.apply(seeded.rendered_id)

    assert response.status_code == 200
    assert response.json()["other_messages_failed"] == []
    first, second = environment.bot.calls
    assert first[0] == "edit_message_caption"
    assert first[1]["reply_markup"] is not None
    assert second[0] == "edit_message_text"
    assert second[1]["reply_markup"] is None
    assert await renders() == before
    assert await environment.status_of(seeded.rendered_id) is PostStatus.RENDERED


async def test_a_failed_draft_edit_after_the_commit_is_named_and_not_undone(
    environment: Environment, seeded: Seeded
) -> None:
    async def hook() -> None:
        environment.bot.answers["edit_message_text"] = TelegramNetworkError(method=METHOD, message="timeout")

    environment.bot.hook = hook

    response = await environment.apply(seeded.rendered_id)

    assert response.status_code == 200
    assert response.json()["other_messages_failed"] == ["draft"]
    assert await environment.caption_of(seeded.rendered_id) is not None


async def test_a_failed_post_edits_its_draft_without_a_keyboard(environment: Environment, seeded: Seeded) -> None:
    response = await environment.apply(seeded.failed_id)

    assert response.status_code == 200
    method, arguments = environment.bot.calls[0]
    assert (method, arguments["reply_markup"]) == ("edit_message_text", None)
    assert await environment.status_of(seeded.failed_id) is PostStatus.FAILED


async def test_a_published_post_edits_the_channel_message_by_its_kind(environment: Environment, seeded: Seeded) -> None:
    response = await environment.apply(seeded.published_id)

    assert response.status_code == 200
    method, arguments = environment.bot.calls[0]
    assert method == "edit_message_caption"
    assert arguments["chat_id"] == CHANNEL_CHAT_ID
    assert arguments["reply_markup"] is None
    assert len(environment.bot.calls) == 1


async def test_a_post_that_cannot_be_edited_is_refused_without_telegram(
    environment: Environment, seeded: Seeded
) -> None:
    async with environment.session_factory() as session:
        post = await session.get(Post, seeded.resolved_id)
        assert post is not None
        post.status = PostStatus.PUBLISHING
        await session.commit()

    response = await environment.apply(seeded.resolved_id, "0" * 64)

    assert (response.status_code, response.json()["code"]) == (409, "not_editable")
    assert environment.bot.calls == []


async def test_a_missing_post_is_a_404(environment: Environment, seeded: Seeded) -> None:
    body = {"expected_caption_sha256": "0" * 64}
    gone = seeded.resolved_id + 10_000_000

    apply = await environment.http.post(f"/api/posts/{gone}/apply", json=body, headers=environment.headers())
    archive = await environment.archive(gone)

    assert (apply.status_code, archive.status_code) == (404, 404)


async def test_a_deleted_channel_message_archives_the_post_and_is_logged(
    environment: Environment, seeded: Seeded
) -> None:
    environment.bot.answers["edit_message_reply_markup"] = TelegramBadRequest(method=METHOD, message=NOT_FOUND)

    with structlog.testing.capture_logs() as captured:
        response = await environment.archive(seeded.published_id)

    assert response.status_code == 200
    assert response.json() == {"post_id": seeded.published_id, "status_label": "Архівовано"}
    assert await environment.status_of(seeded.published_id) is PostStatus.ARCHIVED
    events = [e for e in captured if e["event"] == "post_archived_channel_message_missing"]
    assert events[0]["user_id"] == ADMIN_ID
    assert events[0]["post_id"] == seeded.published_id
    _, arguments = environment.bot.calls[0]
    assert arguments["request_timeout"] == 15
    assert arguments["reply_markup"] is None


async def test_a_live_channel_message_is_never_archived(environment: Environment, seeded: Seeded) -> None:
    environment.bot.answers["edit_message_reply_markup"] = TelegramBadRequest(method=METHOD, message=NOT_MODIFIED)

    response = await environment.archive(seeded.published_id)

    assert (response.status_code, response.json()["code"]) == (409, "channel_message_exists")
    assert await environment.status_of(seeded.published_id) is PostStatus.PUBLISHED


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=METHOD, message="Bad Request: chat not found"),
        TelegramBadRequest(method=METHOD, message="Bad Request: message can't be edited"),
        TelegramNetworkError(method=METHOD, message="timeout"),
        TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=5),
    ],
)
async def test_any_other_answer_changes_nothing(
    environment: Environment,
    seeded: Seeded,
    error: Exception,
) -> None:
    environment.bot.answers["edit_message_reply_markup"] = error

    response = await environment.archive(seeded.published_id)

    assert response.status_code in (429, 502)
    assert await environment.status_of(seeded.published_id) is PostStatus.PUBLISHED


async def test_a_post_that_is_not_published_is_not_archived(environment: Environment, seeded: Seeded) -> None:
    response = await environment.archive(seeded.resolved_id)

    assert (response.status_code, response.json()["code"]) == (409, "not_published")
    assert environment.bot.calls == []
    assert await environment.status_of(seeded.resolved_id) is PostStatus.RESOLVED


async def test_every_action_is_logged_with_who_what_and_how_it_ended_and_no_secret(
    environment: Environment, seeded: Seeded
) -> None:
    with structlog.testing.capture_logs() as captured:
        await environment.apply(seeded.rendered_id)

    event = next(e for e in captured if e["event"] == "caption_applied")
    assert event["user_id"] == ADMIN_ID
    assert event["post_id"] == seeded.rendered_id
    assert event["targets"] == ["preview", "draft"]
    assert event["result"] == "edited"
    assert BOT_TOKEN not in repr(captured)
