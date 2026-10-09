"""The signed audio route over `TestClient` and a temporary volume: Range answers, the type
table, every token refusal, a vanished file, and a log that never holds the token.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog.testing
from fastapi.testclient import TestClient
from support import ADMIN_ID, BOT_TOKEN, NOW

from pisniar.config import Settings
from pisniar.webapp import audio as audio_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.audio import audio_content_type, locate_audio_file
from pisniar.webapp.auth import MediaClaims, issue_media_token, issue_session_token

VIDEO_ID = "n4bPzdIs_nc"
POST_ID = 7
TRACK_ID = 3
CONTENT = bytes(range(256)) * 4


def token_for(
    *,
    post_id: int = POST_ID,
    user_id: int = ADMIN_ID,
    video_id: str = VIDEO_ID,
    now: int = NOW,
    bot_token: str = BOT_TOKEN,
) -> str:
    claims = MediaClaims(user_id=user_id, post_id=post_id, track_id=TRACK_ID, video_id=video_id)
    return issue_media_token(claims, bot_token, now, 900)


def write_audio(media_root: Path, extension: str = "webm") -> Path:
    path = media_root / "audio" / str(TRACK_ID) / f"{VIDEO_ID}.{extension}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CONTENT)
    return path


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def audio_client(settings: Settings, clock: Callable[[], float], media_root: Path) -> TestClient:
    configured = settings.model_copy(update={"media_root": media_root})
    return TestClient(create_webapp(configured, clock=clock))


def audio_url(post_id: int = POST_ID, token: str | None = None) -> str:
    return f"/api/posts/{post_id}/audio?token={token or token_for(post_id=post_id)}"


def test_the_whole_file_is_served_with_the_pinned_headers(
    audio_client: TestClient, media_root: Path
) -> None:
    write_audio(media_root)

    response = audio_client.get(audio_url())

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-type"] == "audio/webm"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "content-disposition" not in response.headers


def test_a_range_request_is_answered_with_206_and_content_range(
    audio_client: TestClient, media_root: Path
) -> None:
    write_audio(media_root)

    response = audio_client.get(audio_url(), headers={"Range": "bytes=0-99"})

    assert response.status_code == 206
    assert response.content == CONTENT[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(CONTENT)}"
    assert response.headers["content-type"] == "audio/webm"
    assert response.headers["cache-control"] == "no-store"


def test_an_open_ended_range_reads_to_the_end(audio_client: TestClient, media_root: Path) -> None:
    write_audio(media_root)

    response = audio_client.get(audio_url(), headers={"Range": "bytes=1000-"})

    assert response.status_code == 206
    assert response.content == CONTENT[1000:]


def test_a_range_beyond_the_file_is_416(audio_client: TestClient, media_root: Path) -> None:
    write_audio(media_root)

    response = audio_client.get(audio_url(), headers={"Range": "bytes=5000-6000"})

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"


@pytest.mark.parametrize(
    ("extension", "content_type"),
    [
        ("webm", "audio/webm"),
        ("m4a", "audio/mp4"),
        ("mp4", "audio/mp4"),
        ("opus", "audio/ogg"),
        ("ogg", "audio/ogg"),
        ("mp3", "audio/mpeg"),
    ],
)
def test_the_content_type_comes_from_the_table(
    audio_client: TestClient, media_root: Path, extension: str, content_type: str
) -> None:
    write_audio(media_root, extension)

    assert audio_client.get(audio_url()).headers["content-type"] == content_type


def test_an_extension_outside_the_table_is_409(audio_client: TestClient, media_root: Path) -> None:
    write_audio(media_root, "mkv")

    response = audio_client.get(audio_url())

    assert response.status_code == 409
    assert response.json()["code"] == "audio_format_unsupported"


def test_a_file_that_vanished_is_404_audio_gone(audio_client: TestClient) -> None:
    response = audio_client.get(audio_url())

    assert response.status_code == 404
    assert response.json()["code"] == "audio_gone"


def test_the_peaks_file_is_never_served_as_audio(
    audio_client: TestClient, media_root: Path
) -> None:
    peaks = media_root / "audio" / str(TRACK_ID) / "peaks" / f"{VIDEO_ID}.json"
    peaks.parent.mkdir(parents=True)
    peaks.write_text("{}", encoding="utf-8")

    assert audio_client.get(audio_url()).status_code == 404


def test_a_session_token_is_refused_as_a_media_token(
    audio_client: TestClient, media_root: Path
) -> None:
    write_audio(media_root)
    session = issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)

    response = audio_client.get(audio_url(token=session))

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_media_token_is_refused_as_a_session_token(audio_client: TestClient) -> None:
    response = audio_client.get("/api/me", headers={"Authorization": f"Bearer {token_for()}"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        token_for(now=NOW - 7200),
        token_for(bot_token="999:other"),
        "not-a-token",
        "a.b",
    ],
)
def test_an_expired_forged_or_malformed_token_is_401(
    audio_client: TestClient, media_root: Path, token: str
) -> None:
    write_audio(media_root)

    assert audio_client.get(audio_url(token=token)).status_code == 401


def test_a_rejected_mismatch_and_a_rejected_stranger_are_logged_with_their_ids(
    settings: Settings, clock: Callable[[], float], media_root: Path
) -> None:
    narrowed = settings.model_copy(update={"admin_ids": [222], "media_root": media_root})
    narrowed_client = TestClient(create_webapp(narrowed, clock=clock))

    with structlog.testing.capture_logs() as captured:
        narrowed_client.get(audio_url(post_id=POST_ID + 1, token=token_for()))
        narrowed_client.get(audio_url())

    rejections = [entry for entry in captured if entry["event"] == "media_token_rejected"]
    assert rejections == [
        {
            "event": "media_token_rejected",
            "log_level": "warning",
            "reason": "post_mismatch",
            "post_id": POST_ID + 1,
            "token_post_id": POST_ID,
            "user_id": ADMIN_ID,
        },
        {
            "event": "media_token_rejected",
            "log_level": "warning",
            "reason": "not_admin",
            "post_id": POST_ID,
            "user_id": ADMIN_ID,
        },
    ]


def test_a_token_for_another_post_is_401(audio_client: TestClient, media_root: Path) -> None:
    write_audio(media_root)

    assert audio_client.get(audio_url(post_id=POST_ID + 1, token=token_for())).status_code == 401


def test_a_token_of_a_user_who_is_no_longer_an_admin_is_403(
    settings: Settings, clock: Callable[[], float], media_root: Path
) -> None:
    write_audio(media_root)
    narrowed = settings.model_copy(update={"admin_ids": [222], "media_root": media_root})
    narrowed_client = TestClient(create_webapp(narrowed, clock=clock))

    response = narrowed_client.get(audio_url())

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_missing_or_oversized_token_is_422(audio_client: TestClient) -> None:
    assert audio_client.get(f"/api/posts/{POST_ID}/audio").status_code == 422
    assert audio_client.get(f"/api/posts/{POST_ID}/audio?token={'a' * 513}").status_code == 422


def test_a_request_is_logged_with_its_status_and_never_with_the_token(
    audio_client: TestClient, media_root: Path
) -> None:
    write_audio(media_root)
    token = token_for()

    with structlog.testing.capture_logs() as captured:
        audio_client.get(audio_url(token=token))
        audio_client.get(audio_url(token=token), headers={"Range": "bytes=0-9"})

    served = [entry for entry in captured if entry["event"] == "fragment_audio_served"]
    assert [(entry["http_status"], entry["ranged"]) for entry in served] == [
        (200, False),
        (206, True),
    ]
    assert all(entry["post_id"] == POST_ID and entry["user_id"] == ADMIN_ID for entry in served)
    assert token not in json.dumps(captured, default=str)


def test_a_refused_token_is_logged_with_a_reason_and_without_the_token(
    audio_client: TestClient,
) -> None:
    token = token_for(bot_token="999:other")

    with structlog.testing.capture_logs() as captured:
        audio_client.get(audio_url(token=token))

    rejections = [entry for entry in captured if entry["event"] == "media_token_rejected"]
    assert [(entry["reason"], entry["post_id"]) for entry in rejections] == [
        ("token_bad_signature", POST_ID)
    ]
    assert token not in json.dumps(captured, default=str)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.webm", "audio/webm"),
        ("a.WEBM", "audio/webm"),
        ("a.m4a", "audio/mp4"),
        ("a.opus", "audio/ogg"),
        ("a.flac", None),
        ("a", None),
    ],
)
def test_audio_content_type_reads_the_extension(name: str, expected: str | None) -> None:
    assert audio_content_type(Path(name)) == expected


def test_locate_audio_file_finds_the_file_of_the_ids(media_root: Path) -> None:
    written = write_audio(media_root)

    assert locate_audio_file(media_root, TRACK_ID, VIDEO_ID) == written.resolve()


def test_locate_audio_file_is_none_without_a_file(media_root: Path) -> None:
    assert locate_audio_file(media_root, TRACK_ID, VIDEO_ID) is None


def test_locate_audio_file_refuses_a_file_that_resolves_outside_the_audio_directory(
    media_root: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.webm"
    outside.write_bytes(CONTENT)
    monkeypatch.setattr(audio_module, "find_existing_audio", lambda *arguments: outside)

    assert locate_audio_file(media_root, TRACK_ID, VIDEO_ID) is None
