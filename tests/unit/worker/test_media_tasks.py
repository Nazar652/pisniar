"""The media tasks' own decisions: which statuses may enter them, how the fragment bounds are chosen against the
file that actually exists, how a failure is worded, and who may start the chain.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
from pisniar.worker.ass import (
    AssGenerationError,
    LyricLine,
    build_ass_subtitles,
    build_display_subtitles,
)
from pisniar.worker.celery_app import PREPARE_POST_AUDIO_TASK_NAME
from pisniar.worker.lyrics import (
    LYRICS_ALREADY_STORED,
    LYRICS_OUTSIDE_FRAGMENT,
    LyricsQuery,
    PreparedLyrics,
)
from pisniar.worker.render import text_version_of

LYRICS_QUERY = LyricsQuery(
    track_id=3,
    artist_names=("Burned Time Machine",),
    title="Покаяння",
    duration_ms=212_000,
    video_id="n4bPzdIs_nc",
)

SOURCE_ROOT = Path(worker_tasks.__file__).resolve().parents[1]


def statuses_with_an_edge_into(target: PostStatus) -> frozenset[PostStatus]:
    return frozenset(status for status, allowed in ALLOWED_TRANSITIONS.items() if target in allowed)


def test_the_download_entry_is_downloading_alone() -> None:
    """every producer writes `downloading` first, so a task that finds `resolved` (a cancelled preparation) or
    `failed` is a late redelivery."""
    entries = worker_tasks.DOWNLOAD_ENTRY_STATUSES
    legal_sources = statuses_with_an_edge_into(PostStatus.DOWNLOADING)
    assert entries == frozenset({PostStatus.DOWNLOADING})
    assert entries.issubset(legal_sources)


def test_the_render_entry_statuses_are_the_legal_ones_except_failed_rendered_and_editing() -> None:
    """a `rendered` or `editing` post enters through the explicit choice that writes `rendering` first, so a render
    task that finds either belongs to someone else."""
    assert statuses_with_an_edge_into(PostStatus.RENDERING) - {
        PostStatus.FAILED,
        PostStatus.RENDERED,
        PostStatus.EDITING,
    } == (worker_tasks.RENDER_ENTRY_STATUSES)
    assert PostStatus.EDITING not in worker_tasks.RENDER_ENTRY_STATUSES


def make_post(start_ms: int | None = None, end_ms: int | None = None) -> Post:
    post = Post()
    post.fragment_start_ms = start_ms
    post.fragment_end_ms = end_ms
    return post


def test_a_post_without_bounds_gets_the_rule() -> None:
    bounds = worker_tasks.fragment_bounds_for(make_post(), 212_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)


def test_bounds_already_on_the_post_are_kept() -> None:
    """bounds on the post are the admin's choice; silently overwriting them would
    undo it on the most expensive artifact in the project."""
    bounds = worker_tasks.fragment_bounds_for(make_post(60_000, 105_000), 212_000)

    assert (bounds.start_ms, bounds.end_ms) == (60_000, 105_000)


def test_stored_bounds_that_do_not_fit_the_downloaded_file_are_recomputed() -> None:
    bounds = worker_tasks.fragment_bounds_for(make_post(60_000, 105_000), 31_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 31_000)


def test_a_half_written_pair_is_replaced_rather_than_crashed_on() -> None:
    bounds = worker_tasks.fragment_bounds_for(make_post(0, None), 212_000)

    assert (bounds.start_ms, bounds.end_ms) == (0, 45_000)


def test_the_error_message_carries_the_class_that_diagnosis_needs() -> None:
    """ "екстрактор зламався" and "цього треку немає" reach the admin as different
    sentences, not as the same generic failure."""
    message = worker_tasks.describe_media_error(audio_download.YtdlpExtractorBrokenError("екстрактор"))

    assert message.startswith("YtdlpExtractorBrokenError:")


def compile_statement(statement: PostgresInsert) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def download_claim() -> worker_tasks.DownloadClaim:
    return worker_tasks.DownloadClaim(post_id=7, track_id=3, platform_links={}, track_duration_ms=212_000)


def download_outcome(ytdlp_version: str | None) -> worker_tasks.DownloadOutcome:
    return worker_tasks.DownloadOutcome(
        video_id="n4bPzdIs_nc",
        path=Path("/data/audio/3/n4bPzdIs_nc.opus"),
        size_bytes=3_400_000,
        duration_ms=212_000,
        ytdlp_version=ytdlp_version,
    )


def test_a_fresh_download_records_the_version_that_produced_it() -> None:
    statement = worker_tasks.upsert_audio_asset_statement(download_claim(), download_outcome("2025.12.8"))

    assert "ytdlp_version" in compile_statement(statement).split("DO UPDATE SET")[1]


def test_a_reused_artifact_keeps_the_version_already_on_the_row() -> None:
    statement = worker_tasks.upsert_audio_asset_statement(download_claim(), download_outcome(None))
    update_clause = compile_statement(statement).split("DO UPDATE SET")[1]

    assert "ytdlp_version" not in update_clause
    assert "updated_at" in update_clause


def test_the_upsert_refreshes_updated_at_itself() -> None:
    """a Core statement never fires the ORM `onupdate` (AGENTS.md), and a row
    whose `updated_at` went stale cannot be fixed after the fact."""
    statement = worker_tasks.upsert_render_statement(
        worker_tasks.RenderClaim(
            post_id=7,
            track_id=3,
            video_id="n4bPzdIs_nc",
            audio_path="/data/audio/3/n4bPzdIs_nc.opus",
            audio_duration_ms=212_000,
            cover_url="https://cdn/cover.jpg",
            fragment_start_ms=0,
            fragment_end_ms=45_000,
            lyrics_query=LYRICS_QUERY,
        ),
        worker_tasks.RenderOutcome(
            params_hash="a" * 64,
            path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
            size_bytes=2_260_000,
            duration_ms=45_000,
            text_version=None,
            lyrics_reason="lyrics_absent",
        ),
    )

    assert "updated_at" in compile_statement(statement).split("DO UPDATE SET")[1]


def test_the_media_tasks_carry_their_own_time_limits() -> None:
    """the resolve limits gate `/retry`'s "resolve has gone stale" window, so raising them for a render would
    silently widen it."""
    download_task = worker_tasks.celery_app.tasks[worker_tasks.DOWNLOAD_POST_AUDIO_TASK_NAME]
    render_task = worker_tasks.celery_app.tasks[worker_tasks.RENDER_POST_VIDEO_TASK_NAME]

    assert download_task.soft_time_limit == worker_tasks.settings.download_task_soft_time_limit_seconds
    assert download_task.time_limit == worker_tasks.settings.download_task_time_limit_seconds
    assert render_task.soft_time_limit == worker_tasks.settings.render_task_soft_time_limit_seconds
    assert render_task.time_limit == worker_tasks.settings.render_task_time_limit_seconds


def test_the_resolve_time_limits_were_not_touched() -> None:
    """`bot.handlers` reads these two numbers as the resolve's staleness window."""
    from pisniar.worker import celery_app as celery_module

    assert celery_module.TASK_SOFT_TIME_LIMIT_SECONDS == 300
    assert celery_module.TASK_TIME_LIMIT_SECONDS == 360


