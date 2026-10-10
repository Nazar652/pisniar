"""The peaks backfill: which stored audio it visits, what it skips, that a second run does
nothing, and the exit code of the command.
"""

from __future__ import annotations

import json
import subprocess
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from pisniar.worker import peaks
from pisniar.worker import storage as media_storage
from pisniar.worker.peaks_format import peaks_document, read_peaks

FIRST_VIDEO_ID = "n4bPzdIs_nc"
SECOND_VIDEO_ID = "EwTrrXSvFFU"


@pytest.fixture(autouse=True)
def peaks_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """a stand-in: a cached structlog proxy defeats `capture_logs()`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(peaks, "logger", fake_logger)
    return fake_logger


def store_audio(
    media_root: Path,
    track_id: int,
    video_id: str,
    extension: str = "webm",
) -> Path:
    path = media_storage.audio_file_path(media_root, track_id, video_id, extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


def stub_ffmpeg(monkeypatch: pytest.MonkeyPatch, returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []
    pcm = array("h", [100] * peaks.BUCKET_SAMPLES).tobytes()

    def fake_run(
        binary: str,
        arguments: list[str],
        *,
        timeout_seconds: float,
    ) -> Any:
        calls.append(arguments)
        return subprocess.CompletedProcess(args=["ffmpeg"], returncode=returncode, stdout=pcm, stderr=b"boom")

    monkeypatch.setattr(peaks, "run_media_tool_bytes", fake_run)
    return calls


def test_stored_audio_is_listed_by_track_and_video_id(tmp_path: Path) -> None:
    second = store_audio(tmp_path, 10, SECOND_VIDEO_ID, "opus")
    first = store_audio(tmp_path, 2, FIRST_VIDEO_ID)

    assert sorted(media_storage.stored_audio_files(tmp_path)) == [
        (2, FIRST_VIDEO_ID, first),
        (10, SECOND_VIDEO_ID, second),
    ]


def test_peaks_files_strays_and_empty_audio_are_never_listed(tmp_path: Path) -> None:
    store_audio(tmp_path, 3, FIRST_VIDEO_ID)
    peaks_path = media_storage.peaks_file_path(tmp_path, 3, FIRST_VIDEO_ID)
    peaks_path.parent.mkdir(parents=True)
    peaks_path.write_text("{}", encoding="utf-8")
    (media_storage.audio_directory(tmp_path, 3) / "notes.txt").write_text("x", encoding="utf-8")
    empty = media_storage.audio_file_path(tmp_path, 4, SECOND_VIDEO_ID, "webm")
    empty.parent.mkdir(parents=True)
    empty.touch()
    (tmp_path / "audio" / "not-a-track").mkdir()
    (tmp_path / "audio" / "0").mkdir()
    (tmp_path / "audio" / "stray.webm").write_bytes(b"x")

    assert [entry[:2] for entry in media_storage.stored_audio_files(tmp_path)] == [(3, FIRST_VIDEO_ID)]


def test_an_empty_volume_lists_nothing(tmp_path: Path) -> None:
    assert media_storage.stored_audio_files(tmp_path) == []


def test_audio_without_peaks_gets_them_and_the_staging_directory_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_ffmpeg(monkeypatch)
    store_audio(tmp_path, 2, FIRST_VIDEO_ID)

    summary = peaks.backfill_peaks(tmp_path)

    assert summary == peaks.BackfillSummary(written=1, current=0, failed=0)
    assert read_peaks(media_storage.peaks_file_path(tmp_path, 2, FIRST_VIDEO_ID)) is not None
    assert not (tmp_path / "tmp" / peaks.BACKFILL_DIRECTORY_NAME).exists()


def test_a_file_of_the_old_version_is_rewritten_and_a_current_one_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_ffmpeg(monkeypatch)
    store_audio(tmp_path, 2, FIRST_VIDEO_ID)
    store_audio(tmp_path, 3, SECOND_VIDEO_ID)
    old = media_storage.peaks_file_path(tmp_path, 2, FIRST_VIDEO_ID)
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"format_version": 1, "peaks_per_second": 20, "values": [9]}), encoding="utf-8")
    current = media_storage.peaks_file_path(tmp_path, 3, SECOND_VIDEO_ID)
    current.parent.mkdir(parents=True)
    current.write_text(peaks_document([5]), encoding="utf-8")

    summary = peaks.backfill_peaks(tmp_path)

    assert summary == peaks.BackfillSummary(written=1, current=1, failed=0)
    assert len(calls) == 1
    assert read_peaks(old) == [255]
    assert read_peaks(current) == [5]


def test_a_second_run_runs_ffmpeg_for_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_ffmpeg(monkeypatch)
    store_audio(tmp_path, 2, FIRST_VIDEO_ID)
    peaks.backfill_peaks(tmp_path)
    calls.clear()

    summary = peaks.backfill_peaks(tmp_path)

    assert summary == peaks.BackfillSummary(written=0, current=1, failed=0)
    assert calls == []


def test_a_failing_file_is_counted_and_does_not_stop_the_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_ffmpeg(monkeypatch, returncode=1)
    store_audio(tmp_path, 2, FIRST_VIDEO_ID)
    store_audio(tmp_path, 3, SECOND_VIDEO_ID)

    summary = peaks.backfill_peaks(tmp_path)

    assert summary == peaks.BackfillSummary(written=0, current=0, failed=2)


def test_the_summary_is_logged_with_its_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peaks_logger: MagicMock,
) -> None:
    stub_ffmpeg(monkeypatch)
    store_audio(tmp_path, 2, FIRST_VIDEO_ID)

    peaks.backfill_peaks(tmp_path)

    last_call = peaks_logger.info.call_args_list[-1]
    assert last_call.args[0] == "audio_peaks_backfill_finished"
    assert last_call.kwargs == {"written": 1, "current": 0, "failed": 0}


@pytest.mark.parametrize(("failed", "exit_code"), [(0, 0), (2, 1)])
def test_the_command_fails_only_when_a_file_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed: int,
    exit_code: int,
) -> None:
    settings = SimpleNamespace(log_format="json", log_level="INFO", media_root=tmp_path)
    monkeypatch.setattr(peaks, "get_settings", lambda: settings)
    monkeypatch.setattr(peaks, "redaction_secrets", lambda settings: [])
    monkeypatch.setattr(peaks, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        peaks,
        "backfill_peaks",
        lambda root: peaks.BackfillSummary(written=1, current=0, failed=failed),
    )

    assert peaks.main() == exit_code
