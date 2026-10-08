"""The subprocess wrapper: argument lists and `shell=False`, hard timeouts, and
`ffprobe`, not the exit code, deciding whether an output is usable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pisniar.worker import ffmpeg as media_tools


def make_completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def probe_payload(
    *, duration: str | None = "45.000", streams: list[dict[str, Any]] | None = None
) -> str:
    payload: dict[str, Any] = {"streams": streams if streams is not None else []}
    payload["format"] = {} if duration is None else {"duration": duration}
    return json.dumps(payload)


AUDIO_STREAM = {"codec_type": "audio", "codec_name": "aac"}
VIDEO_STREAM = {
    "codec_type": "video",
    "codec_name": "h264",
    "width": 720,
    "height": 1280,
    "pix_fmt": "yuv420p",
    "color_range": "tv",
}


def test_the_command_is_a_list_and_the_shell_is_never_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a shell string is how a value out of the database becomes a command.
    There is no code path here that builds one."""
    recorded: dict[str, Any] = {}

    def fake_run(command: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return make_completed()

    monkeypatch.setattr(media_tools.subprocess, "run", fake_run)

    media_tools.run_media_tool("ffmpeg", ["-i", "a b;rm -rf /"], timeout_seconds=7)

    assert recorded["command"] == ["ffmpeg", "-i", "a b;rm -rf /"]
    assert recorded["kwargs"]["shell"] is False
    assert recorded["kwargs"]["timeout"] == 7


def test_a_missing_binary_is_named_rather_than_raising_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(media_tools.subprocess, "run", fake_run)

    with pytest.raises(media_tools.MediaToolNotFoundError, match="ffprobe"):
        media_tools.run_media_tool("ffprobe", ["-version"], timeout_seconds=5)


def test_a_timeout_becomes_its_own_error_class(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=3)

    monkeypatch.setattr(media_tools.subprocess, "run", fake_run)

    with pytest.raises(media_tools.MediaToolTimeoutError, match="3"):
        media_tools.run_media_tool("ffmpeg", ["-i", "x"], timeout_seconds=3)


def test_a_non_zero_exit_carries_the_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media_tools.subprocess,
        "run",
        lambda command, **kwargs: make_completed(returncode=1, stderr="Invalid data found"),
    )

    with pytest.raises(media_tools.MediaToolFailedError) as raised:
        media_tools.run_media_tool_checked("ffmpeg", ["-i", "x"], timeout_seconds=5)

    assert raised.value.exit_code == 1
    assert "Invalid data found" in raised.value.stderr_tail


def test_run_media_tool_checked_returns_the_process_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_tools.subprocess, "run", lambda command, **kwargs: make_completed(stdout="2025.12.8")
    )

    completed = media_tools.run_media_tool_checked("yt-dlp", ["--version"], timeout_seconds=5)

    assert completed.stdout == "2025.12.8"


def test_the_bytes_runner_keeps_the_argument_discipline_and_returns_raw_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(command: object, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout=b"\x00\xff")

    monkeypatch.setattr(media_tools.subprocess, "run", fake_run)

    completed = media_tools.run_media_tool_bytes(
        "ffmpeg", ["-i", "a b;rm -rf /"], timeout_seconds=9
    )

    assert completed.stdout == b"\x00\xff"
    assert recorded["command"] == ["ffmpeg", "-i", "a b;rm -rf /"]
    assert recorded["kwargs"]["shell"] is False
    assert recorded["kwargs"]["timeout"] == 9
    assert recorded["kwargs"]["text"] is False
    assert recorded["kwargs"]["encoding"] is None