# the media tasks are reachable from Telegram; these two bound who may reach them.
@pytest.mark.parametrize("constant_name", ["DOWNLOAD_POST_AUDIO_TASK_NAME", "RENDER_POST_VIDEO_TASK_NAME"])
def test_the_media_tasks_are_dispatched_by_name_and_never_imported(constant_name: str) -> None:
    """AGENTS.md's boundary: `bot/` reaches the worker through the broker, so the two processes stay decoupled
    and the bot image needs no ffmpeg."""
    for path in (SOURCE_ROOT / "bot").rglob("*.py"):
        imports = [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "worker.tasks" in line], f"{path} imports the task module"

    handlers_source = (SOURCE_ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")
    assert constant_name in handlers_source


@pytest.mark.parametrize(
    "task_name",
    [worker_tasks.DOWNLOAD_POST_AUDIO_TASK_NAME, worker_tasks.RENDER_POST_VIDEO_TASK_NAME],
)
def test_nothing_under_telegram_can_start_the_media_chain(task_name: str) -> None:
    """`telegram/` is the delivery layer and is imported by the worker itself: a dispatch there would let a preview
    send enqueue another render."""
    for path in (SOURCE_ROOT / "telegram").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert task_name not in source, f"{path} can dispatch {task_name}"
        assert "send_task" not in source


def test_the_chain_is_started_by_the_render_button_and_by_retry_only() -> None:
    """disk and minutes are spent on approval, never automatically; a third door besides the button and
    `/retry` would render posts nobody asked for."""
    handlers_source = (SOURCE_ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")

    dispatch_sites = handlers_source.count("dispatch_media_task(")
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
    """two posts of one track must cost one download. The guard is the filesystem, because the lock is
    gone by the time yt-dlp would run."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    directory = tmp_path / "audio" / "3"
    directory.mkdir(parents=True)
    (directory / "n4bPzdIs_nc.opus").write_bytes(b"already downloaded")

    monkeypatch.setattr(worker_tasks.audio_download, "verify_media", lambda path, **k: audio_probe())

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("yt-dlp must not run when the artifact is already on the volume")

    monkeypatch.setattr(worker_tasks.audio_download, "download_audio", fail_if_called)

    outcome = worker_tasks.download_audio_off_transaction(
        worker_tasks.DownloadClaim(
            post_id=7,
            track_id=3,
            platform_links={"youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}},
            track_duration_ms=212_000,
        ),
        tmp_path / "tmp" / "7",
    )

    assert outcome.path == directory / "n4bPzdIs_nc.opus"
    assert outcome.ytdlp_version is None


DOWNLOAD_CLAIM = worker_tasks.DownloadClaim(
    post_id=7,
    track_id=3,
    platform_links={"youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}},
    track_duration_ms=212_000,
)


def record_peaks_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def spy(*arguments: object) -> None:
        calls.append(arguments)

    monkeypatch.setattr(worker_tasks.audio_peaks, "ensure_peaks", spy)
    return calls


def test_peaks_are_written_for_a_reused_audio_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    directory = tmp_path / "audio" / "3"
    directory.mkdir(parents=True)
    (directory / "n4bPzdIs_nc.opus").write_bytes(b"already downloaded")
    monkeypatch.setattr(worker_tasks.audio_download, "verify_media", lambda path, **k: audio_probe())
    calls = record_peaks_calls(monkeypatch)

    worker_tasks.download_audio_off_transaction(DOWNLOAD_CLAIM, tmp_path / "tmp" / "7")

    assert calls == [(tmp_path, 3, "n4bPzdIs_nc", directory / "n4bPzdIs_nc.opus", tmp_path / "tmp" / "7")]


def test_peaks_are_written_for_a_fresh_download_from_its_published_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    temporary_directory = tmp_path / "tmp" / "7"
    temporary_directory.mkdir(parents=True)
    downloaded = temporary_directory / "n4bPzdIs_nc.opus"
    downloaded.write_bytes(b"fresh audio")
    monkeypatch.setattr(worker_tasks.media_storage, "ensure_free_space", lambda root, size: size)
    monkeypatch.setattr(worker_tasks.audio_download, "download_audio", lambda *a, **k: (downloaded, "2026.1.1"))
    monkeypatch.setattr(worker_tasks.audio_download, "verify_downloaded_audio", lambda path, duration: audio_probe())
    calls = record_peaks_calls(monkeypatch)

    outcome = worker_tasks.download_audio_off_transaction(DOWNLOAD_CLAIM, temporary_directory)

    published = tmp_path / "audio" / "3" / "n4bPzdIs_nc.opus"
    assert outcome.path == published
    assert calls == [(tmp_path, 3, "n4bPzdIs_nc", published, temporary_directory)]


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
        lyrics_query=LYRICS_QUERY,
    )


LYRIC_LINES = (
    LyricLine(line_index=0, start_ms=1_000, text="Перший рядок"),
    LyricLine(line_index=1, start_ms=6_000, text="Другий рядок"),
)


def stub_prepared_lyrics(
    monkeypatch: pytest.MonkeyPatch,
    lines: tuple[LyricLine, ...],
    reason: str,
) -> None:
    async def prepared(session_factory: object, claim: object) -> PreparedLyrics:
        return PreparedLyrics(lines=lines, reason=reason)

    monkeypatch.setattr(worker_tasks, "prepare_lyrics_off_transaction", prepared)


def expected_subtitles(claim: worker_tasks.RenderClaim) -> str | None:
    return build_ass_subtitles(
        LYRIC_LINES,
        fragment_start_ms=claim.fragment_start_ms,
        fragment_end_ms=claim.fragment_end_ms,
    )


def expected_display(claim: worker_tasks.RenderClaim) -> str:
    return build_display_subtitles(
        claim.lyrics_query.title,
        claim.lyrics_query.artist_names,
        claim.fragment_end_ms - claim.fragment_start_ms,
    )


def params_hash_for(claim: worker_tasks.RenderClaim, text_version: str | None) -> str:
    return worker_tasks.video_render.compute_params_hash(
        video_id=claim.video_id,
        audio_duration_ms=claim.audio_duration_ms,
        cover_url=claim.cover_url or "",
        fragment_start_ms=claim.fragment_start_ms,
        fragment_end_ms=claim.fragment_end_ms,
        display_version=worker_tasks.video_render.text_digest(expected_display(claim)),
        text_version=text_version,
    )


def fail_if_called(*args: object, **kwargs: object) -> object:
    raise AssertionError("ffmpeg and the cover fetch must not run here")


async def test_the_same_text_on_the_volume_means_ffmpeg_is_never_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the hash is derived from the `.ass` itself, so a repeated render of the same lines finds its own file and
    costs nothing."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    claim = render_claim()
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, LYRICS_ALREADY_STORED)

    params_hash = params_hash_for(claim, text_version_of(expected_subtitles(claim)))
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
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", fail_if_called)
    monkeypatch.setattr(worker_tasks.video_render, "download_cover", fail_if_called)

    outcome = await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")

    assert outcome.path == final_path
    assert outcome.params_hash == params_hash
    assert outcome.lyrics_reason == LYRICS_ALREADY_STORED


def install_fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """a stand-in: a cached structlog proxy defeats `capture_logs()`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(worker_tasks, "logger", fake_logger)
    return fake_logger


def logged(fake_logger: MagicMock, event: str) -> list[dict[str, Any]]:
    return [call.kwargs for call in fake_logger.info.call_args_list if call.args == (event,)]


async def test_a_reused_render_logs_the_text_it_was_rendered_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """no ffmpeg on this path, so this line is the only place the log says which text the reused file carries."""
    fake_logger = install_fake_logger(monkeypatch)
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    claim = render_claim()
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, LYRICS_ALREADY_STORED)

    text_version = text_version_of(expected_subtitles(claim))
    params_hash = params_hash_for(claim, text_version)
    final_path = worker_tasks.media_storage.render_file_path(tmp_path, claim.post_id, params_hash)
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"already rendered")

    probe = media_tools.MediaProbe(
        path=final_path,
        size_bytes=final_path.stat().st_size,
        duration_ms=45_000,
        streams=(media_tools.StreamInfo("video", "h264", 720, 1280, "yuv420p", "tv"),),
    )
    monkeypatch.setattr(worker_tasks.media_tools, "verify_media", lambda path, **k: probe)

    await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")

    assert logged(fake_logger, "render_post_video_reused_artifact") == [
        {
            "post_id": 7,
            "track_id": 3,
            "path": str(final_path),
            "params_hash": params_hash,
            "text_version": text_version,
            "lyrics_reason": LYRICS_ALREADY_STORED,
        }
    ]


