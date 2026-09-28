"""The media tasks' own decisions: which statuses may enter them, how the
fragment bounds are chosen against the file that actually exists, how a failure
is worded, and who may start the chain.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert

from pisniar.db.models import AudioAsset, Post, Track
from pisniar.db.states import ALLOWED_TRANSITIONS, PostStatus
from pisniar.worker import download as audio_download
from pisniar.worker import ffmpeg as media_tools
from pisniar.worker import tasks as worker_tasks

SOURCE_ROOT = Path(worker_tasks.__file__).resolve().parents[1]


def statuses_with_an_edge_into(target: PostStatus) -> frozenset[PostStatus]:
    return frozenset(status for status, allowed in ALLOWED_TRANSITIONS.items() if target in allowed)


def test_the_download_entry_statuses_are_exactly_the_legal_ones() -> None:
    assert statuses_with_an_edge_into(PostStatus.DOWNLOADING) == (
        worker_tasks.DOWNLOAD_ENTRY_STATUSES
    )


def test_the_render_entry_statuses_are_exactly_the_legal_ones() -> None:
    assert statuses_with_an_edge_into(PostStatus.RENDERING) == worker_tasks.RENDER_ENTRY_STATUSES


def make_post(start_ms: int | None = None, end_ms: int | None = None) -> Post:
    post = Post()
    post.fragment_start_ms = start_ms
    post.fragment_end_ms = end_ms
    return post


def test_a_post_without_bounds_gets_the_rule() -> None:
    bounds = worker_tasks._fragment_bounds_for(make_post(), 212_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)


def test_bounds_already_on_the_post_are_kept() -> None:
    """Bounds on the post are the admin's choice; silently overwriting them would
    undo it on the most expensive artifact in the project."""
    bounds = worker_tasks._fragment_bounds_for(make_post(60_000, 105_000), 212_000)

    assert (bounds.start_ms, bounds.end_ms) == (60_000, 105_000)


def test_stored_bounds_that_do_not_fit_the_downloaded_file_are_recomputed() -> None:
    bounds = worker_tasks._fragment_bounds_for(make_post(60_000, 105_000), 31_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 31_000)


def test_a_half_written_pair_is_replaced_rather_than_crashed_on() -> None:
    bounds = worker_tasks._fragment_bounds_for(make_post(0, None), 212_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)


def test_the_error_message_carries_the_class_that_diagnosis_needs() -> None:
    """ "екстрактор зламався" and "цього треку немає" reach the admin as different
    sentences, not as the same generic failure."""
    message = worker_tasks._describe_media_error(
        audio_download.YtdlpExtractorBrokenError("екстрактор")
    )

    assert message.startswith("YtdlpExtractorBrokenError:")


def compile_statement(statement: PostgresInsert) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def download_claim() -> worker_tasks.DownloadClaim:
    return worker_tasks.DownloadClaim(
        post_id=7, track_id=3, platform_links={}, track_duration_ms=212_000
    )


def download_outcome(ytdlp_version: str | None) -> worker_tasks.DownloadOutcome:
    return worker_tasks.DownloadOutcome(
        video_id="n4bPzdIs_nc",
        path=Path("/data/audio/3/n4bPzdIs_nc.opus"),
        size_bytes=3_400_000,
        duration_ms=212_000,
        ytdlp_version=ytdlp_version,
    )


def test_a_fresh_download_records_the_version_that_produced_it() -> None:
    statement = worker_tasks._upsert_audio_asset_statement(
        download_claim(), download_outcome("2025.12.8")
    )

    assert "ytdlp_version" in compile_statement(statement).split("DO UPDATE SET")[1]


def test_a_reused_artifact_keeps_the_version_already_on_the_row() -> None:
    statement = worker_tasks._upsert_audio_asset_statement(download_claim(), download_outcome(None))
    update_clause = compile_statement(statement).split("DO UPDATE SET")[1]

    assert "ytdlp_version" not in update_clause
    assert "updated_at" in update_clause


def test_the_upsert_refreshes_updated_at_itself() -> None:
    """A Core statement never fires the ORM `onupdate` (AGENTS.md), and a row
    whose `updated_at` went stale cannot be fixed after the fact."""
    statement = worker_tasks._upsert_render_statement(
        worker_tasks.RenderClaim(
            post_id=7,
            track_id=3,
            video_id="n4bPzdIs_nc",
            audio_path="/data/audio/3/n4bPzdIs_nc.opus",
            audio_duration_ms=212_000,
            cover_url="https://cdn/cover.jpg",
            fragment_start_ms=0,
            fragment_end_ms=45_000,
        ),
        worker_tasks.RenderOutcome(
            params_hash="a" * 64,
            path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
            size_bytes=2_260_000,
            duration_ms=45_000,
        ),
    )

    assert "updated_at" in compile_statement(statement).split("DO UPDATE SET")[1]


def test_the_media_tasks_carry_their_own_time_limits() -> None:
    """The resolve limits gate `/retry`'s "resolve has gone stale" window, so
    raising them for a render would silently widen it."""
    download_task = worker_tasks.celery_app.tasks[worker_tasks.DOWNLOAD_POST_AUDIO_TASK_NAME]
    render_task = worker_tasks.celery_app.tasks[worker_tasks.RENDER_POST_VIDEO_TASK_NAME]

    assert (
        download_task.soft_time_limit == worker_tasks.settings.download_task_soft_time_limit_seconds
    )
    assert download_task.time_limit == worker_tasks.settings.download_task_time_limit_seconds
    assert render_task.soft_time_limit == worker_tasks.settings.render_task_soft_time_limit_seconds
    assert render_task.time_limit == worker_tasks.settings.render_task_time_limit_seconds


def test_the_resolve_time_limits_were_not_touched() -> None:
    """`bot.handlers` reads these two numbers as the resolve's staleness window."""
    from pisniar.worker import celery_app as celery_module

    assert celery_module.TASK_SOFT_TIME_LIMIT_SECONDS == 300
    assert celery_module.TASK_TIME_LIMIT_SECONDS == 360