def test_the_bytes_runner_names_a_missing_binary_and_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(command: object, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(media_tools.subprocess, "run", missing)
    with pytest.raises(media_tools.MediaToolNotFoundError, match="ffmpeg"):
        media_tools.run_media_tool_bytes("ffmpeg", ["-version"], timeout_seconds=5)

    def slow(command: object, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=3)

    monkeypatch.setattr(media_tools.subprocess, "run", slow)
    with pytest.raises(media_tools.MediaToolTimeoutError):
        media_tools.run_media_tool_bytes("ffmpeg", ["-i", "x"], timeout_seconds=3)


def test_stderr_tail_is_bounded() -> None:
    tail = media_tools.stderr_tail("x" * (media_tools.STDERR_TAIL_CHARACTERS + 500))

    assert len(tail) == media_tools.STDERR_TAIL_CHARACTERS


def test_stderr_tail_of_nothing_is_empty() -> None:
    assert media_tools.stderr_tail(None) == ""
    assert media_tools.stderr_tail("") == ""


def test_probing_a_missing_file_is_loud(tmp_path: Path) -> None:
    with pytest.raises(media_tools.MediaProbeError, match="не існує"):
        media_tools.probe_media(tmp_path / "absent.mp4")


def test_an_empty_file_is_refused_before_ffprobe_is_even_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg can exit 0 and leave a zero-byte file, the pipeline's main silent
    failure; removing this guard must break this test."""
    empty = tmp_path / "render.mp4"
    empty.touch()

    def fail_if_called(*args: Any, **kwargs: Any) -> object:
        raise AssertionError("ffprobe must not be invoked for a zero-byte file")

    monkeypatch.setattr(media_tools, "run_media_tool_checked", fail_if_called)

    with pytest.raises(media_tools.MediaProbeError, match="порожній"):
        media_tools.probe_media(empty)


def stub_probe(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(
        media_tools,
        "run_media_tool_checked",
        lambda binary, arguments, **kwargs: make_completed(stdout=stdout),
    )


def write_file(tmp_path: Path, name: str = "media.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not really a container")
    return path


def test_a_probe_reports_size_duration_and_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(streams=[VIDEO_STREAM, AUDIO_STREAM]))

    probe = media_tools.probe_media(path)

    assert probe.size_bytes == path.stat().st_size
    assert probe.duration_ms == 45_000
    assert probe.video_streams[0].pix_fmt == "yuv420p"
    assert probe.video_streams[0].color_range == "tv"
    assert probe.audio_streams[0].codec_name == "aac"


def test_a_duration_only_on_the_stream_is_still_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """what yt-dlp's raw audio containers actually look like."""
    path = write_file(tmp_path, "audio.webm")
    stub_probe(
        monkeypatch,
        probe_payload(duration=None, streams=[{**AUDIO_STREAM, "duration": "212.345"}]),
    )

    assert media_tools.probe_media(path).duration_ms == 212_345


def test_a_probe_without_any_duration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(duration=None, streams=[AUDIO_STREAM]))

    with pytest.raises(media_tools.MediaProbeError, match="тривалості"):
        media_tools.probe_media(path)


def test_a_zero_duration_is_an_absence_not_a_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(duration="0.000", streams=[AUDIO_STREAM]))

    with pytest.raises(media_tools.MediaProbeError, match="тривалості"):
        media_tools.probe_media(path)


def test_non_json_output_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, "ffprobe version 7.1.5")

    with pytest.raises(media_tools.MediaProbeError, match="не-JSON"):
        media_tools.probe_media(path)


def test_verify_media_demands_a_video_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(streams=[AUDIO_STREAM]))

    with pytest.raises(media_tools.MediaProbeError, match="відеопотоку"):
        media_tools.verify_media(path, require_video=True)


def test_verify_media_demands_an_audio_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(streams=[VIDEO_STREAM]))

    with pytest.raises(media_tools.MediaProbeError, match="аудіопотоку"):
        media_tools.verify_media(path, require_audio=True)


def test_verify_media_refuses_a_duration_outside_the_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(duration="40.000", streams=[VIDEO_STREAM, AUDIO_STREAM]))

    with pytest.raises(media_tools.MediaProbeError, match="не збігається"):
        media_tools.verify_media(path, expected_duration_ms=45_000, duration_tolerance_ms=500)


def test_verify_media_accepts_a_duration_inside_the_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_file(tmp_path)
    stub_probe(monkeypatch, probe_payload(duration="45.400", streams=[VIDEO_STREAM, AUDIO_STREAM]))

    probe = media_tools.verify_media(
        path,
        require_video=True,
        require_audio=True,
        expected_duration_ms=45_000,
        duration_tolerance_ms=500,
    )

    assert probe.duration_ms == 45_400