class RecordingRenderer:
    """stands in for ffmpeg: writes the staged file and keeps what it was given, the subtitle file's content included,
    while that file still exists."""

    def __init__(self) -> None:
        self.parameters: list[worker_tasks.video_render.RenderParameters] = []
        self.subtitles: list[str | None] = []
        self.displays: list[str] = []

    def __call__(
        self,
        parameters: worker_tasks.video_render.RenderParameters,
        output_path: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[media_tools.MediaProbe, object]:
        self.parameters.append(parameters)
        self.displays.append(parameters.display_subtitles_path.read_text(encoding="utf-8"))
        subtitles_path = parameters.subtitles_path
        self.subtitles.append(subtitles_path.read_text(encoding="utf-8") if subtitles_path is not None else None)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"rendered")
        probe = media_tools.MediaProbe(
            path=output_path,
            size_bytes=8,
            duration_ms=45_000,
            streams=(media_tools.StreamInfo("video", "h264", 720, 1280, "yuv420p", "tv"),),
        )
        return probe, worker_tasks.video_render.DEFAULT_QUALITY


def install_renderer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RecordingRenderer:
    renderer = RecordingRenderer()
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    monkeypatch.setattr(worker_tasks.settings, "media_min_free_bytes", 1)
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", renderer)
    monkeypatch.setattr(worker_tasks.video_render, "render_thumbnail", lambda *a, **k: None)

    async def cover(
        http_client: object,
        cover_url: object,
        directory: Path,
    ) -> Path:
        return directory / "cover.img"

    monkeypatch.setattr(worker_tasks.video_render, "download_cover", cover)
    return renderer


async def test_lyrics_reach_ffmpeg_as_the_subtitles_file_and_the_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, "lyrics_synced")
    claim = render_claim()
    subtitles = expected_subtitles(claim)
    assert subtitles is not None

    outcome = await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")

    assert renderer.parameters[0].subtitles_path == (
        worker_tasks.media_storage.subtitles_file_path(tmp_path, claim.post_id)
    )
    assert renderer.subtitles == [subtitles]
    assert outcome.text_version == text_version_of(subtitles)
    assert outcome.text_version is not None
    assert outcome.params_hash == params_hash_for(claim, outcome.text_version)
    assert outcome.params_hash != params_hash_for(claim, None)
    assert outcome.lyrics_reason == "lyrics_synced"


async def test_the_display_file_reaches_ffmpeg_and_the_hash_beside_the_song_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, "lyrics_synced")
    claim = render_claim()

    await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")

    assert renderer.parameters[0].display_subtitles_path == (
        worker_tasks.media_storage.display_subtitles_file_path(tmp_path, claim.post_id)
    )
    assert renderer.displays == [expected_display(claim)]
    assert "Покаяння" in renderer.displays[0]