# The media tasks are reachable from Telegram; these two bound who may reach them.
@pytest.mark.parametrize(
    "constant_name", ["DOWNLOAD_POST_AUDIO_TASK_NAME", "RENDER_POST_VIDEO_TASK_NAME"]
)
def test_the_media_tasks_are_dispatched_by_name_and_never_imported(constant_name: str) -> None:
    """AGENTS.md's boundary: `bot/` reaches the worker through the broker, so
    the two processes stay decoupled and the bot image needs no ffmpeg."""
    for path in (SOURCE_ROOT / "bot").rglob("*.py"):
        imports = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "worker.tasks" in line], (
            f"{path} imports the task module"
        )

    handlers_source = (SOURCE_ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")
    assert constant_name in handlers_source


@pytest.mark.parametrize(
    "task_name",
    [worker_tasks.DOWNLOAD_POST_AUDIO_TASK_NAME, worker_tasks.RENDER_POST_VIDEO_TASK_NAME],
)
def test_nothing_under_telegram_can_start_the_media_chain(task_name: str) -> None:
    """`telegram/` is the delivery layer and is imported by the worker itself:
    a dispatch there would let a preview send enqueue another render."""
    for path in (SOURCE_ROOT / "telegram").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert task_name not in source, f"{path} can dispatch {task_name}"
        assert "send_task" not in source


def test_the_chain_is_started_by_the_render_button_and_by_retry_only() -> None:
    """Disk and minutes are spent on approval, never automatically; a third door
    besides the button and `/retry` would render posts nobody asked for."""
    handlers_source = (SOURCE_ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")

    dispatch_sites = handlers_source.count("_dispatch_media_task(")
    # one definition, one call in the render tap, two in the retry router
    assert dispatch_sites == 4


def audio_probe(duration_ms: int = 212_000) -> media_tools.MediaProbe:
    return media_tools.MediaProbe(
        path=Path("/data/audio/3/n4bPzdIs_nc.opus"),
        size_bytes=3_400_000,
        duration_ms=duration_ms,
        streams=(media_tools.StreamInfo("audio", "opus", None, None, None, None),),
    )


def test_a_stored_audio_file_is_reused_instead_of_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two posts of one track must cost one download. The guard is the
    filesystem, because the lock is gone by the time yt-dlp would run."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    directory = tmp_path / "audio" / "3"
    directory.mkdir(parents=True)
    (directory / "n4bPzdIs_nc.opus").write_bytes(b"already downloaded")

    monkeypatch.setattr(
        worker_tasks.audio_download, "verify_media", lambda path, **k: audio_probe()
    )

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("yt-dlp must not run when the artifact is already on the volume")

    monkeypatch.setattr(worker_tasks.audio_download, "download_audio", fail_if_called)

    outcome = worker_tasks._download_audio_off_transaction(
        worker_tasks.DownloadClaim(
            post_id=7,
            track_id=3,
            platform_links={
                "youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}
            },
            track_duration_ms=212_000,
        ),
        tmp_path / "tmp" / "7",
    )

    assert outcome.path == directory / "n4bPzdIs_nc.opus"
    assert outcome.ytdlp_version is None


def render_claim() -> worker_tasks.RenderClaim:
    return worker_tasks.RenderClaim(
        post_id=7,
        track_id=3,
        video_id="n4bPzdIs_nc",
        audio_path="/data/audio/3/n4bPzdIs_nc.opus",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
    )


async def test_a_valid_render_on_the_volume_means_ffmpeg_is_never_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two parallel render tasks produce one file and one ffmpeg."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    claim = render_claim()
    params_hash = worker_tasks.video_render.compute_params_hash(
        video_id=claim.video_id,
        audio_duration_ms=claim.audio_duration_ms,
        cover_url=claim.cover_url or "",
        fragment_start_ms=claim.fragment_start_ms,
        fragment_end_ms=claim.fragment_end_ms,
    )
    final_path = worker_tasks.media_storage.render_file_path(tmp_path, claim.post_id, params_hash)
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"already rendered")

    probe = media_tools.MediaProbe(
        path=final_path,
        size_bytes=final_path.stat().st_size,
        duration_ms=45_000,
        streams=(
            media_tools.StreamInfo("video", "h264", 720, 1280, "yuv420p", "tv"),
            media_tools.StreamInfo("audio", "aac", None, None, None, None),
        ),
    )
    monkeypatch.setattr(worker_tasks.media_tools, "verify_media", lambda path, **k: probe)

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("ffmpeg must not run when a valid artifact already exists")

    monkeypatch.setattr(
        worker_tasks.video_render, "render_fragment_within_size_limit", fail_if_called
    )
    monkeypatch.setattr(worker_tasks.video_render, "download_cover", fail_if_called)

    outcome = await worker_tasks._render_video_off_transaction(claim, tmp_path / "tmp" / "7")

    assert outcome.path == final_path
    assert outcome.params_hash == params_hash


class FakeResult:
    """Enough of `Result` for the one `select(AudioAsset)` the render claim runs."""

    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class FakeSession:
    """Records its own lifetime in `registry`, so a test can ask "was any
    session open while ffmpeg ran?" instead of reading the code and hoping."""

    def __init__(self, registry: list[object], post: Post, track: Track, asset: object) -> None:
        self.registry = registry
        self.post = post
        self.track = track
        self.asset = asset
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        self.registry.append(self)
        return self

    async def __aexit__(self, *exception: object) -> bool:
        self.registry.remove(self)
        return False

    async def get(self, model: object, primary_key: object, **kwargs: object) -> object:
        return self.post if model is Post else self.track

    async def execute(self, statement: object) -> FakeResult:
        return FakeResult(self.asset)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class FakeEngine:
    async def dispose(self) -> None:
        return None


def media_fixtures(status: PostStatus) -> tuple[Post, Track]:
    track = Track()
    track.id = 3
    track.platform_links = {
        "youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}
    }
    track.duration_ms = 212_000
    track.cover_url = "https://cdn/cover.jpg"

    post = Post()
    post.id = 7
    post.track_id = 3
    post.status = status
    post.fragment_start_ms = 0
    post.fragment_end_ms = 45_000
    return post, track


def install_fake_database(
    monkeypatch: pytest.MonkeyPatch, post: Post, track: Track, asset: object
) -> list[object]:
    open_sessions: list[object] = []
    monkeypatch.setattr(worker_tasks, "build_engine", lambda *a, **k: FakeEngine())
    monkeypatch.setattr(
        worker_tasks,
        "build_session_factory",
        lambda engine: lambda: FakeSession(open_sessions, post, track, asset),
    )
    return open_sessions


async def test_yt_dlp_runs_with_no_session_and_no_lock_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download takes minutes, and a lock held across it would block the bot;
    asserted, not assumed."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post, track = media_fixtures(PostStatus.RESOLVED)
    open_sessions = install_fake_database(monkeypatch, post, track, None)
    sessions_during_download: list[int] = []

    def spy_download(claim: object, tmp_directory: Path) -> worker_tasks.DownloadOutcome:
        sessions_during_download.append(len(open_sessions))
        return download_outcome("2025.12.8")

    monkeypatch.setattr(worker_tasks, "_download_audio_off_transaction", spy_download)
    monkeypatch.setattr(worker_tasks, "_finish_download", AsyncMock())

    await worker_tasks._download_post_audio(7)

    assert sessions_during_download == [0]
    assert post.status is PostStatus.DOWNLOADING


async def test_ffmpeg_runs_with_no_session_and_no_lock_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same assertion for the render, where the window is longest."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post, track = media_fixtures(PostStatus.DOWNLOADED)
    asset = AudioAsset()
    asset.path = "/data/audio/3/n4bPzdIs_nc.opus"
    asset.duration_ms = 212_000
    open_sessions = install_fake_database(monkeypatch, post, track, asset)
    sessions_during_render: list[int] = []

    async def spy_render(claim: object, tmp_directory: Path) -> worker_tasks.RenderOutcome:
        sessions_during_render.append(len(open_sessions))
        return worker_tasks.RenderOutcome(
            params_hash="b" * 64,
            path=Path("/data/render/7/" + "b" * 64 + ".mp4"),
            size_bytes=2_260_000,
            duration_ms=45_000,
        )

    monkeypatch.setattr(worker_tasks, "_render_video_off_transaction", spy_render)
    monkeypatch.setattr(worker_tasks, "_finish_render", AsyncMock())

    await worker_tasks._render_post_video(7)

    assert sessions_during_render == [0]
    assert post.status is PostStatus.RENDERING


def rendered_post(
    post_id: int = 7,
    *,
    preview_message_id: int | None = 99,
    status: PostStatus = PostStatus.PUBLISHING,
) -> Post:
    post = Post(id=post_id, track_id=3, created_by_user_id=1, caption="caption")
    post.status = status
    post.preview_chat_id = 555 if preview_message_id is not None else None
    post.preview_message_id = preview_message_id
    return post


def session_returning(render_row: object) -> AsyncMock:
    """`session.execute()` is awaited and its result is not — a plain
    AsyncMock would hand back a coroutine where `.scalars()` is expected."""
    result = MagicMock()
    result.scalars.return_value.first.return_value = render_row

    session = AsyncMock()
    session.execute.return_value = result
    return session


def test_a_post_that_never_reached_rendered_publishes_as_before() -> None:
    """The status decides: a post with no preview goes down the photo path,
    whatever is on the volume."""
    assert worker_tasks.post_reached_rendered(rendered_post(preview_message_id=None)) is False


def test_the_preview_reference_is_the_durable_trace_of_rendered() -> None:
    """By the time `publish_post` runs the status is already `publishing` —
    the claim moved it — so `RENDERED` has to be read from what it wrote."""
    assert worker_tasks.post_reached_rendered(rendered_post()) is True


async def test_a_previewed_post_whose_file_vanished_fails_instead_of_sending_a_photo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back to `send_photo` would put a different post in the channel
    than the one the admin approved."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    render_row = SimpleNamespace(
        path=str(tmp_path / "render" / "7" / ("a" * 64 + ".mp4")),
        params_hash="a" * 64,
        duration_ms=45_000,
    )
    session = session_returning(render_row)

    with pytest.raises(worker_tasks.RenderedVideoUnavailableError):
        await worker_tasks._video_for_publication(session, rendered_post())


