"""The Mini App's read routes against a real Postgres: the list hides archived and draft posts,
pages by id with a cursor, costs one query, and the detail finds the other posts of a track.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from pisniar.config import Settings
from pisniar.db.models import Post, Track
from pisniar.db.session import LOCK_TIMEOUT_MS, build_engine, build_session_factory
from pisniar.db.states import PostStatus
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import issue_session_token
from pisniar.webapp.schemas import POST_ID_MAX

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:integration-token"
ADMIN_ID = 111
NOW = 1_800_000_000


@dataclass
class Seeded:
    track_id: int
    other_track_id: int
    visible_ids: list[int]
    archived_id: int
    draft_id: int


@dataclass
class WebappClient:
    http: httpx.AsyncClient
    engine: AsyncEngine
    session_factory: async_sessionmaker

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)}"}


@pytest_asyncio.fixture
async def webapp_client(migrated_test_database_url: str) -> AsyncIterator[WebappClient]:
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
        yield WebappClient(http=http, engine=engine, session_factory=session_factory)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(webapp_client: WebappClient) -> AsyncIterator[Seeded]:
    marker = uuid.uuid4().hex
    created_posts: list[int] = []
    async with webapp_client.session_factory() as session:
        track = Track(
            source_url=f"test://{marker}-a",
            source_url_normalized=f"test://{marker}-a",
            title="Тема",
            artists=[{"name": "Артист", "hashtag": None, "spotify_id": None}],
            year=2019,
            release_type="single",
        )
        other_track = Track(
            source_url=f"test://{marker}-b",
            source_url_normalized=f"test://{marker}-b",
            title="Інша",
            artists=[{"name": "Інший"}],
        )
        session.add_all([track, other_track])
        await session.flush()

        statuses = [
            PostStatus.RESOLVED,
            PostStatus.ARCHIVED,
            PostStatus.RENDERED,
            PostStatus.DRAFT,
            PostStatus.PUBLISHED,
            PostStatus.RESOLVING,
            PostStatus.FAILED,
        ]
        posts = []
        for index, status in enumerate(statuses):
            post = Post(
                track_id=track.id if index != 5 else other_track.id,
                created_by_user_id=1,
                status=status,
                caption="<b>x</b>" if status is PostStatus.RESOLVED else None,
            )
            if status is PostStatus.FAILED:
                post.failed_from_status = PostStatus.RESOLVING
            if status is PostStatus.RENDERED:
                post.preview_chat_id, post.preview_message_id = 1, 10_000_000 + index
                post.preview_params_hash = "a" * 64
            session.add(post)
            posts.append(post)
        await session.flush()
        await session.commit()

        seeded_ids = Seeded(
            track_id=track.id,
            other_track_id=other_track.id,
            visible_ids=[post.id for post in posts if post.status not in (PostStatus.ARCHIVED, PostStatus.DRAFT)],
            archived_id=posts[1].id,
            draft_id=posts[3].id,
        )
        created_posts = [post.id for post in posts]

    yield seeded_ids

    async with webapp_client.session_factory() as session:
        await session.execute(delete(Post).where(Post.id.in_(created_posts)))
        await session.execute(delete(Track).where(Track.id.in_([track.id, other_track.id])))
        await session.commit()


async def test_the_list_hides_archived_and_draft_posts_and_runs_newest_first(
    webapp_client: WebappClient, seeded: Seeded
) -> None:
    response = await webapp_client.http.get("/api/posts", params={"limit": 50}, headers=webapp_client.headers())

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert seeded.archived_id not in ids
    assert seeded.draft_id not in ids
    mine = [post_id for post_id in ids if post_id in seeded.visible_ids]
    assert mine == sorted(seeded.visible_ids, reverse=True)
    assert ids == sorted(ids, reverse=True)


async def test_a_page_carries_the_verdicts_and_the_video_flag(webapp_client: WebappClient, seeded: Seeded) -> None:
    response = await webapp_client.http.get("/api/posts", params={"limit": 50}, headers=webapp_client.headers())

    by_id = {item["id"]: item for item in response.json()["items"]}
    rendered_id = seeded.visible_ids[1]
    resolving_id = seeded.visible_ids[3]
    assert by_id[rendered_id]["has_video"] is True
    assert by_id[rendered_id]["editable"] is True
    assert by_id[resolving_id]["editable"] is False
    assert by_id[resolving_id]["not_editable_reason"]
    assert by_id[seeded.visible_ids[0]]["title"] == "Тема"
    assert by_id[seeded.visible_ids[0]]["artists"] == ["Артист"]


async def test_the_cursor_walks_the_list_without_gaps_or_repeats(webapp_client: WebappClient, seeded: Seeded) -> None:
    newest_first = sorted(seeded.visible_ids, reverse=True)
    first = await webapp_client.http.get("/api/posts", params={"limit": 2}, headers=webapp_client.headers())
    first_page = first.json()
    assert [item["id"] for item in first_page["items"]] == newest_first[:2]
    assert first_page["next_before_id"] == newest_first[1]

    second = await webapp_client.http.get(
        "/api/posts",
        params={"limit": 2, "before_id": first_page["next_before_id"]},
        headers=webapp_client.headers(),
    )
    assert [item["id"] for item in second.json()["items"]] == newest_first[2:4]


async def test_a_cursor_below_every_post_gives_an_empty_page_with_no_cursor(
    webapp_client: WebappClient, seeded: Seeded
) -> None:
    response = await webapp_client.http.get("/api/posts", params={"before_id": 1}, headers=webapp_client.headers())

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_before_id": None}


async def test_the_list_costs_exactly_one_query_whatever_the_page_size(
    webapp_client: WebappClient, seeded: Seeded
) -> None:
    statements: list[str] = []

    def count_statement(
        connection: object,
        cursor: object,
        statement: str,
        *rest: object,
    ) -> None:
        statements.append(statement)

    event.listen(webapp_client.engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        response = await webapp_client.http.get("/api/posts", params={"limit": 50}, headers=webapp_client.headers())
    finally:
        event.remove(webapp_client.engine.sync_engine, "before_cursor_execute", count_statement)

    assert response.status_code == 200
    assert len(response.json()["items"]) >= len(seeded.visible_ids)
    assert len(statements) == 1
    assert "FOR UPDATE" not in statements[0].upper()


async def test_the_detail_lists_the_other_posts_of_the_same_track(webapp_client: WebappClient, seeded: Seeded) -> None:
    resolved_id = seeded.visible_ids[0]

    response = await webapp_client.http.get(f"/api/posts/{resolved_id}", headers=webapp_client.headers())

    body = response.json()
    assert response.status_code == 200
    assert body["id"] == resolved_id
    assert body["title"] == "Тема"
    assert body["year"] == 2019
    assert body["artists"][0]["default_hashtag"] == "Артист"
    assert body["editable"] is True
    assert set(body["other_post_ids"]) == {
        post_id
        for post_id in [*seeded.visible_ids, seeded.archived_id, seeded.draft_id]
        if post_id != resolved_id and post_id != seeded.visible_ids[3]
    }
    assert body["expected_post_updated_at"]
    assert body["expected_track_updated_at"]


async def test_the_detail_of_an_archived_post_is_readable_with_its_sentence(
    webapp_client: WebappClient, seeded: Seeded
) -> None:
    response = await webapp_client.http.get(f"/api/posts/{seeded.archived_id}", headers=webapp_client.headers())

    assert response.status_code == 200
    assert response.json()["editable"] is False
    assert response.json()["not_editable_reason"] == "Пост архівовано — дій із ним немає."


async def test_the_detail_costs_two_queries_and_takes_no_lock(webapp_client: WebappClient, seeded: Seeded) -> None:
    statements: list[str] = []

    def count_statement(
        connection: object,
        cursor: object,
        statement: str,
        *rest: object,
    ) -> None:
        statements.append(statement)

    event.listen(webapp_client.engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        await webapp_client.http.get(f"/api/posts/{seeded.visible_ids[0]}", headers=webapp_client.headers())
    finally:
        event.remove(webapp_client.engine.sync_engine, "before_cursor_execute", count_statement)

    assert len(statements) == 2
    assert not any("FOR UPDATE" in statement.upper() for statement in statements)


@pytest.mark.parametrize("post_id", [POST_ID_MAX, 10**15])
async def test_an_id_that_fits_bigint_but_does_not_exist_is_a_404(webapp_client: WebappClient, post_id: int) -> None:
    response = await webapp_client.http.get(f"/api/posts/{post_id}", headers=webapp_client.headers())

    assert response.status_code == 404
    assert response.json()["code"] == "post_not_found"


async def test_an_id_beyond_bigint_never_reaches_the_database(
    webapp_client: WebappClient,
) -> None:
    response = await webapp_client.http.get(f"/api/posts/{POST_ID_MAX + 1}", headers=webapp_client.headers())

    assert response.status_code == 422


async def test_a_reader_is_not_blocked_by_a_writer_holding_the_post_row(
    webapp_client: WebappClient, seeded: Seeded
) -> None:
    """a plain SELECT does not wait on `FOR UPDATE`, so a render holding the lock never hangs it."""
    async with webapp_client.session_factory() as writer:
        await writer.get(Post, seeded.visible_ids[0], with_for_update=True)

        response = await webapp_client.http.get(f"/api/posts/{seeded.visible_ids[0]}", headers=webapp_client.headers())
        await writer.rollback()

    assert response.status_code == 200