async def test_the_layers_cover_and_both_subtitle_files_share_the_directory_the_finally_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`render_fragment_within_size_limit` draws the layers beside the cover, so removing the
    post's tmp directory in the task's `finally` removes them with everything else."""
    renderer = install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, "lyrics_synced")
    claim = render_claim()
    temporary = worker_tasks.media_storage.task_temporary_directory(tmp_path, claim.post_id)

    await worker_tasks.render_video_off_transaction(AsyncMock(), claim, temporary)

    [parameters] = renderer.parameters
    assert parameters.cover_path.parent == temporary
    assert parameters.display_subtitles_path.parent == temporary
    assert parameters.subtitles_path is not None
    assert parameters.subtitles_path.parent == temporary


async def test_a_new_title_makes_a_new_hash_and_no_text_still_draws_the_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, (), "lyrics_source_unavailable")
    claim = render_claim()
    renamed = replace(claim, lyrics_query=replace(claim.lyrics_query, title="Інша назва"))

    first = await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")
    second = await worker_tasks.render_video_off_transaction(AsyncMock(), renamed, tmp_path / "tmp" / "7")

    assert first.text_version is None
    assert first.params_hash != second.params_hash
    assert renderer.parameters[0].subtitles_path is None
    assert "Інша назва" in renderer.displays[1]


async def test_a_track_without_a_title_still_gets_a_display_of_its_artists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """a NULL `track.title` reaches the claim as an empty string."""
    renderer = install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, (), "lyrics_source_unavailable")
    claim = render_claim()
    untitled = replace(claim, lyrics_query=replace(claim.lyrics_query, title=""))

    await worker_tasks.render_video_off_transaction(AsyncMock(), untitled, tmp_path / "tmp" / "7")

    rows = [line for line in renderer.displays[0].splitlines() if line.startswith("Dialogue: ")]
    assert len(rows) == 2
    assert all("Burned Time Machine" in row for row in rows)


async def test_a_fresh_render_logs_its_track_and_text_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_logger = install_fake_logger(monkeypatch)
    install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, "lyrics_synced")

    outcome = await worker_tasks.render_video_off_transaction(AsyncMock(), render_claim(), tmp_path / "tmp" / "7")

    [rendered] = logged(fake_logger, "render_post_video_rendered")
    assert (rendered["post_id"], rendered["track_id"]) == (7, 3)
    assert rendered["text_version"] == outcome.text_version
    assert rendered["display_version"] == worker_tasks.video_render.text_digest(expected_display(render_claim()))
    assert rendered["lyrics_reason"] == "lyrics_synced"


async def test_the_render_row_records_the_text_version_it_was_rendered_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, "lyrics_synced")
    claim = render_claim()

    outcome = await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")
    parameters = worker_tasks.upsert_render_statement(claim, outcome).compile().params

    assert outcome.text_version is not None
    assert parameters["text_version"] == outcome.text_version
    assert parameters["params_hash"] == outcome.params_hash


async def test_no_lyrics_means_no_subtitles_step_and_no_text_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """a source that did not answer still yields a video, and the reason travels
    with it to the preview."""
    renderer = install_renderer(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, (), "lyrics_source_unavailable")
    claim = render_claim()

    outcome = await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "tmp" / "7")

    assert renderer.parameters[0].subtitles_path is None
    assert outcome.text_version is None
    assert outcome.params_hash == params_hash_for(claim, None)
    assert outcome.lyrics_reason == "lyrics_source_unavailable"


async def test_a_subtitle_generation_failure_fails_the_render_instead_of_dropping_the_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the generator is our code: a defect there must be heard, not turned into
    a quietly textless video."""
    install_renderer(monkeypatch, tmp_path)
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", fail_if_called)
    stub_prepared_lyrics(monkeypatch, LYRIC_LINES, "lyrics_synced")

    def broken_generator(*args: object, **kwargs: object) -> str | None:
        raise AssGenerationError("генератор зламався")

    monkeypatch.setattr(worker_tasks.ass_subtitles, "build_ass_subtitles", broken_generator)

    with pytest.raises(AssGenerationError):
        await worker_tasks.render_video_off_transaction(AsyncMock(), render_claim(), tmp_path / "tmp" / "7")


def test_the_lyrics_query_reads_only_real_artist_names() -> None:
    assert worker_tasks.artist_names_from_stored(
        [{"name": "Burned Time Machine"}, {"name": " "}, {"hashtag": "x"}, "junk"]
    ) == ("Burned Time Machine",)


class FakeResult:
    """enough of `Result` for the one `select(AudioAsset)` the render claim runs."""

    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class FakeSession:
    """records its own lifetime in `registry`, so a test can ask "was any session open while ffmpeg ran?"
    instead of reading the code and hoping."""

    def __init__(
        self,
        registry: list[object],
        post: Post,
        track: Track,
        asset: object,
    ) -> None:
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

    async def get(
        self,
        model: object,
        primary_key: object,
        **kwargs: object,
    ) -> object:
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
    track.platform_links = {"youtubeMusic": {"url": "https://music.youtube.com/watch?v=n4bPzdIs_nc"}}
    track.duration_ms = 212_000
    track.cover_url = "https://cdn/cover.jpg"
    track.title = "Покаяння"
    track.artists = [{"name": "Burned Time Machine", "hashtag": None, "spotify_id": None}]

    post = Post()
    post.id = 7
    post.track_id = 3
    post.status = status
    post.fragment_start_ms = 0
    post.fragment_end_ms = 45_000
    return post, track


def install_fake_database(
    monkeypatch: pytest.MonkeyPatch,
    post: Post,
    track: Track,
    asset: object,
) -> list[object]:
    open_sessions: list[object] = []
    monkeypatch.setattr(worker_tasks, "build_engine", lambda *a, **k: FakeEngine())
    monkeypatch.setattr(
        worker_tasks,
        "build_session_factory",
        lambda engine: lambda: FakeSession(open_sessions, post, track, asset),
    )
    return open_sessions