async def test_a_previewed_post_with_no_render_row_fails_loudly(tmp_path: Path) -> None:
    session = session_returning(None)

    with pytest.raises(worker_tasks.RenderedVideoUnavailableError):
        await worker_tasks._video_for_publication(session, rendered_post())


async def test_a_previewed_post_with_its_file_sends_the_renderers_own_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    directory = tmp_path / "render" / "7"
    directory.mkdir(parents=True)
    video_path = directory / ("a" * 64 + ".mp4")
    video_path.write_bytes(b"video bytes")

    session = session_returning(
        SimpleNamespace(path=str(video_path), params_hash="a" * 64, duration_ms=45_000)
    )

    video = await worker_tasks._video_for_publication(session, rendered_post())

    assert video is not None
    assert (video.width, video.height) == (720, 1280)
    assert video.duration_ms == 45_000
    assert video.thumbnail_path is None


async def test_a_failed_render_dispatch_fails_the_post_instead_of_stranding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DOWNLOADED` is committed and no task will ever move it on, so `/retry` has
    to find a `failed` post with a stage."""
    failures: list[tuple[int, str]] = []

    def refuse_to_send(*args: object, **kwargs: object) -> object:
        raise RuntimeError("broker down")

    async def record_failure(session_factory: object, post_id: int, error: str) -> None:
        failures.append((post_id, error))

    monkeypatch.setattr(worker_tasks.celery_app, "send_task", refuse_to_send)
    monkeypatch.setattr(worker_tasks, "_fail_media_post", record_failure)

    await worker_tasks._chain_render_after_download(AsyncMock(), 7)

    assert failures == [(7, worker_tasks.RENDER_DISPATCH_FAILED_ERROR)]


async def test_a_successful_download_chains_the_render_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one internal hop: nothing in `bot/` knows the chain has two stages."""
    dispatched: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        worker_tasks.celery_app,
        "send_task",
        lambda name, kwargs: dispatched.append((name, kwargs)),
    )

    await worker_tasks._chain_render_after_download(AsyncMock(), 7)

    assert dispatched == [(worker_tasks.RENDER_POST_VIDEO_TASK_NAME, {"post_id": 7})]


async def test_a_timeout_during_the_preview_send_is_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SoftTimeLimitExceeded` is an ordinary `Exception`, so a blanket handler
    around the preview send would report «помилка» where the truth is «таймаут»."""
    reported: list[str] = []

    async def time_out(*args: object, **kwargs: object) -> None:
        raise SoftTimeLimitExceeded

    async def record(session_factory: object, post_id: int, error: str) -> None:
        reported.append(error)

    monkeypatch.setattr(worker_tasks, "_fail_media_post", record)

    claim = worker_tasks.RenderClaim(
        post_id=7,
        track_id=3,
        video_id="n4bPzdIs_nc",
        audio_path="/data/audio/3/n4bPzdIs_nc.opus",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
    )
    outcome = worker_tasks.RenderOutcome(
        params_hash="a" * 64,
        path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
        size_bytes=2_260_000,
        duration_ms=45_000,
    )
    monkeypatch.setattr(worker_tasks, "_finish_render", time_out)

    await worker_tasks._finish_render_or_fail(AsyncMock(), claim, outcome)

    assert reported == [worker_tasks.MEDIA_TASK_TIMEOUT_ERROR]


async def test_a_failed_preview_send_fails_the_post_from_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RENDERED` has no edge to `FAILED`, so the only state a delivery failure
    can be reported from is `RENDERING`."""
    reported: list[str] = []

    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("telegram refused the upload")

    async def record(session_factory: object, post_id: int, error: str) -> None:
        reported.append(error)

    monkeypatch.setattr(worker_tasks, "_finish_render", refuse)
    monkeypatch.setattr(worker_tasks, "_fail_media_post", record)

    claim = worker_tasks.RenderClaim(
        post_id=7,
        track_id=3,
        video_id="n4bPzdIs_nc",
        audio_path="/data/audio/3/n4bPzdIs_nc.opus",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
    )
    outcome = worker_tasks.RenderOutcome(
        params_hash="a" * 64,
        path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
        size_bytes=2_260_000,
        duration_ms=45_000,
    )

    await worker_tasks._finish_render_or_fail(AsyncMock(), claim, outcome)

    assert reported == ["RuntimeError: telegram refused the upload"]
