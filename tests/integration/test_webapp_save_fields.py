"""Saving fields through the Mini App against a real Postgres: what is written, the optimistic
check, the NOWAIT and `lock_timeout` refusals, and the `youtubeMusic` guard over sibling posts.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.session import build_engine, build_session_factory
from pisniar.db.states import PostStatus
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import issue_session_token

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 111
NOW = 1_800_000_000
LOCK_TIMEOUT_MS = 400
ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"
SPOTIFY_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
TIDAL_URL = "https://listen.tidal.com/track/1"
MUSIC_URL = "https://music.youtube.com/watch?v=n4bPzdIs_nc"
OTHER_MUSIC_URL = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
MEDIA_STATUSES = [
    PostStatus.DOWNLOADING,
    PostStatus.DOWNLOADED,
    PostStatus.RENDERING,
    PostStatus.EDITING,
]


@dataclass
class Environment:
    http: httpx.AsyncClient
    engine: AsyncEngine
    session_factory: async_sessionmaker

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)}"}

    async def detail(self, post_id: int) -> dict[str, Any]:
        response = await self.http.get(f"/api/posts/{post_id}", headers=self.headers())
        assert response.status_code == 200
        return response.json()

    async def form_of(self, post_id: int, **overrides: Any) -> dict[str, Any]:
        """the body the editor would send back for the post as it is now."""
        detail = await self.detail(post_id)
        body: dict[str, Any] = {
            "title": detail["title"],
            "artists": [
                {"name": artist["name"], "hashtag": artist["hashtag"]}
                for artist in detail["artists"]
            ],
            "languages": detail["languages"],
            "genres": [
                {"name": genre["name"], "hashtag": genre["hashtag"]} for genre in detail["genres"]
            ],
            "year": detail["year"],
            "release_type": detail["release_type"],
            "release_title": detail["release_title"],
            "album_link": detail["album_url"],
            "platform_links": {key: link["url"] for key, link in detail["platform_links"].items()},
            "expected_post_updated_at": detail["expected_post_updated_at"],
            "expected_track_updated_at": detail["expected_track_updated_at"],
        }
        body.update(overrides)
        return body

    async def save(self, post_id: int, body: dict[str, Any]) -> httpx.Response:
        return await self.http.put(
            f"/api/posts/{post_id}/fields", json=body, headers=self.headers()
        )


@dataclass
class Seeded:
    track_id: int
    post_id: int
    sibling_ids: dict[PostStatus, int]
    publishing_id: int


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
    app = create_webapp(settings, clock=lambda: float(NOW), session_factory=session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield Environment(http=http, engine=engine, session_factory=session_factory)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(environment: Environment) -> AsyncIterator[Seeded]:
    marker = uuid.uuid4().hex
    post_ids: list[int] = []
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
                "tidal": {"url": TIDAL_URL, "source": "songlink", "match": "aggregator"},
                "youtubeMusic": {"url": MUSIC_URL, "source": "ytmusic", "match": "confirmed"},
            },
        )
        session.add(track)
        await session.flush()

        post = Post(track_id=track.id, created_by_user_id=1, status=PostStatus.RESOLVED)
        publishing = Post(track_id=track.id, created_by_user_id=1, status=PostStatus.PUBLISHING)
        session.add_all([post, publishing])
        siblings = {
            status: Post(track_id=track.id, created_by_user_id=1, status=status)
            for status in MEDIA_STATUSES
        }
        session.add_all(siblings.values())
        await session.flush()
        await session.commit()

        result = Seeded(
            track_id=track.id,
            post_id=post.id,
            sibling_ids={status: row.id for status, row in siblings.items()},
            publishing_id=publishing.id,
        )
        post_ids = [post.id, publishing.id, *result.sibling_ids.values()]

    yield result

    async with environment.session_factory() as session:
        await session.execute(delete(Post).where(Post.id.in_(post_ids)))
        await session.execute(delete(Track).where(Track.id == result.track_id))
        await session.commit()


async def stored_track(environment: Environment, track_id: int) -> Track:
    async with environment.session_factory() as session:
        track = await session.get(Track, track_id)
        assert track is not None
        return track


def links_of(track: Track) -> dict[str, dict[str, Any]]:
    return cast("dict[str, dict[str, Any]]", track.platform_links)


async def remove_siblings(environment: Environment, seeded: Seeded) -> None:
    async with environment.session_factory() as session:
        await session.execute(delete(Post).where(Post.id.in_(list(seeded.sibling_ids.values()))))
        await session.commit()


async def test_a_save_writes_the_fields_marks_the_changed_one_and_returns_the_caption_sha(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title="Нова назва", year=None)

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 200
    saved = response.json()
    assert saved["title"] == "Нова назва"
    assert saved["manual_fields"] == ["title", "year"]
    assert len(saved["caption"]["sha256"]) == 64
    track = await stored_track(environment, seeded.track_id)
    assert (track.title, track.year) == ("Нова назва", None)
    assert track.manual_fields == ["title", "year"]
    assert cast("list[dict[str, Any]]", track.artists)[0]["spotify_id"] == "a1"
    assert saved["expected_track_updated_at"] != body["expected_track_updated_at"]


async def test_a_save_without_changes_writes_nothing_and_keeps_both_stamps(
    environment: Environment, seeded: Seeded
) -> None:
    before = await environment.detail(seeded.post_id)
    body = await environment.form_of(seeded.post_id)

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 200
    after = response.json()
    assert after["expected_post_updated_at"] == before["expected_post_updated_at"]
    assert after["expected_track_updated_at"] == before["expected_track_updated_at"]
    assert after["manual_fields"] == []


async def test_a_stale_post_version_is_a_409_and_nothing_is_written(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id)
    async with environment.session_factory() as session:
        await session.execute(
            text("UPDATE post SET updated_at = now() + interval '1 second' WHERE id = :id"),
            {"id": seeded.post_id},
        )
        await session.commit()

    response = await environment.save(seeded.post_id, {**body, "title": "Нова"})

    assert response.status_code == 409
    assert response.json()["code"] == "stale_data"
    assert (await stored_track(environment, seeded.track_id)).title == "Тема"


async def test_a_stale_track_version_is_a_409_and_nothing_is_written(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id)
    async with environment.session_factory() as session:
        track = await session.get(Track, seeded.track_id)
        assert track is not None
        track.release_title = "Змінено в іншому місці"
        await session.commit()

    response = await environment.save(seeded.post_id, {**body, "title": "Нова"})

    assert response.status_code == 409
    assert response.json()["code"] == "stale_data"
    assert (await stored_track(environment, seeded.track_id)).title == "Тема"


async def test_a_post_locked_elsewhere_is_refused_at_once(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title="Нова")

    async with environment.session_factory() as holder:
        await holder.execute(select(Post).where(Post.id == seeded.post_id).with_for_update())
        started = time.monotonic()
        response = await environment.save(seeded.post_id, body)
        elapsed = time.monotonic() - started

    assert response.status_code == 409
    assert response.json()["code"] == "post_busy"
    assert elapsed < LOCK_TIMEOUT_MS / 1000
    assert (await stored_track(environment, seeded.track_id)).title == "Тема"


async def test_a_track_locked_elsewhere_gives_up_at_the_lock_timeout(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title="Нова")

    async with environment.session_factory() as holder:
        await holder.execute(
            select(Track).where(Track.id == seeded.track_id).with_for_update(key_share=True)
        )
        response = await environment.save(seeded.post_id, body)

    assert response.status_code == 409
    assert response.json()["code"] == "post_busy"
    assert (await stored_track(environment, seeded.track_id)).title == "Тема"


async def test_the_database_is_usable_after_a_refused_lock(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title="Нова")
    async with environment.session_factory() as holder:
        await holder.execute(select(Post).where(Post.id == seeded.post_id).with_for_update())
        assert (await environment.save(seeded.post_id, body)).status_code == 409

    assert (await environment.save(seeded.post_id, body)).status_code == 200


@pytest.mark.parametrize("status", MEDIA_STATUSES)
async def test_the_youtube_music_link_is_held_while_a_post_of_the_track_is_in_media(
    environment: Environment, seeded: Seeded, status: PostStatus
) -> None:
    for other_status, post_id in seeded.sibling_ids.items():
        if other_status is not status:
            async with environment.session_factory() as session:
                await session.execute(delete(Post).where(Post.id == post_id))
                await session.commit()
    body = await environment.form_of(seeded.post_id)
    body["platform_links"]["youtubeMusic"] = OTHER_MUSIC_URL

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 409
    assert response.json()["code"] == "media_in_progress"
    track = await stored_track(environment, seeded.track_id)
    assert links_of(track)["youtubeMusic"]["url"] == MUSIC_URL


async def test_removing_the_youtube_music_link_is_held_too(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id)
    del body["platform_links"]["youtubeMusic"]

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 409
    assert response.json()["code"] == "media_in_progress"


async def test_other_fields_and_links_stay_editable_while_a_media_post_exists(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title="Нова")
    body["platform_links"]["tidal"] = "https://listen.tidal.com/track/2"

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 200
    assert response.json()["platform_links"]["tidal"]["source"] == "manual"


async def test_the_youtube_music_link_changes_once_no_post_is_in_media(
    environment: Environment, seeded: Seeded
) -> None:
    await remove_siblings(environment, seeded)
    body = await environment.form_of(seeded.post_id)
    body["platform_links"]["youtubeMusic"] = OTHER_MUSIC_URL

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 200
    track = await stored_track(environment, seeded.track_id)
    assert links_of(track)["youtubeMusic"]["url"] == OTHER_MUSIC_URL
    assert links_of(track)["youtubeMusic"]["source"] == "manual"


async def test_a_removed_platform_is_remembered_and_adding_it_back_lifts_the_removal(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id)
    del body["platform_links"]["tidal"]
    assert (await environment.save(seeded.post_id, body)).status_code == 200
    track = await stored_track(environment, seeded.track_id)
    assert track.removed_platforms == ["tidal"]
    assert "tidal" not in track.platform_links

    again = await environment.form_of(seeded.post_id)
    again["platform_links"]["tidal"] = TIDAL_URL
    response = await environment.save(seeded.post_id, again)

    assert response.status_code == 200
    track = await stored_track(environment, seeded.track_id)
    assert track.removed_platforms == []
    assert links_of(track)["tidal"] == {
        "url": TIDAL_URL,
        "source": "manual",
        "match": "manual",
    }


async def test_going_back_to_automatic_takes_the_name_off_the_list(
    environment: Environment, seeded: Seeded
) -> None:
    first = await environment.form_of(seeded.post_id, year=2000)
    assert (await environment.save(seeded.post_id, first)).json()["manual_fields"] == ["year"]

    second = await environment.form_of(seeded.post_id, revert_to_automatic=["year"])
    response = await environment.save(seeded.post_id, second)

    assert response.status_code == 200
    assert response.json()["manual_fields"] == []
    assert response.json()["year"] == 2000


async def test_a_post_that_cannot_be_edited_is_a_409_without_naming_its_state(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title="Нова")

    response = await environment.save(seeded.publishing_id, body)

    assert response.status_code == 409
    assert response.json()["code"] == "not_editable"
    assert "publishing" not in response.json()["message"].lower()


async def test_invalid_fields_are_a_422_listing_each_and_write_nothing(
    environment: Environment, seeded: Seeded
) -> None:
    body = await environment.form_of(seeded.post_id, title=" ", year=1500)
    body["platform_links"]["tidal"] = "https://evil.example/track/1"

    response = await environment.save(seeded.post_id, body)

    assert response.status_code == 422
    fields = {error["field"] for error in response.json()["errors"]}
    assert {"title", "year", "platform_links.tidal"} <= fields
    assert (await stored_track(environment, seeded.track_id)).title == "Тема"


async def test_a_missing_post_is_a_404(environment: Environment, seeded: Seeded) -> None:
    body = await environment.form_of(seeded.post_id)

    response = await environment.save(seeded.post_id + 10_000_000, body)

    assert response.status_code == 404


async def test_a_preview_shows_the_new_caption_and_writes_nothing(
    environment: Environment, seeded: Seeded
) -> None:
    before = await environment.detail(seeded.post_id)
    body = await environment.form_of(seeded.post_id, title="Попередній перегляд")
    for stamp in ("expected_post_updated_at", "expected_track_updated_at"):
        del body[stamp]

    response = await environment.http.post(
        f"/api/posts/{seeded.post_id}/preview", json=body, headers=environment.headers()
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["differs_from_stored"] is True
    assert "Попередній перегляд" in "".join(
        segment["text"] or "" for segment in preview["caption"]["segments"]
    )
    after = await environment.detail(seeded.post_id)
    assert after["title"] == before["title"]
    assert after["expected_track_updated_at"] == before["expected_track_updated_at"]
    assert after["expected_post_updated_at"] == before["expected_post_updated_at"]