async def test_yt_dlp_runs_with_no_session_and_no_lock_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """a download takes minutes, and a lock held across it would block the bot;
    asserted, not assumed."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post, track = media_fixtures(PostStatus.DOWNLOADING)
    open_sessions = install_fake_database(monkeypatch, post, track, None)
    sessions_during_download: list[int] = []

    def spy_download(claim: object, temporary_directory: Path) -> worker_tasks.DownloadOutcome:
        sessions_during_download.append(len(open_sessions))
        return download_outcome("2025.12.8")

    monkeypatch.setattr(worker_tasks, "download_audio_off_transaction", spy_download)
    monkeypatch.setattr(worker_tasks, "finish_download", AsyncMock())

    await worker_tasks.perform_download_post_audio(7, then_render=True)

    assert sessions_during_download == [0]
    assert post.status is PostStatus.DOWNLOADING


@pytest.mark.parametrize(
    "status",
    [
        PostStatus.DOWNLOADED,
        PostStatus.EDITING,
        PostStatus.RENDERING,
        PostStatus.RENDERED,
        PostStatus.PUBLISHED,
        PostStatus.FAILED,
    ],
)
async def test_a_redelivered_prepare_at_a_settled_post_does_nothing(
    status: PostStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post, track = media_fixtures(status)
    install_fake_database(monkeypatch, post, track, None)
    download = MagicMock()
    monkeypatch.setattr(worker_tasks, "download_audio_off_transaction", download)
    finish = AsyncMock()
    monkeypatch.setattr(worker_tasks, "finish_download", finish)

    await worker_tasks.perform_download_post_audio(7, then_render=False)

    download.assert_not_called()
    finish.assert_not_awaited()
    assert post.status is status


async def test_a_redelivered_prepare_at_downloading_runs_again_without_a_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post, track = media_fixtures(PostStatus.DOWNLOADING)
    install_fake_database(monkeypatch, post, track, None)
    monkeypatch.setattr(worker_tasks, "download_audio_off_transaction", lambda *args: download_outcome("2025.12.8"))
    finish = AsyncMock()
    monkeypatch.setattr(worker_tasks, "finish_download", finish)

    await worker_tasks.perform_download_post_audio(7, then_render=False)

    assert finish.await_args is not None
    assert finish.await_args.kwargs == {"then_render": False}


@pytest.mark.parametrize(
    ("task", "then_render", "soft_event"),
    [
        (worker_tasks.download_post_audio, True, "download_post_audio_soft_time_limit"),
        (worker_tasks.prepare_post_audio, False, "download_post_audio_soft_time_limit"),
    ],
)
def test_the_download_tasks_differ_only_in_the_render_flag(
    task: Any,
    then_render: bool,
    soft_event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock()
    monkeypatch.setattr(worker_tasks, "run_media_task", run)

    task.run(7)

    post_id, perform, event = run.call_args.args
    assert (post_id, event) == (7, soft_event)
    assert perform.func is worker_tasks.perform_download_post_audio
    assert perform.keywords == {"then_render": then_render}


def test_the_prepare_task_carries_the_download_limits() -> None:
    task = worker_tasks.prepare_post_audio

    assert task.name == PREPARE_POST_AUDIO_TASK_NAME
    assert task.soft_time_limit == worker_tasks.settings.download_task_soft_time_limit_seconds
    assert task.time_limit == worker_tasks.settings.download_task_time_limit_seconds


def test_a_prepare_soft_limit_fails_the_post_with_the_timeout_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail = MagicMock(return_value="failing")
    monkeypatch.setattr(worker_tasks, "fail_media_post_by_id", fail)
    calls: list[object] = []

    def run_async(coroutine: object) -> None:
        calls.append(coroutine)
        if len(calls) == 1:
            raise SoftTimeLimitExceeded

    monkeypatch.setattr(worker_tasks, "run_async", run_async)
    perform = MagicMock(return_value="performing")

    worker_tasks.run_media_task(7, perform, "download_post_audio_soft_time_limit")

    fail.assert_called_once_with(7, worker_tasks.MEDIA_TASK_TIMEOUT_ERROR)
    assert calls == ["performing", "failing"]


async def test_ffmpeg_runs_with_no_session_and_no_lock_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """the same assertion for the render, where the window is longest."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    post, track = media_fixtures(PostStatus.DOWNLOADED)
    asset = AudioAsset()
    asset.path = "/data/audio/3/n4bPzdIs_nc.opus"
    asset.duration_ms = 212_000
    open_sessions = install_fake_database(monkeypatch, post, track, asset)
    sessions_during_render: list[int] = []

    async def spy_render(
        session_factory: object,
        claim: object,
        temporary_directory: Path,
    ) -> worker_tasks.RenderOutcome:
        sessions_during_render.append(len(open_sessions))
        return worker_tasks.RenderOutcome(
            params_hash="b" * 64,
            path=Path("/data/render/7/" + "b" * 64 + ".mp4"),
            size_bytes=2_260_000,
            duration_ms=45_000,
            text_version=None,
            lyrics_reason="lyrics_absent",
        )

    monkeypatch.setattr(worker_tasks, "render_video_off_transaction", spy_render)
    monkeypatch.setattr(worker_tasks, "finish_render", AsyncMock())

    await worker_tasks.perform_render_post_video(7)

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
    """`session.execute()` is awaited and its result is not — a plain AsyncMock would hand back a coroutine where
    `.scalars()` is expected."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = render_row

    session = AsyncMock()
    session.execute.return_value = result
    return session


def test_a_post_that_never_reached_rendered_publishes_as_before() -> None:
    """the status decides: a post with no preview goes down the photo path, whatever is on the volume."""
    assert worker_tasks.post_reached_rendered(rendered_post(preview_message_id=None)) is False


def test_the_preview_reference_is_the_durable_trace_of_rendered() -> None:
    """by the time `publish_post` runs the status is already `publishing` — the claim moved it — so `RENDERED`
    has to be read from what it wrote."""
    assert worker_tasks.post_reached_rendered(rendered_post()) is True


async def test_a_previewed_post_whose_file_vanished_fails_instead_of_sending_a_photo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """falling back to `send_photo` would put a different post in the channel than the one the admin approved."""
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    render_row = SimpleNamespace(
        path=str(tmp_path / "render" / "7" / ("a" * 64 + ".mp4")),
        params_hash="a" * 64,
        duration_ms=45_000,
    )
    session = session_returning(render_row)

    with pytest.raises(worker_tasks.RenderedVideoUnavailableError):
        await worker_tasks.video_for_publication(session, rendered_post())


async def test_a_previewed_post_with_no_render_row_fails_loudly(tmp_path: Path) -> None:
    session = session_returning(None)

    with pytest.raises(worker_tasks.RenderedVideoUnavailableError):
        await worker_tasks.video_for_publication(session, rendered_post())


async def test_a_previewed_post_with_its_file_sends_the_renderers_own_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
    directory = tmp_path / "render" / "7"
    directory.mkdir(parents=True)
    video_path = directory / ("a" * 64 + ".mp4")
    video_path.write_bytes(b"video bytes")

    session = session_returning(SimpleNamespace(path=str(video_path), params_hash="a" * 64, duration_ms=45_000))

    video = await worker_tasks.video_for_publication(session, rendered_post())

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

    async def record_failure(
        session_factory: object,
        post_id: int,
        error: str,
    ) -> None:
        failures.append((post_id, error))

    monkeypatch.setattr(worker_tasks.celery_app, "send_task", refuse_to_send)
    monkeypatch.setattr(worker_tasks, "fail_media_post", record_failure)

    await worker_tasks.chain_render_after_download(AsyncMock(), 7)

    assert failures == [(7, worker_tasks.RENDER_DISPATCH_FAILED_ERROR)]


async def test_a_successful_download_chains_the_render_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the one internal hop: nothing in `bot/` knows the chain has two stages."""
    dispatched: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        worker_tasks.celery_app,
        "send_task",
        lambda name, kwargs: dispatched.append((name, kwargs)),
    )

    await worker_tasks.chain_render_after_download(AsyncMock(), 7)

    assert dispatched == [(worker_tasks.RENDER_POST_VIDEO_TASK_NAME, {"post_id": 7})]


