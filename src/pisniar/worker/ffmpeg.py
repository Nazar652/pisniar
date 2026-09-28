"""`ffmpeg`/`ffprobe` as subprocesses: argument lists, never a shell, always a
hard timeout. ffmpeg can exit 0 on an unusable file, so `verify_media()`, not
the exit code, is what says a render succeeded.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pisniar.logging import get_logger

logger = get_logger(__name__)

FFMPEG_BINARY = "ffmpeg"
FFPROBE_BINARY = "ffprobe"

PROBE_TIMEOUT_SECONDS = 30

# Enough of stderr to diagnose with, bounded so a chatty failure cannot turn
# `post.last_error` (and the admin alert built from it) into a wall of text.
STDERR_TAIL_CHARACTERS = 2000


class MediaToolError(RuntimeError):
    """Base for every ffmpeg/ffprobe failure. Deliberately not routed through
    `isolated_source_call`: a failure here means there is no product."""


class MediaToolNotFoundError(MediaToolError):
    def __init__(self, binary: str) -> None:
        self.binary = binary
        super().__init__(f"{binary} не знайдено у PATH контейнера")


class MediaToolTimeoutError(MediaToolError):
    def __init__(self, binary: str, timeout_seconds: float) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{binary} перевищив таймаут {timeout_seconds} с і був зупинений")


class MediaToolFailedError(MediaToolError):
    def __init__(self, binary: str, exit_code: int, stderr_tail: str) -> None:
        self.binary = binary
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        super().__init__(f"{binary} завершився з кодом {exit_code}: {stderr_tail}")


class MediaProbeError(MediaToolError):
    """`ffprobe` ran but the file it described is not usable."""


@dataclass(frozen=True)
class StreamInfo:
    codec_type: str
    codec_name: str | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    color_range: str | None


@dataclass(frozen=True)
class MediaProbe:
    path: Path
    size_bytes: int
    duration_ms: int
    streams: tuple[StreamInfo, ...]

    @property
    def video_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "video")

    @property
    def audio_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "audio")


def run_media_tool(
    binary: str, arguments: list[str], *, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Every value in `arguments` is built from validated ids, never concatenated."""
    command = [binary, *arguments]
    logger.info("media_tool_started", binary=binary, argument_count=len(arguments))

    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise MediaToolNotFoundError(binary) from error
    except subprocess.TimeoutExpired as error:
        logger.error("media_tool_timed_out", binary=binary, timeout_seconds=timeout_seconds)
        raise MediaToolTimeoutError(binary, timeout_seconds) from error

    logger.info("media_tool_finished", binary=binary, exit_code=completed.returncode)
    return completed


def run_media_tool_checked(
    binary: str, arguments: list[str], *, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    completed = run_media_tool(binary, arguments, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        raise MediaToolFailedError(binary, completed.returncode, stderr_tail(completed.stderr))
    return completed


def stderr_tail(stderr: str | None) -> str:
    if not stderr:
        return ""
    return stderr.strip()[-STDERR_TAIL_CHARACTERS:]


def probe_media(path: Path, *, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> MediaProbe:
    """Size first, on purpose: a zero-byte output is what ffmpeg exiting 0 leaves."""
    if not path.is_file():
        raise MediaProbeError(f"файл не існує: {path}")

    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise MediaProbeError(f"файл порожній (0 байт): {path}")

    completed = run_media_tool_checked(
        FFPROBE_BINARY,
        [
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
    )

    payload = _parse_probe_payload(completed.stdout, path)
    streams = _parse_streams(payload.get("streams"))
    duration_ms = _parse_duration_ms(payload, streams_payload=payload.get("streams"), path=path)

    return MediaProbe(path=path, size_bytes=size_bytes, duration_ms=duration_ms, streams=streams)


def verify_media(
    path: Path,
    *,
    require_video: bool = False,
    require_audio: bool = False,
    expected_duration_ms: int | None = None,
    duration_tolerance_ms: int = 0,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> MediaProbe:
    probe = probe_media(path, timeout_seconds=timeout_seconds)

    if require_video and not probe.video_streams:
        raise MediaProbeError(f"у файлі немає відеопотоку: {path}")
    if require_audio and not probe.audio_streams:
        raise MediaProbeError(f"у файлі немає аудіопотоку: {path}")
    if probe.duration_ms <= 0:
        raise MediaProbeError(f"тривалість файлу нульова: {path}")

    if expected_duration_ms is not None:
        difference_ms = abs(probe.duration_ms - expected_duration_ms)
        if difference_ms > duration_tolerance_ms:
            raise MediaProbeError(
                f"тривалість {probe.duration_ms} мс не збігається з очікуваною "
                f"{expected_duration_ms} мс (розбіжність {difference_ms} мс, "
                f"допуск {duration_tolerance_ms} мс): {path}"
            )

    return probe


def _parse_probe_payload(stdout: str, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as error:
        raise MediaProbeError(f"ffprobe повернув не-JSON для {path}: {error}") from error

    if not isinstance(payload, dict):
        raise MediaProbeError(f"ffprobe повернув не об'єкт для {path}")
    return payload


def _parse_streams(streams_payload: object) -> tuple[StreamInfo, ...]:
    if not isinstance(streams_payload, list):
        return ()

    streams: list[StreamInfo] = []
    for entry in streams_payload:
        if not isinstance(entry, dict):
            continue
        codec_type = entry.get("codec_type")
        if not isinstance(codec_type, str):
            continue
        streams.append(
            StreamInfo(
                codec_type=codec_type,
                codec_name=_optional_string(entry.get("codec_name")),
                width=_optional_int(entry.get("width")),
                height=_optional_int(entry.get("height")),
                pix_fmt=_optional_string(entry.get("pix_fmt")),
                color_range=_optional_string(entry.get("color_range")),
            )
        )
    return tuple(streams)


def _parse_duration_ms(payload: dict[str, object], *, streams_payload: object, path: Path) -> int:
    format_payload = payload.get("format")
    if isinstance(format_payload, dict):
        duration_ms = _seconds_to_ms(format_payload.get("duration"))
        if duration_ms is not None:
            return duration_ms

    # A raw audio container (what yt-dlp hands back) often carries the
    # duration only on its stream, not in the container header.
    if isinstance(streams_payload, list):
        for entry in streams_payload:
            if isinstance(entry, dict):
                duration_ms = _seconds_to_ms(entry.get("duration"))
                if duration_ms is not None:
                    return duration_ms

    raise MediaProbeError(f"ffprobe не повідомив тривалості для {path}")


def _seconds_to_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return round(seconds * 1000)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
