"""Reading, replacing and looking up a track's lyrics through the Mini App against a real Postgres:
the replacement under the track lock, the version check, the media guard, and lookups that write
nothing and lock nothing. The outside sources are stand-ins; the rows are real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_webapp_fragment_render import make_previewed_post

from pisniar.config import Settings
from pisniar.db.models import Lyric, Post, Track
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.db.states import PostStatus
from pisniar.resolve.lyrics_lookup import SourceAnswer
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.webapp import lyrics_proposals
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import issue_session_token

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 111
NOW = 1_800_000_000
DURATION_MS = 180_000


@dataclass
class Environment:
    http: httpx.AsyncClient
    session_factory: async_sessionmaker[AsyncSession]

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)}"}

    async def view(self, post_id: int) -> dict[str, Any]:
        response = await self.http.get(f"/api/posts/{post_id}/lyrics", headers=self.headers())
        assert response.status_code == 200
        return response.json()

    async def save(
        self,
        post_id: int,
        lines: list[dict[str, Any]],
        version: str,
    ) -> httpx.Response:
        return await self.http.put(
            f"/api/posts/{post_id}/lyrics",
            json={"lines": lines, "expected_lyrics_version": version},
            headers=self.headers(),
        )


@pytest_asyncio.fixture
async def environment(migrated_test_database_url: str) -> AsyncIterator[Environment]:
    settings = Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        admin_ids=str(ADMIN_ID),
        alert_chat_id=1,
        channel_id=-1,
        database_url=migrated_test_database_url,
        spotify_client_id="id",
        spotify_client_secret="secret",
    )
    engine = build_engine(migrated_test_database_url, lock_timeout_ms=LOCK_TIMEOUT_MS)
    session_factory = build_session_factory(engine)
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        http_client=httpx.AsyncClient(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield Environment(http=http, session_factory=session_factory)
    await engine.dispose()


async def stored_lines(environment: Environment, track_id: int) -> list[tuple[int, int | None, str, str]]:
    async with environment.session_factory() as session:
        rows = (await session.scalars(select(Lyric).where(Lyric.track_id == track_id).order_by(Lyric.line_index))).all()
        return [(row.line_index, row.start_ms, row.text, row.source) for row in rows]


async def track_of(environment: Environment, post_id: int) -> Track:
    async with environment.session_factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        track = await session.get(Track, post.track_id)
        assert track is not None
        return track


async def add_line(
    environment: Environment,
    track_id: int,
    index: int,
    start_ms: int | None,
    text: str,
) -> None:
    async with environment.session_factory() as session:
        session.add(Lyric(track_id=track_id, line_index=index, start_ms=start_ms, text=text, source="lrclib"))
        await session.commit()


async def add_sibling(
    environment: Environment,
    track_id: int,
    status: PostStatus,
) -> None:
    async with environment.session_factory() as session:
        session.add(Post(track_id=track_id, created_by_user_id=1, status=status))
        await session.commit()


@pytest.fixture
def previewed(environment: Environment) -> Any:
    return lambda **kwargs: make_previewed_post(environment.session_factory, **kwargs)


async def test_an_untouched_track_has_an_empty_unmarked_text(environment: Environment, previewed: Any) -> None:
    post_id = await previewed()

    view = await environment.view(post_id)

    assert view["lines"] == [] and view["edited_by_admin"] is False
    assert len(view["lyrics_version"]) == 64


async def test_a_save_replaces_the_text_marks_it_and_returns_the_fresh_view(
    environment: Environment, previewed: Any
) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    await add_line(environment, track.id, 0, 500, "старий")
    before = await environment.view(post_id)

    response = await environment.save(
        post_id,
        [
            {"text": "  раз  два ", "start_ms": 1_000},
            {"text": "три", "start_ms": None},
            {"text": "", "start_ms": 5_000},
        ],
        before["lyrics_version"],
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["edited_by_admin"] is True
    assert saved["lyrics_version"] != before["lyrics_version"]
    assert await stored_lines(environment, track.id) == [
        (0, 1_000, "раз два", "manual"),
        (1, None, "три", "manual"),
        (2, 5_000, "", "manual"),
    ]
    assert (await track_of(environment, post_id)).lyrics_edited_at is not None
    assert await environment.view(post_id) == saved


async def test_an_empty_save_removes_the_text_and_keeps_the_marker(environment: Environment, previewed: Any) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    await add_line(environment, track.id, 0, 500, "старий")
    version = (await environment.view(post_id))["lyrics_version"]

    response = await environment.save(post_id, [], version)

    assert response.status_code == 200
    assert await stored_lines(environment, track.id) == []
    assert (await track_of(environment, post_id)).lyrics_edited_at is not None


async def test_a_text_that_changed_since_the_editor_opened_is_stale_and_stays_whole(
    environment: Environment, previewed: Any
) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    version = (await environment.view(post_id))["lyrics_version"]
    await add_line(environment, track.id, 0, 500, "прийшов іншим шляхом")

    response = await environment.save(post_id, [{"text": "мій", "start_ms": 1_000}], version)

    assert (response.status_code, response.json()["code"]) == (409, "stale_data")
    assert await stored_lines(environment, track.id) == [(0, 500, "прийшов іншим шляхом", "lrclib")]


async def test_a_sibling_post_in_a_media_state_blocks_the_save_and_editing_does_not(
    environment: Environment, previewed: Any
) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    version = (await environment.view(post_id))["lyrics_version"]
    line = [{"text": "раз", "start_ms": 1_000}]

    await add_sibling(environment, track.id, PostStatus.EDITING)
    allowed = await environment.save(post_id, line, version)
    assert allowed.status_code == 200

    for status in (PostStatus.DOWNLOADING, PostStatus.DOWNLOADED, PostStatus.RENDERING):
        await add_sibling(environment, track.id, status)
        version = (await environment.view(post_id))["lyrics_version"]
        blocked = await environment.save(post_id, [{"text": "два", "start_ms": 2_000}], version)
        assert (blocked.status_code, blocked.json()["code"]) == (409, "media_in_progress")
        assert await stored_lines(environment, track.id) == [(0, 1_000, "раз", "manual")]


async def test_audio_of_another_recording_is_audio_missing(environment: Environment, previewed: Any) -> None:
    post_id = await previewed(audio_video_id="dQw4w9WgXcQ")
    version = (await environment.view(post_id))["lyrics_version"]

    response = await environment.save(post_id, [{"text": "раз", "start_ms": 1_000}], version)

    assert (response.status_code, response.json()["code"]) == (409, "audio_missing")


async def test_a_time_past_the_end_of_the_audio_is_422_and_the_old_text_stays(
    environment: Environment, previewed: Any
) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    await add_line(environment, track.id, 0, 500, "старий")
    version = (await environment.view(post_id))["lyrics_version"]

    response = await environment.save(
        post_id,
        [{"text": "раз", "start_ms": 1_000}, {"text": "два", "start_ms": DURATION_MS}],
        version,
    )

    assert (response.status_code, response.json()["code"]) == (422, "invalid_lyrics")
    assert "2" in response.json()["message"]
    assert await stored_lines(environment, track.id) == [(0, 500, "старий", "lrclib")]


async def test_a_post_that_cannot_be_edited_is_not_choosable(environment: Environment) -> None:
    choosable_id = await make_previewed_post(
        environment.session_factory, status=PostStatus.FAILED, failed_from=PostStatus.DOWNLOADED
    )
    assert (
        await environment.http.get(f"/api/posts/{choosable_id}/lyrics", headers=environment.headers())
    ).status_code == 200

    post_id = await make_previewed_post(environment.session_factory, status=PostStatus.RENDERING)

    response = await environment.http.get(f"/api/posts/{post_id}/lyrics", headers=environment.headers())
    assert (response.status_code, response.json()["code"]) == (409, "not_choosable")


async def test_a_track_held_by_another_transaction_makes_the_save_post_busy_and_changes_nothing(
    environment: Environment, previewed: Any
) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    await add_line(environment, track.id, 0, 500, "старий")
    version = (await environment.view(post_id))["lyrics_version"]

    async with environment.session_factory() as holder:
        await holder.get(Track, track.id, with_for_update={"key_share": True})
        response = await environment.save(post_id, [{"text": "раз", "start_ms": 1_000}], version)
        await holder.rollback()

    assert (response.status_code, response.json()["code"]) == (409, "post_busy")
    assert await stored_lines(environment, track.id) == [(0, 500, "старий", "lrclib")]


async def test_two_saves_from_one_version_leave_one_whole_text(environment: Environment, previewed: Any) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    version = (await environment.view(post_id))["lyrics_version"]

    first, second = await asyncio.gather(
        environment.save(
            post_id,
            [{"text": "перший", "start_ms": 1_000}, {"text": "ще", "start_ms": None}],
            version,
        ),
        environment.save(post_id, [{"text": "другий", "start_ms": 2_000}], version),
    )

    assert sorted(response.status_code for response in (first, second)) == [200, 409]
    lines = await stored_lines(environment, track.id)
    assert lines in (
        [(0, 1_000, "перший", "manual"), (1, None, "ще", "manual")],
        [(0, 2_000, "другий", "manual")],
    )


async def test_a_lookup_writes_nothing_and_takes_no_lock(
    environment: Environment,
    previewed: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_id = await previewed()
    track = await track_of(environment, post_id)
    await add_line(environment, track.id, 0, None, "старий")
    answer = SourceAnswer(
        "ytmusic",
        LyricsResult(LyricsOutcome.SYNCED, (LyricsLine(start_ms=1_000, text="знайдений"),)),
    )
    monkeypatch.setattr(lyrics_proposals, "look_up_timed_text", AsyncMock(return_value=(answer, "")))
    monkeypatch.setattr(
        lyrics_proposals,
        "fetch_plain_text",
        AsyncMock(return_value=LyricsResult(LyricsOutcome.UNSYNCED, (LyricsLine(start_ms=None, text="простий"),))),
    )

    async with environment.session_factory() as holder:
        await holder.get(Track, track.id, with_for_update={"key_share": True})
        synced = await environment.http.get(f"/api/posts/{post_id}/lyrics/synced", headers=environment.headers())
        plain = await environment.http.get(
            f"/api/posts/{post_id}/lyrics/plain?source=genius", headers=environment.headers()
        )
        await holder.rollback()

    assert synced.status_code == 200 and synced.json()["lines"][0]["text"] == "знайдений"
    assert plain.status_code == 200 and plain.json()["lines"][0]["start_ms"] is None
    after = await track_of(environment, post_id)
    assert after.updated_at == track.updated_at and after.lyrics_edited_at is None
    assert await stored_lines(environment, track.id) == [(0, None, "старий", "lrclib")]