async def test_a_timeout_during_the_preview_send_is_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SoftTimeLimitExceeded` is an ordinary `Exception`, so a blanket handler around the preview send would
    report «помилка» where the truth is «таймаут»."""
    reported: list[str] = []

    async def time_out(*args: object, **kwargs: object) -> None:
        raise SoftTimeLimitExceeded

    async def record(
        session_factory: object,
        post_id: int,
        error: str,
    ) -> None:
        reported.append(error)

    monkeypatch.setattr(worker_tasks, "fail_media_post", record)

    claim = worker_tasks.RenderClaim(
        post_id=7,
        track_id=3,
        video_id="n4bPzdIs_nc",
        audio_path="/data/audio/3/n4bPzdIs_nc.opus",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
        lyrics_query=LYRICS_QUERY,
    )
    outcome = worker_tasks.RenderOutcome(
        params_hash="a" * 64,
        path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
        size_bytes=2_260_000,
        duration_ms=45_000,
        text_version=None,
        lyrics_reason="lyrics_absent",
    )
    monkeypatch.setattr(worker_tasks, "finish_render", time_out)

    await worker_tasks.finish_render_or_fail(AsyncMock(), claim, outcome)

    assert reported == [worker_tasks.MEDIA_TASK_TIMEOUT_ERROR]


async def test_a_failed_preview_send_fails_the_post_from_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RENDERED` has no edge to `FAILED`, so the only state a delivery failure
    can be reported from is `RENDERING`."""
    reported: list[str] = []

    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("telegram refused the upload")

    async def record(
        session_factory: object,
        post_id: int,
        error: str,
    ) -> None:
        reported.append(error)

    monkeypatch.setattr(worker_tasks, "finish_render", refuse)
    monkeypatch.setattr(worker_tasks, "fail_media_post", record)

    claim = worker_tasks.RenderClaim(
        post_id=7,
        track_id=3,
        video_id="n4bPzdIs_nc",
        audio_path="/data/audio/3/n4bPzdIs_nc.opus",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
        lyrics_query=LYRICS_QUERY,
    )
    outcome = worker_tasks.RenderOutcome(
        params_hash="a" * 64,
        path=Path("/data/render/7/" + "a" * 64 + ".mp4"),
        size_bytes=2_260_000,
        duration_ms=45_000,
        text_version=None,
        lyrics_reason="lyrics_absent",
    )

    await worker_tasks.finish_render_or_fail(AsyncMock(), claim, outcome)

    assert reported == ["RuntimeError: telegram refused the upload"]


def claim_fixtures(status: PostStatus, *, preview_message_id: int | None) -> tuple[Post, list[FakeSession], Any]:
    post, track = media_fixtures(status)
    post.preview_message_id = preview_message_id
    asset = AudioAsset()
    asset.path = "/data/audio/3/n4bPzdIs_nc.opus"
    asset.duration_ms = 212_000

    sessions: list[FakeSession] = []

    def session_factory() -> FakeSession:
        session = FakeSession([], post, track, asset)
        sessions.append(session)
        return session

    return post, sessions, session_factory


async def test_a_previewed_post_is_never_downloaded_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, sessions, session_factory = claim_fixtures(PostStatus.DOWNLOADING, preview_message_id=99)
    fake_logger = install_fake_logger(monkeypatch)

    claim = await worker_tasks.claim_post_for_downloading(session_factory, 7)

    assert claim is None
    assert logged(fake_logger, "download_post_audio_task_arrived_at_previewed_post") == [
        {"post_id": 7, "status": "downloading"}
    ]
    assert post.status is PostStatus.DOWNLOADING
    assert sum(session.commits for session in sessions) == 0


async def test_a_download_that_finds_a_resolved_post_does_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a cancelled preparation is `resolved` again; a late delivery must not bring it back."""
    post, sessions, session_factory = claim_fixtures(PostStatus.RESOLVED, preview_message_id=None)
    fake_logger = install_fake_logger(monkeypatch)

    claim = await worker_tasks.claim_post_for_downloading(session_factory, 7)

    assert claim is None
    assert logged(fake_logger, "download_post_audio_task_arrived_at_settled_post") == [
        {"post_id": 7, "status": "resolved"}
    ]
    assert post.status is PostStatus.RESOLVED
    assert sum(session.commits for session in sessions) == 0


