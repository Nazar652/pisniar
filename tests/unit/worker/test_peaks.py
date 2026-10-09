"""Waveform peaks: bucketing, normalisation, the ffmpeg arguments, the atomic write and a
failure that costs nothing but the waveform. The file format is in `test_peaks_format.py`.
"""

from __future__ import annotations

import json
import subprocess
from array import array
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from pisniar.worker import peaks
from pisniar.worker import storage as media_storage
from pisniar.worker.ffmpeg import MediaToolError, MediaToolFailedError
from pisniar.worker.peaks_format import peaks_document, read_peaks

VIDEO_ID = "n4bPzdIs_nc"


@pytest.fixture(autouse=True)
def peaks_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """a stand-in: a cached structlog proxy defeats `capture_logs()`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(peaks, "logger", fake_logger)
    return fake_logger


def pcm_of(*samples: int) -> bytes:
    return array("h", samples).tobytes()


def bucket_of(*, loudest: int, length: int = peaks.BUCKET_SAMPLES) -> list[int]:
    return [loudest, *([0] * (length - 1))]


def completed(
    returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["ffmpeg"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def stub_ffmpeg(monkeypatch: pytest.MonkeyPatch, pcm: bytes) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(binary: str, arguments: list[str], *, timeout_seconds: float) -> Any:
        calls.append([binary, *arguments])
        return completed(stdout=pcm)

    monkeypatch.setattr(peaks, "run_media_tool_bytes", fake_run)
    return calls


def write_document(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_a_bucket_is_400_samples_which_is_twenty_to_the_second() -> None:
    assert peaks.BUCKET_SAMPLES == 400
    assert peaks.SAMPLE_RATE_HZ // peaks.BUCKET_SAMPLES == peaks.PEAKS_PER_SECOND


def test_each_bucket_keeps_its_loudest_sample_scaled_to_the_loudest_bucket() -> None:
    samples = bucket_of(loudest=1000) + bucket_of(loudest=500) + bucket_of(loudest=250)

    assert peaks.peak_values(pcm_of(*samples)) == [255, 128, 64]


def test_a_negative_swing_counts_as_loudly_as_a_positive_one() -> None:
    samples = bucket_of(loudest=-1000) + bucket_of(loudest=1000)

    assert peaks.peak_values(pcm_of(*samples)) == [255, 255]


def test_the_most_negative_sample_does_not_overflow() -> None:
    assert peaks.peak_values(pcm_of(-32768, 0, 0)) == [255]


def test_the_last_bucket_may_be_short_and_still_counts() -> None:
    samples = [*bucket_of(loudest=1000), 500, 0, 0]

    assert peaks.peak_values(pcm_of(*samples)) == [255, 128]


def test_silence_is_zeros_not_a_division_by_zero() -> None:
    assert peaks.peak_values(pcm_of(*([0] * (peaks.BUCKET_SAMPLES * 2)))) == [0, 0]


def test_a_dangling_byte_is_dropped() -> None:
    assert peaks.peak_values(pcm_of(10, 20) + b"\x01") == [255]


def test_no_samples_at_all_is_a_failure_not_an_empty_waveform() -> None:
    with pytest.raises(MediaToolError, match="семпла"):
        peaks.peak_values(b"")


def test_ten_minutes_of_audio_are_twelve_thousand_values() -> None:
    ten_minutes = pcm_of(*([1] * (peaks.SAMPLE_RATE_HZ * 600)))

    assert len(peaks.peak_values(ten_minutes)) == 12_000


def test_ffmpeg_is_asked_for_mono_8_khz_s16le_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_ffmpeg(monkeypatch, pcm_of(1))
    audio = tmp_path / f"{VIDEO_ID}.opus"

    peaks.decode_pcm(audio)

    assert calls == [
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        ]
    ]


def test_the_ffmpeg_timeout_is_thirty_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, float] = {}

    def fake_run(binary: str, arguments: list[str], *, timeout_seconds: float) -> Any:
        recorded["timeout"] = timeout_seconds
        return completed(stdout=pcm_of(1))

    monkeypatch.setattr(peaks, "run_media_tool_bytes", fake_run)

    peaks.decode_pcm(tmp_path / "a.opus")

    assert peaks.PEAKS_TIMEOUT_SECONDS == 30
    assert recorded["timeout"] == 30


def test_a_non_zero_ffmpeg_exit_carries_its_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        peaks,
        "run_media_tool_bytes",
        lambda binary, arguments, *, timeout_seconds: completed(
            returncode=1, stderr=b"Invalid data found"
        ),
    )

    with pytest.raises(MediaToolFailedError, match="Invalid data found"):
        peaks.decode_pcm(tmp_path / "a.opus")


def test_peaks_are_computed_and_published_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_ffmpeg(monkeypatch, pcm_of(*(bucket_of(loudest=100) + bucket_of(loudest=50))))
    temporary = media_storage.task_temporary_directory(tmp_path, 7)

    result = peaks.ensure_peaks(tmp_path, 3, VIDEO_ID, tmp_path / "a.opus", temporary)

    expected = media_storage.peaks_file_path(tmp_path, 3, VIDEO_ID)
    assert result == expected
    assert read_peaks(expected) == [255, 128]
    assert list(temporary.iterdir()) == []


def test_a_valid_existing_file_skips_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = media_storage.peaks_file_path(tmp_path, 3, VIDEO_ID)
    final.parent.mkdir(parents=True)
    final.write_text(peaks_document([5]), encoding="utf-8")

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("ffmpeg must not run when valid peaks are already on the volume")

    monkeypatch.setattr(peaks, "run_media_tool_bytes", fail_if_called)

    assert peaks.ensure_peaks(tmp_path, 3, VIDEO_ID, tmp_path / "a.opus", tmp_path / "tmp") == final
    assert read_peaks(final) == [5]


def test_a_file_of_another_format_version_is_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = media_storage.peaks_file_path(tmp_path, 3, VIDEO_ID)
    write_document(final, {"format_version": 0, "peaks_per_second": 20, "values": [9]})
    stub_ffmpeg(monkeypatch, pcm_of(10, 20))

    peaks.ensure_peaks(tmp_path, 3, VIDEO_ID, tmp_path / "a.opus", tmp_path / "tmp")

    assert read_peaks(final) == [255]


def test_a_failure_is_a_warning_and_none_with_nothing_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peaks_logger: MagicMock
) -> None:
    monkeypatch.setattr(
        peaks,
        "run_media_tool_bytes",
        lambda binary, arguments, *, timeout_seconds: completed(returncode=1, stderr=b"boom"),
    )
    temporary = tmp_path / "tmp"

    result = peaks.ensure_peaks(tmp_path, 3, VIDEO_ID, tmp_path / "a.opus", temporary)

    assert result is None
    assert peaks_logger.warning.call_args.args[0] == "audio_peaks_failed"
    assert not media_storage.peaks_file_path(tmp_path, 3, VIDEO_ID).exists()


def test_more_values_than_the_reader_accepts_are_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peaks_logger: MagicMock
) -> None:
    monkeypatch.setattr(peaks, "MAX_PEAK_VALUES", 1)
    stub_ffmpeg(monkeypatch, pcm_of(*(bucket_of(loudest=100) + bucket_of(loudest=50))))

    result = peaks.ensure_peaks(tmp_path, 3, VIDEO_ID, tmp_path / "a.opus", tmp_path / "tmp")

    assert result is None
    assert peaks_logger.warning.call_args.args[0] == "audio_peaks_failed"
    assert not media_storage.peaks_file_path(tmp_path, 3, VIDEO_ID).exists()


def test_the_soft_time_limit_is_never_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_soft_limit(*args: object, **kwargs: object) -> object:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(peaks, "run_media_tool_bytes", raise_soft_limit)

    with pytest.raises(SoftTimeLimitExceeded):
        peaks.ensure_peaks(tmp_path, 3, VIDEO_ID, tmp_path / "a.opus", tmp_path / "tmp")
