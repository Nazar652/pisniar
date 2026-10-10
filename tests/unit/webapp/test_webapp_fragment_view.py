"""The fragment view: the bounds, the waveform read from the file the worker wrote, the signed
URL, and the refusals; plus the route over `TestClient` with the database faked.
"""

from __future__ import annotations

import subprocess
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from support import ADMIN_ID, BOT_TOKEN, NOW

from pisniar.config import Settings
from pisniar.webapp import fragment_view as fragment_view_module
from pisniar.webapp import posts as posts_module
from pisniar.webapp.app import create_webapp
from pisniar.webapp.auth import (
    AuthWindow,
    WebappContext,
    issue_session_token,
    verify_media_token,
)
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.fragment_view import (
    AUDIO_MISSING_MESSAGE,
    FragmentSource,
    build_fragment_view,
    current_video_id,
    displayed_bounds,
    peaks_of,
)
from pisniar.webapp.schemas import FragmentPeaks, FragmentView
from pisniar.worker import peaks as worker_peaks
from pisniar.worker import storage as media_storage
from pisniar.worker.fragment import MAX_FRAGMENT_LENGTH_MS
from pisniar.worker.peaks_format import peaks_document

VIDEO_ID = "n4bPzdIs_nc"
STAMP = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
MUSIC_LINK = {"url": f"https://music.youtube.com/watch?v={VIDEO_ID}", "source": "ytmusic"}
CLIP_LINK = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "source": "user_input"}


def make_context(media_root: Path) -> WebappContext:
    return WebappContext(
        bot_token=BOT_TOKEN,
        admin_ids=frozenset({ADMIN_ID}),
        auth_window=AuthWindow(max_age_seconds=600, max_future_seconds=60),
        session_ttl_seconds=3600,
        media_url_ttl_seconds=900,
        media_root=media_root,
        clock=lambda: float(NOW),
    )


def make_source(**changes: Any) -> FragmentSource:
    fields: dict[str, Any] = {
        "post_id": 7,
        "post_updated_at": STAMP,
        "track_id": 3,
        "video_id": VIDEO_ID,
        "audio_duration_ms": 180_000,
        "stored_start_ms": 10_000,
        "stored_end_ms": 55_000,
    }
    return FragmentSource(**{**fields, **changes})


def write_audio(media_root: Path, source: FragmentSource) -> Path:
    path = media_storage.audio_file_path(media_root, source.track_id, source.video_id, "webm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


def test_the_video_id_comes_from_the_youtube_music_link_only() -> None:
    assert current_video_id({"youtubeMusic": MUSIC_LINK, "youtube": CLIP_LINK}) == VIDEO_ID
    assert current_video_id({"youtube": CLIP_LINK}) is None
    assert current_video_id({"youtubeMusic": {"url": "https://music.youtube.com/playlist"}}) is None
    assert current_video_id({}) is None


def test_stored_bounds_that_fit_the_audio_are_shown() -> None:
    bounds = displayed_bounds(make_source())

    assert (bounds.start_ms, bounds.end_ms) == (10_000, 55_000)


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(None, None), (150_000, 200_000), (55_000, 10_000), (-5, 1000)],
)
def test_bounds_that_are_missing_invalid_or_beyond_the_audio_fall_back_to_the_default(
    start_ms: int | None, end_ms: int | None
) -> None:
    bounds = displayed_bounds(make_source(stored_start_ms=start_ms, stored_end_ms=end_ms))

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)


def test_the_default_of_a_short_track_is_the_whole_track() -> None:
    bounds = displayed_bounds(
        make_source(audio_duration_ms=20_000, stored_start_ms=None, stored_end_ms=None)
    )

    assert (bounds.start_ms, bounds.end_ms) == (0, 20_000)


@pytest.fixture
def view_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """a stand-in: a cached structlog proxy defeats `capture_logs()` after other tests ran."""
    fake_logger = MagicMock()
    monkeypatch.setattr(fragment_view_module, "logger", fake_logger)
    return fake_logger


def test_a_missing_peaks_file_is_none_with_a_warning(
    tmp_path: Path, view_logger: MagicMock
) -> None:
    result = peaks_of(make_context(tmp_path), make_source())

    assert result is None
    assert view_logger.warning.call_args.args[0] == "fragment_peaks_unavailable"


def test_a_corrupt_peaks_file_is_none(tmp_path: Path) -> None:
    source = make_source()
    path = media_storage.peaks_file_path(tmp_path, source.track_id, source.video_id)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert peaks_of(make_context(tmp_path), source) is None


def test_a_file_written_by_the_worker_is_read_by_the_webapp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = array("h", [1000] * 400 + [500] * 400 + [250] * 400).tobytes()
    monkeypatch.setattr(
        worker_peaks,
        "run_media_tool_bytes",
        lambda binary, arguments, *, timeout_seconds: subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout=pcm, stderr=b""
        ),
    )
    source = make_source()

    written = worker_peaks.ensure_peaks(
        tmp_path, source.track_id, source.video_id, tmp_path / "a.webm", tmp_path / "tmp"
    )

    assert written is not None
    assert peaks_of(make_context(tmp_path), source) == FragmentPeaks(
        peaks_per_second=20, values=worker_peaks.loudness_values(pcm)
    )