async def test_a_previewed_post_in_rendering_is_rendered_again() -> None:
    """the explicit choice of new bounds writes `rendering` and keeps the old preview reference until
    the new preview replaces it."""
    post, sessions, session_factory = claim_fixtures(PostStatus.RENDERING, preview_message_id=99)

    claim = await worker_tasks.claim_post_for_rendering(session_factory, 7)

    assert claim is not None
    assert post.status is PostStatus.RENDERING
    assert post.preview_message_id == 99
    assert sum(session.commits for session in sessions) == 1


@pytest.mark.parametrize("status", [PostStatus.RENDERED, PostStatus.PUBLISHING, PostStatus.PUBLISHED])
async def test_a_redelivered_render_of_a_previewed_post_does_no_work(status: PostStatus) -> None:
    post, sessions, session_factory = claim_fixtures(status, preview_message_id=99)

    claim = await worker_tasks.claim_post_for_rendering(session_factory, 7)

    assert claim is None
    assert post.status is status
    assert sum(session.commits for session in sessions) == 0


@pytest.mark.parametrize("claim_name", ["claim_post_for_downloading", "claim_post_for_rendering"])
async def test_a_task_that_finds_a_failed_post_leaves_it_alone(claim_name: str) -> None:
    """only a redelivery gets here; acting on it would re-render a post whose publication has just failed."""
    post, sessions, session_factory = claim_fixtures(PostStatus.FAILED, preview_message_id=None)

    claim = await getattr(worker_tasks, claim_name)(session_factory, 7)

    assert claim is None
    assert post.status is PostStatus.FAILED
    assert sum(session.commits for session in sessions) == 0


@pytest.mark.parametrize(
    ("claim_name", "status", "claimed_status"),
    [
        ("claim_post_for_downloading", PostStatus.DOWNLOADING, PostStatus.DOWNLOADING),
        ("claim_post_for_rendering", PostStatus.RENDERING, PostStatus.RENDERING),
        ("claim_post_for_rendering", PostStatus.DOWNLOADED, PostStatus.RENDERING),
    ],
)
async def test_a_post_without_a_preview_is_still_claimed(
    claim_name: str,
    status: PostStatus,
    claimed_status: PostStatus,
) -> None:
    """the statuses the bot and the download chain dispatch into keep working."""
    post, sessions, session_factory = claim_fixtures(status, preview_message_id=None)

    claim = await getattr(worker_tasks, claim_name)(session_factory, 7)

    assert claim is not None
    assert post.status is claimed_status
    assert sum(session.commits for session in sessions) == 1


async def test_the_render_claim_carries_the_lyrics_query_as_plain_values() -> None:
    post, _, session_factory = claim_fixtures(PostStatus.DOWNLOADED, preview_message_id=None)

    claim = await worker_tasks.claim_post_for_rendering(session_factory, post.id)

    assert claim is not None
    assert claim.lyrics_query == LyricsQuery(
        track_id=3,
        artist_names=("Burned Time Machine",),
        title="Покаяння",
        duration_ms=212_000,
        video_id="n4bPzdIs_nc",
    )


PREVIEW_CHAT_ID = 555
PREVIEW_MESSAGE_ID = 9_001


