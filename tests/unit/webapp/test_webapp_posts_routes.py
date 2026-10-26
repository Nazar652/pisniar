"""The read routes over `TestClient` with the database faked: parameter bounds answer 422 and never 500, a missing post
is 404, a database failure is a 503 with no details, and authorization is decided before the database is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from support import ADMIN_ID, BOT_TOKEN, NOW

from pisniar.config import Settings
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import issue_session_token
from pisniar.webapp.database import DATABASE_UNAVAILABLE_MESSAGE
from pisniar.webapp.schemas import (
    POST_ID_MAX,
    CoverPublicationAvailability,
    FragmentAvailability,
    PostDetail,
    PostListResponse,
)

STAMP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def make_client(settings: Settings, with_database: bool = True) -> tuple[TestClient, MagicMock]:
    session_factory = MagicMock(return_value=FakeSession())
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory if with_database else None,
    )
    return TestClient(app), session_factory


def bearer(user_id: int = ADMIN_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_session_token(user_id, BOT_TOKEN, NOW, 3600)}"}


def empty_page() -> PostListResponse:
    return PostListResponse(items=[], next_before_id=None)


def make_detail() -> PostDetail:
    return PostDetail(
        id=7,
        has_video=False,
        status_label="Чернетка готова",
        editable=True,
        not_editable_reason=None,
        fragment=FragmentAvailability(
            start_ms=None,
            end_ms=None,
            choosable=False,
            preparable=False,
            preparing=False,
            cancellable=False,
            reason="Фрагмент ще не обрано.",
        ),
        cover_publication=CoverPublicationAvailability(allowed=True, reason=None),
        created_at=STAMP,
        published_at=None,
        title="Тема",
        artists=[],
        languages=["українська"],
        genres=[],
        year=None,
        release_type=None,
        release_title=None,
        album_id_spotify=None,
        album_url=None,
        platform_links={},
        manual_fields=[],
        removed_platforms=[],
        caption=None,
        stored_caption_html=None,
        stored_caption_matches=False,
        other_post_ids=[],
        expected_post_updated_at=STAMP,
        expected_track_updated_at=STAMP,
    )


@pytest.mark.parametrize("path", ["/api/posts", "/api/posts/7", "/api/meta"])
def test_every_read_route_refuses_an_anonymous_caller_before_the_database(settings: Settings, path: str) -> None:
    client, session_factory = make_client(settings)

    assert client.get(path).status_code == 401
    assert client.get(path, headers=bearer(999)).status_code == 403
    session_factory.assert_not_called()


def test_the_list_defaults_to_twenty_posts_from_the_newest(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock(return_value=empty_page())
    monkeypatch.setattr(posts_module, "fetch_post_page", fetch)
    client, _ = make_client(settings)

    response = client.get("/api/posts", headers=bearer())

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_before_id": None}
    assert fetch.await_args_list[0].args[1:] == (None, 20)


def test_the_list_passes_the_cursor_and_the_page_size_through(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = AsyncMock(return_value=empty_page())
    monkeypatch.setattr(posts_module, "fetch_post_page", fetch)
    client, _ = make_client(settings)

    client.get("/api/posts?before_id=30&limit=50", headers=bearer())

    assert fetch.await_args_list[0].args[1:] == (30, 50)


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=-1",
        "limit=abc",
        "before_id=0",
        "before_id=-5",
        "before_id=abc",
        f"before_id={POST_ID_MAX + 1}",
        "before_id=99999999999999999999999",
    ],
)
def test_an_out_of_range_list_parameter_is_a_422_and_never_a_500(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    fetch = AsyncMock(return_value=empty_page())
    monkeypatch.setattr(posts_module, "fetch_post_page", fetch)
    client, _ = make_client(settings)

    response = client.get(f"/api/posts?{query}", headers=bearer())

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "message": "Некоректний запит."}
    fetch.assert_not_awaited()


def test_the_largest_bigint_is_still_a_valid_cursor(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posts_module, "fetch_post_page", AsyncMock(return_value=empty_page()))
    client, _ = make_client(settings)

    assert client.get(f"/api/posts?before_id={POST_ID_MAX}", headers=bearer()).status_code == 200


def test_a_post_is_returned_by_its_id(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock(return_value=make_detail())
    monkeypatch.setattr(posts_module, "fetch_post_detail", fetch)
    client, _ = make_client(settings)

    response = client.get("/api/posts/7", headers=bearer())

    assert response.status_code == 200
    assert response.json()["id"] == 7
    assert response.json()["expected_post_updated_at"].startswith("2026-10-02T12:00:00")
    assert fetch.await_args_list[0].args[1] == 7


def test_a_missing_post_is_a_404_with_a_sentence(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posts_module, "fetch_post_detail", AsyncMock(return_value=None))
    client, _ = make_client(settings)

    response = client.get("/api/posts/7", headers=bearer())

    assert response.status_code == 404
    assert response.json() == {"code": "post_not_found", "message": "Пост не знайдено."}


@pytest.mark.parametrize("post_id", ["0", "-1", "abc", "1.5", str(POST_ID_MAX + 1), "99999999999999999999999"])
def test_a_post_id_outside_bigint_or_not_a_number_is_a_422_and_never_a_500(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    post_id: str,
) -> None:
    fetch = AsyncMock(return_value=make_detail())
    monkeypatch.setattr(posts_module, "fetch_post_detail", fetch)
    client, _ = make_client(settings)

    response = client.get(f"/api/posts/{post_id}", headers=bearer())

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    fetch.assert_not_awaited()


@pytest.mark.parametrize("error", [OperationalError("SELECT", {}, Exception("secret-dsn")), OSError("refused")])
def test_a_database_failure_is_a_503_that_leaks_nothing(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(posts_module, "fetch_post_page", AsyncMock(side_effect=error))
    client, _ = make_client(settings)

    response = client.get("/api/posts", headers=bearer())

    assert response.status_code == 503
    assert response.json() == {
        "code": "database_unavailable",
        "message": DATABASE_UNAVAILABLE_MESSAGE,
    }
    assert "secret-dsn" not in response.text
    assert "refused" not in response.text


def test_a_database_failure_never_looks_like_an_authorization_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posts_module, "fetch_post_page", AsyncMock(side_effect=OSError("connection refused")))
    client, _ = make_client(settings)

    assert client.get("/api/posts", headers=bearer()).status_code == 503
    assert client.get("/api/posts", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_without_a_session_factory_the_data_routes_are_a_503(settings: Settings) -> None:
    client, _ = make_client(settings, with_database=False)

    assert client.get("/api/posts", headers=bearer()).status_code == 503
    assert client.get("/api/posts/7", headers=bearer()).status_code == 503
    assert client.get("/api/meta", headers=bearer()).status_code == 200


def test_the_meta_route_serves_the_policy_tables(settings: Settings) -> None:
    client, session_factory = make_client(settings)

    body = client.get("/api/meta", headers=bearer()).json()

    assert {rule["key"] for rule in body["platforms"]} >= {"spotify", "youtubeMusic", "deezer"}
    assert "tidal" not in {rule["key"] for rule in body["platforms"]}
    assert body["caption_limit"] == 1024
    assert body["manual_field_names"][0] == "title"
    session_factory.assert_not_called()
