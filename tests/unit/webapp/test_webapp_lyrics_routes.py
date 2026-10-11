"""The lyrics read and save routes with the database faked: authorization before the database, the
request model, the one larger body ceiling, and the mapping of lock and database failures.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from support import ADMIN_ID
from test_webapp_edit_routes import SAVE_BODY, database_error
from test_webapp_posts_routes import bearer, make_client

from pisniar.config import Settings
from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import LYRICS_MAX_BODY_BYTES, MAX_BODY_BYTES, body_limit_of
from pisniar.webapp.database import DATABASE_UNAVAILABLE_MESSAGE, POST_BUSY_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import BIGINT_MAX, POST_ID_MAX, LyricLineView, LyricsView

PATH = "/api/posts/7/lyrics"
VERSION = "a" * 64
BODY = {
    "lines": [{"text": "раз", "start_ms": 1_000}, {"text": "два", "start_ms": None}],
    "expected_lyrics_version": VERSION,
}


def make_view() -> LyricsView:
    return LyricsView(
        lines=[LyricLineView(text="раз", start_ms=1_000, source="manual")],
        lyrics_version=VERSION,
        edited_by_admin=True,
        other_post_ids=[8],
        max_lines=MAX_LYRIC_LINES,
        max_line_length=MAX_LYRIC_TEXT_LENGTH,
        max_total_characters=20_000,
    )


def encoded(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def put(
    client: Any,
    content: bytes,
    headers: dict[str, str] | None = None,
) -> Any:
    return client.put(PATH, content=content, headers={"Content-Type": "application/json", **(headers or {})})


@pytest.mark.parametrize("method", ["get", "put"])
def test_an_anonymous_or_foreign_caller_is_refused_before_the_database(settings: Settings, method: str) -> None:
    client, session_factory = make_client(settings)

    anonymous = client.request(method, PATH, json=BODY)
    stranger = client.request(method, PATH, json=BODY, headers=bearer(999))

    assert (anonymous.status_code, stranger.status_code) == (401, 403)
    session_factory.assert_not_called()


def test_the_text_is_read_for_the_admin_who_asked(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock(return_value=make_view())
    monkeypatch.setattr(posts_module, "fetch_lyrics_view", fetch)
    client, _ = make_client(settings)

    response = client.get(PATH, headers=bearer())

    assert response.status_code == 200
    assert response.json()["lyrics_version"] == VERSION
    assert response.json()["lines"] == [{"text": "раз", "start_ms": 1_000, "source": "manual"}]
    args = fetch.await_args_list[0].args
    assert (args[1], args[2]) == (7, ADMIN_ID)


def test_a_save_replaces_the_text_and_returns_the_fresh_view(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = AsyncMock(return_value=make_view())
    monkeypatch.setattr(posts_module, "replace_lyrics", replace)
    client, _ = make_client(settings)

    response = client.put(PATH, json=BODY, headers=bearer())

    assert response.status_code == 200
    assert response.json()["edited_by_admin"] is True
    args = replace.await_args_list[0].args
    assert (args[1], args[3]) == (7, ADMIN_ID)
    assert args[2].expected_lyrics_version == VERSION
    assert [(line.text, line.start_ms) for line in args[2].lines] == [("раз", 1_000), ("два", None)]


@pytest.mark.parametrize("method", ["get", "put"])
def test_a_missing_post_is_a_404(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setattr(posts_module, "fetch_lyrics_view", AsyncMock(return_value=None))
    monkeypatch.setattr(posts_module, "replace_lyrics", AsyncMock(return_value=None))
    client, _ = make_client(settings)

    response = client.request(method, PATH, json=BODY, headers=bearer())

    assert response.status_code == 404
    assert response.json()["code"] == "post_not_found"


def test_a_refusal_of_the_save_keeps_its_code_and_sentence(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = ApiProblem(422, "invalid_lyrics", "Рядок 2 задовгий.")
    monkeypatch.setattr(posts_module, "replace_lyrics", AsyncMock(side_effect=refusal))
    client, _ = make_client(settings)

    response = client.put(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()) == (
        422,
        {"code": "invalid_lyrics", "message": "Рядок 2 задовгий."},
    )


@pytest.mark.parametrize("sqlstate", ["55P03", "57014"])
def test_a_refused_or_timed_out_lock_is_post_busy(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
) -> None:
    monkeypatch.setattr(posts_module, "replace_lyrics", AsyncMock(side_effect=database_error(sqlstate)))
    client, _ = make_client(settings)

    response = client.put(PATH, json=BODY, headers=bearer())

    assert (response.status_code, response.json()) == (
        409,
        {"code": "post_busy", "message": POST_BUSY_MESSAGE},
    )


def test_any_other_database_failure_is_a_503_and_never_a_401(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posts_module, "replace_lyrics", AsyncMock(side_effect=database_error("08006")))
    monkeypatch.setattr(posts_module, "fetch_lyrics_view", AsyncMock(side_effect=database_error("08006")))
    client, _ = make_client(settings)

    save = client.put(PATH, json=BODY, headers=bearer())
    read = client.get(PATH, headers=bearer())

    for response in (save, read):
        assert (response.status_code, response.json()) == (
            503,
            {"code": "database_unavailable", "message": DATABASE_UNAVAILABLE_MESSAGE},
        )


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "extra": 1},
        {**BODY, "expected_lyrics_version": "short"},
        {**BODY, "expected_lyrics_version": "A" * 64},
        {"lines": BODY["lines"]},
        {**BODY, "lines": [{"text": "раз"}]},
        {**BODY, "lines": [{"text": "раз", "start_ms": True}]},
        {**BODY, "lines": [{"text": "раз", "start_ms": 1.5}]},
        {**BODY, "lines": [{"text": "раз", "start_ms": "1000"}]},
        {**BODY, "lines": [{"text": "раз", "start_ms": BIGINT_MAX + 1}]},
        {**BODY, "lines": [{"text": "раз", "start_ms": 1, "extra": 1}]},
        {**BODY, "lines": [{"text": "р", "start_ms": None}] * (MAX_LYRIC_LINES + 1)},
        {**BODY, "lines": None},
    ],
)
def test_the_request_model_refuses_a_bad_body_without_echoing_it(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    replace = AsyncMock(return_value=make_view())
    monkeypatch.setattr(posts_module, "replace_lyrics", replace)
    client, _ = make_client(settings)

    response = put(client, encoded(body), bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")
    replace.assert_not_awaited()


def test_the_length_of_a_line_is_judged_by_the_rule_not_by_the_request_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = ApiProblem(422, "invalid_lyrics", "Рядок 1 задовгий: 501 символів.")
    replace = AsyncMock(side_effect=refusal)
    monkeypatch.setattr(posts_module, "replace_lyrics", replace)
    client, _ = make_client(settings)
    long_line = {"text": "а" * (MAX_LYRIC_TEXT_LENGTH + 1), "start_ms": None}

    response = client.put(PATH, json={**BODY, "lines": [long_line]}, headers=bearer())

    assert (response.status_code, response.json()["code"]) == (422, "invalid_lyrics")
    replace.assert_awaited_once()


def test_an_empty_text_and_a_text_of_untimed_lines_are_valid_requests(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = AsyncMock(return_value=make_view())
    monkeypatch.setattr(posts_module, "replace_lyrics", replace)
    client, _ = make_client(settings)

    empty = client.put(PATH, json={**BODY, "lines": []}, headers=bearer())
    untimed = client.put(PATH, json={**BODY, "lines": [{"text": "раз", "start_ms": None}]}, headers=bearer())

    assert (empty.status_code, untimed.status_code) == (200, 200)


def test_a_post_id_beyond_bigint_is_a_422_before_the_database(
    settings: Settings,
) -> None:
    client, session_factory = make_client(settings)

    response = client.get(f"/api/posts/{POST_ID_MAX + 1}/lyrics", headers=bearer())

    assert response.status_code == 422
    session_factory.assert_not_called()


def lyrics_body_of(size_bytes: int) -> bytes:
    """a valid-looking save of about `size_bytes`: lines of Cyrillic text, 500 characters each."""
    line = {"text": "ж" * MAX_LYRIC_TEXT_LENGTH, "start_ms": None}
    count = max(1, size_bytes // len(encoded(line)))
    return encoded({"lines": [line] * count, "expected_lyrics_version": VERSION})


def test_a_text_beyond_the_ordinary_ceiling_but_under_the_lyrics_one_reaches_the_route(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = AsyncMock(return_value=make_view())
    monkeypatch.setattr(posts_module, "replace_lyrics", replace)
    client, _ = make_client(settings)
    body = lyrics_body_of(60 * 1024)
    assert MAX_BODY_BYTES < len(body) < LYRICS_MAX_BODY_BYTES

    response = put(client, body, bearer())

    assert response.status_code == 200
    replace.assert_awaited_once()


def test_a_text_beyond_the_lyrics_ceiling_is_413_before_the_route_runs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = AsyncMock(return_value=make_view())
    monkeypatch.setattr(posts_module, "replace_lyrics", replace)
    client, session_factory = make_client(settings)
    body = lyrics_body_of(LYRICS_MAX_BODY_BYTES + 1_000)
    assert len(body) > LYRICS_MAX_BODY_BYTES

    response = put(client, body, bearer())

    assert (response.status_code, response.json()["code"]) == (413, "body_too_large")
    replace.assert_not_awaited()
    session_factory.assert_not_called()


def test_the_exact_lyrics_ceiling_is_accepted_and_one_byte_more_is_not(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posts_module, "replace_lyrics", AsyncMock(return_value=make_view()))
    client, _ = make_client(settings)
    padding = b" " * LYRICS_MAX_BODY_BYTES

    at_limit = put(client, padding[:LYRICS_MAX_BODY_BYTES], bearer())
    over_limit = put(client, padding + b" ", bearer())

    assert at_limit.status_code != 413
    assert over_limit.status_code == 413


def test_the_fields_route_keeps_the_ordinary_ceiling(settings: Settings) -> None:
    client, _ = make_client(settings)
    padded = encoded({**SAVE_BODY, "title": "ж" * (17 * 1024)})

    response = client.put(
        "/api/posts/7/fields",
        content=padded,
        headers={**bearer(), "Content-Type": "application/json"},
    )

    assert (response.status_code, response.json()["code"]) == (413, "body_too_large")


def test_a_lyrics_body_without_a_content_length_is_still_refused(settings: Settings) -> None:
    client, _ = make_client(settings)

    def chunks() -> Any:
        yield b'{"lines": []}'

    response = client.put(PATH, content=chunks(), headers=bearer())

    assert (response.status_code, response.json()["code"]) == (411, "length_required")


@pytest.mark.parametrize(
    ("method", "path", "limit"),
    [
        ("PUT", "/api/posts/7/lyrics", LYRICS_MAX_BODY_BYTES),
        ("PUT", "/api/posts/123456/lyrics", LYRICS_MAX_BODY_BYTES),
        ("POST", "/api/posts/7/lyrics", MAX_BODY_BYTES),
        ("PUT", "/api/posts/7/lyrics/", MAX_BODY_BYTES),
        ("PUT", "/api/posts/7/lyrics/synced", MAX_BODY_BYTES),
        ("PUT", "/api/posts/x/lyrics", MAX_BODY_BYTES),
        ("PUT", "/api/posts/7/fields", MAX_BODY_BYTES),
        ("PUT", "/x/api/posts/7/lyrics", MAX_BODY_BYTES),
        ("POST", "/api/session", MAX_BODY_BYTES),
    ],
)
def test_one_method_and_path_have_the_larger_ceiling_and_every_other_the_ordinary_one(
    method: str,
    path: str,
    limit: int,
) -> None:
    assert body_limit_of(method, path) == limit