class NoticePipeline:
    """the render task with its database faked and Telegram replaced: records every session it opened
    and what `send_alert` saw when it ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.post, self.track = media_fixtures(PostStatus.DOWNLOADED)
        self.asset = AudioAsset()
        self.asset.path = "/data/audio/3/n4bPzdIs_nc.opus"
        self.asset.duration_ms = 212_000
        self.sessions: list[FakeSession] = []
        self.alerts: list[dict[str, object]] = []
        self.failures: list[str] = []
        # (track id, reason, alerts already sent): the lookup is queued after the notice.
        self.plain_lookups: list[tuple[int, str, int]] = []

        monkeypatch.setattr(worker_tasks, "queue_plain_lyrics_lookup", self.queue_lookup)
        monkeypatch.setattr(worker_tasks.settings, "media_root", tmp_path)
        monkeypatch.setattr(worker_tasks, "build_engine", lambda *a, **k: FakeEngine())
        monkeypatch.setattr(worker_tasks, "build_session_factory", lambda engine: self.session)
        monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=AsyncMock()))
        monkeypatch.setattr(worker_tasks, "deliver_preview_for", self.deliver_preview)
        monkeypatch.setattr(worker_tasks, "send_alert", self.send_alert)
        monkeypatch.setattr(worker_tasks, "fail_media_post", self.fail)

    def session(self) -> FakeSession:
        session = FakeSession([], self.post, self.track, self.asset)
        self.sessions.append(session)
        return session

    async def deliver_preview(self, post: Post, outcome: object) -> None:
        post.preview_chat_id = PREVIEW_CHAT_ID
        post.preview_message_id = PREVIEW_MESSAGE_ID

    async def send_alert(
        self,
        bot: object,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> bool:
        self.alerts.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "status": self.post.status,
                "commits": sum(session.commits for session in self.sessions),
            }
        )
        return True

    async def fail(
        self,
        session_factory: object,
        post_id: int,
        error: str,
    ) -> None:
        self.failures.append(error)

    async def queue_lookup(self, track_id: int, lyrics_reason: str) -> None:
        self.plain_lookups.append((track_id, lyrics_reason, len(self.alerts)))

    def render_returns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        text_version: str | None,
        reason: str,
    ) -> None:
        async def rendered(
            session_factory: object,
            claim: object,
            temporary_directory: Path,
        ) -> worker_tasks.RenderOutcome:
            return worker_tasks.RenderOutcome(
                params_hash="c" * 64,
                path=Path("/data/render/7/" + "c" * 64 + ".mp4"),
                size_bytes=2_260_000,
                duration_ms=45_000,
                text_version=text_version,
                lyrics_reason=reason,
            )

        monkeypatch.setattr(worker_tasks, "render_video_off_transaction", rendered)


async def test_a_video_without_text_is_explained_under_its_preview_after_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    pipeline.render_returns(monkeypatch, text_version=None, reason="lyrics_absent")

    await worker_tasks.perform_render_post_video(7)

    assert pipeline.alerts == [
        {
            "chat_id": PREVIEW_CHAT_ID,
            "text": ("Пост 7: Тексту цього треку немає ні на YouTube Music, ні в LRCLIB — відео без тексту."),
            "reply_to_message_id": PREVIEW_MESSAGE_ID,
            "status": PostStatus.RENDERED,
            # transaction 1 and transaction 2 are both committed by then
            "commits": 2,
        }
    ]
    assert pipeline.failures == []


async def test_the_plain_lookup_is_queued_after_the_notice_for_the_tracks_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    pipeline.render_returns(monkeypatch, text_version=None, reason="lyrics_absent")

    await worker_tasks.perform_render_post_video(7)

    assert pipeline.plain_lookups == [(3, "lyrics_absent", 1)]


async def test_no_lookup_is_queued_when_the_preview_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    pipeline.render_returns(monkeypatch, text_version=None, reason="lyrics_absent")

    async def refuse(post: Post, outcome: object) -> None:
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(worker_tasks, "deliver_preview_for", refuse)

    await worker_tasks.perform_render_post_video(7)

    assert pipeline.plain_lookups == []
    assert pipeline.alerts == []


async def test_a_video_with_text_gets_no_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    pipeline.render_returns(monkeypatch, text_version="0123456789abcdef", reason="lyrics_synced")

    await worker_tasks.perform_render_post_video(7)

    assert pipeline.post.status is PostStatus.RENDERED
    assert pipeline.alerts == []


async def test_the_log_of_a_video_with_text_runs_to_its_preview_and_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """without the last line the log of a video with text ends in silence, which reads the same as a
    notice that was never attempted."""
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    fake_logger = install_fake_logger(monkeypatch)
    pipeline.render_returns(monkeypatch, text_version="0123456789abcdef", reason="lyrics_synced")

    await worker_tasks.perform_render_post_video(7)

    assert logged(fake_logger, "render_post_video_preview_sent") == [
        {"post_id": 7, "track_id": 3, "preview_message_id": PREVIEW_MESSAGE_ID}
    ]
    [finished] = logged(fake_logger, "render_post_video_finished")
    assert (finished["post_id"], finished["track_id"]) == (7, 3)
    assert finished["text_version"] == "0123456789abcdef"
    assert logged(fake_logger, "render_post_video_no_text_notice_not_needed") == [
        {"post_id": 7, "text_version": "0123456789abcdef"}
    ]


async def test_a_failed_notice_leaves_the_post_rendered_and_the_task_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the notice explains a video that is already in the chat; losing it must never fail the post nor surface
    as a task error Celery would record."""
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    pipeline.render_returns(monkeypatch, text_version=None, reason="lyrics_source_unavailable")

    async def broken_send(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(worker_tasks, "send_alert", broken_send)

    await worker_tasks.perform_render_post_video(7)

    assert pipeline.post.status is PostStatus.RENDERED
    assert pipeline.failures == []


async def test_a_redelivered_render_sends_no_second_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    pipeline.render_returns(monkeypatch, text_version=None, reason="lyrics_absent")

    await worker_tasks.perform_render_post_video(7)
    await worker_tasks.perform_render_post_video(7)

    assert len(pipeline.alerts) == 1


async def test_a_reused_file_without_text_is_explained_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no ffmpeg on this path, yet the admin still needs the reason."""
    pipeline = NoticePipeline(monkeypatch, tmp_path)
    stub_prepared_lyrics(monkeypatch, (), "lyrics_instrumental")
    claim = render_claim()

    final_path = worker_tasks.media_storage.render_file_path(tmp_path, claim.post_id, params_hash_for(claim, None))
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"already rendered")
    probe = media_tools.MediaProbe(
        path=final_path,
        size_bytes=final_path.stat().st_size,
        duration_ms=45_000,
        streams=(media_tools.StreamInfo("video", "h264", 720, 1280, "yuv420p", "tv"),),
    )
    monkeypatch.setattr(worker_tasks.media_tools, "verify_media", lambda path, **k: probe)
    monkeypatch.setattr(worker_tasks.video_render, "render_fragment_within_size_limit", fail_if_called)

    await worker_tasks.perform_render_post_video(7)

    assert [alert["text"] for alert in pipeline.alerts] == [
        "Пост 7: Трек інструментальний — відео без тексту, так і має бути."
    ]


async def test_lines_that_all_miss_the_fragment_are_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """an empty `.ass` from stored lines is not "no lyrics found": the text exists,
    the chosen fragment just holds none of it."""
    renderer = install_renderer(monkeypatch, tmp_path)
    late_lines = (LyricLine(line_index=0, start_ms=120_000, text="Пізній рядок"),)
    stub_prepared_lyrics(monkeypatch, late_lines, LYRICS_ALREADY_STORED)

    outcome = await worker_tasks.render_video_off_transaction(AsyncMock(), render_claim(), tmp_path / "tmp" / "7")

    assert renderer.subtitles == [None]
    assert outcome.text_version is None
    assert outcome.lyrics_reason == LYRICS_OUTSIDE_FRAGMENT


async def test_new_bounds_change_the_text_and_the_hash_and_lines_outside_them_say_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """timecodes are absolute, so the same stored lines follow whatever fragment is chosen."""
    install_renderer(monkeypatch, tmp_path)
    claim = render_claim()
    later_claim = replace(claim, fragment_start_ms=60_000, fragment_end_ms=105_000)
    lines = (
        LyricLine(line_index=0, start_ms=1_000, text="Перший рядок"),
        LyricLine(line_index=1, start_ms=70_000, text="Приспів"),
    )
    stub_prepared_lyrics(monkeypatch, lines, LYRICS_ALREADY_STORED)

    first = await worker_tasks.render_video_off_transaction(AsyncMock(), claim, tmp_path / "a")
    second = await worker_tasks.render_video_off_transaction(AsyncMock(), later_claim, tmp_path / "b")

    assert first.text_version is not None
    assert second.text_version is not None
    assert first.text_version != second.text_version
    assert first.params_hash != second.params_hash

    stub_prepared_lyrics(monkeypatch, lines[:1], LYRICS_ALREADY_STORED)
    missed = await worker_tasks.render_video_off_transaction(AsyncMock(), later_claim, tmp_path / "c")

    assert missed.text_version is None
    assert missed.lyrics_reason == LYRICS_OUTSIDE_FRAGMENT