def test_the_view_carries_the_bounds_the_peaks_and_a_relative_signed_url(tmp_path: Path) -> None:
    source = make_source()
    write_audio(tmp_path, source)
    peaks_path = media_storage.peaks_file_path(tmp_path, source.track_id, source.video_id)
    peaks_path.parent.mkdir(parents=True)
    peaks_path.write_text(peaks_document([0, 10, 255]), encoding="utf-8")

    view = build_fragment_view(make_context(tmp_path), source, ADMIN_ID)

    assert view.post_updated_at == STAMP
    assert (view.start_ms, view.end_ms) == (10_000, 55_000)
    assert view.audio_duration_ms == 180_000
    assert view.max_length_ms == MAX_FRAGMENT_LENGTH_MS == 120_000
    assert view.peaks == FragmentPeaks(peaks_per_second=20, values=[0, 10, 255])
    assert view.audio_url_expires_at == datetime.fromtimestamp(NOW + 900, UTC)

    prefix = "/api/posts/7/audio?token="
    assert view.audio_url.startswith(prefix)
    claims = verify_media_token(view.audio_url[len(prefix) :], BOT_TOKEN, NOW)
    assert (claims.user_id, claims.post_id, claims.track_id, claims.video_id) == (
        ADMIN_ID,
        7,
        3,
        VIDEO_ID,
    )


def test_the_view_without_a_peaks_file_has_none_and_still_a_url(tmp_path: Path) -> None:
    source = make_source()
    write_audio(tmp_path, source)

    view = build_fragment_view(make_context(tmp_path), source, ADMIN_ID)

    assert view.peaks is None
    assert view.audio_url.startswith("/api/posts/7/audio?token=")


def test_a_view_without_the_audio_file_is_409_audio_missing(tmp_path: Path) -> None:
    with pytest.raises(ApiProblem) as refusal:
        build_fragment_view(make_context(tmp_path), make_source(), ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "audio_missing")
    assert refusal.value.message == AUDIO_MISSING_MESSAGE


def test_a_view_never_logs_its_token(tmp_path: Path, view_logger: MagicMock) -> None:
    source = make_source()
    write_audio(tmp_path, source)

    view = build_fragment_view(make_context(tmp_path), source, ADMIN_ID)

    token = view.audio_url.split("token=")[1]
    logged = [str(call) for call in view_logger.mock_calls]
    assert not any(token in line for line in logged)
    assert view_logger.info.call_args.args[0] == "fragment_view_read"


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None


def route_client(settings: Settings, media_root: Path) -> tuple[TestClient, MagicMock]:
    session_factory = MagicMock(return_value=FakeSession())
    configured = settings.model_copy(update={"media_root": media_root})
    app = create_webapp(configured, clock=lambda: float(NOW), session_factory=session_factory)
    return TestClient(app), session_factory


def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_session_token(ADMIN_ID, BOT_TOKEN, NOW, 3600)}"}


def test_the_route_answers_a_view_with_the_pinned_shape(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source()
    write_audio(tmp_path, source)
    fetch = AsyncMock(return_value=source)
    monkeypatch.setattr(posts_module, "fetch_fragment_source", fetch)
    client, _ = route_client(settings, tmp_path)

    response = client.get("/api/posts/7/fragment", headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert FragmentView.model_validate(body).start_ms == 10_000
    assert set(body) == {
        "post_updated_at",
        "start_ms",
        "end_ms",
        "audio_duration_ms",
        "max_length_ms",
        "peaks",
        "audio_url",
        "audio_url_expires_at",
    }
    assert body["peaks"] is None
    fetch.assert_awaited_once_with(ANY, 7, ADMIN_ID)


def test_the_url_of_the_view_plays_the_file_through_the_audio_route(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source()
    write_audio(tmp_path, source)
    fetch = AsyncMock(return_value=source)
    monkeypatch.setattr(posts_module, "fetch_fragment_source", fetch)
    client, _ = route_client(settings, tmp_path)

    url = client.get("/api/posts/7/fragment", headers=bearer()).json()["audio_url"]
    audio = client.get(url, headers={"Range": "bytes=0-2"})

    assert audio.status_code == 206
    assert audio.content == b"aud"


def test_a_missing_post_is_404(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(posts_module, "fetch_fragment_source", fetch)
    client, _ = route_client(settings, tmp_path)

    response = client.get("/api/posts/7/fragment", headers=bearer())

    assert response.status_code == 404
    assert response.json()["code"] == "post_not_found"


def test_a_refusal_from_the_reader_is_passed_through(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = AsyncMock(side_effect=ApiProblem(409, "not_choosable", "Пост опубліковано."))
    monkeypatch.setattr(posts_module, "fetch_fragment_source", fetch)
    client, _ = route_client(settings, tmp_path)

    response = client.get("/api/posts/7/fragment", headers=bearer())

    assert response.status_code == 409
    assert response.json() == {"code": "not_choosable", "message": "Пост опубліковано."}


def test_the_route_refuses_an_anonymous_caller_before_the_database(
    settings: Settings, tmp_path: Path
) -> None:
    client, session_factory = route_client(settings, tmp_path)

    assert client.get("/api/posts/7/fragment").status_code == 401
    session_factory.assert_not_called()
