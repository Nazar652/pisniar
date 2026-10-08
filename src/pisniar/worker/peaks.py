"""Waveform peaks of a downloaded track: ffmpeg decodes mono 8 kHz PCM, 20 buckets
a second keep their loudest sample, scaled to 0-255. A derived file, never required.
"""

from __future__ import annotations

import json
from array import array
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from pisniar.logging import get_logger
from pisniar.worker import storage as media_storage
from pisniar.worker.ffmpeg import (
    FFMPEG_BINARY,
    MediaToolError,
    MediaToolFailedError,
    run_media_tool_bytes,
    stderr_tail,
)

logger = get_logger(__name__)

FORMAT_VERSION = 1
PEAKS_PER_SECOND = 20
SAMPLE_RATE_HZ = 8000
BUCKET_SAMPLES = SAMPLE_RATE_HZ // PEAKS_PER_SECOND
PEAK_SCALE = 255

# one hour of audio at 20 values a second; the reader refuses anything longer.
MAX_PEAK_VALUES = 72_000
MAX_PEAKS_FILE_BYTES = 1_000_000
PEAKS_TIMEOUT_SECONDS = 30


def decode_pcm(audio_path: Path) -> bytes:
    completed = run_media_tool_bytes(
        FFMPEG_BINARY,
        [
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE_HZ),
            "-f",
            "s16le",
            "pipe:1",
        ],
        timeout_seconds=PEAKS_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise MediaToolFailedError(
            FFMPEG_BINARY,
            completed.returncode,
            stderr_tail(completed.stderr.decode("utf-8", errors="replace")),
        )
    return completed.stdout


def peak_values(pcm: bytes) -> list[int]:
    """the loudest sample of each bucket, scaled to the loudest bucket; silence is all zeros."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // samples.itemsize * samples.itemsize])
    if not samples:
        raise MediaToolError("ffmpeg не віддав жодного семпла")

    peaks: list[int] = []
    for start in range(0, len(samples), BUCKET_SAMPLES):
        bucket = samples[start : start + BUCKET_SAMPLES]
        peaks.append(max(max(bucket), -min(bucket)))

    loudest = max(peaks)
    if loudest == 0:
        return [0] * len(peaks)
    return [round(peak * PEAK_SCALE / loudest) for peak in peaks]


def peaks_document(values: list[int]) -> str:
    return json.dumps(
        {"format_version": FORMAT_VERSION, "peaks_per_second": PEAKS_PER_SECOND, "values": values},
        separators=(",", ":"),
    )


def read_peaks(path: Path) -> list[int] | None:
    """`None` with a warning for a missing, oversized or malformed file; pure, no ffmpeg."""
    try:
        if path.stat().st_size > MAX_PEAKS_FILE_BYTES:
            raise ValueError("файл peaks завеликий")
        document = json.loads(path.read_text(encoding="utf-8"))
        return validated_values(document)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("audio_peaks_unreadable", path=str(path), exc_info=True)
        return None


def validated_values(document: object) -> list[int]:
    if not isinstance(document, dict):
        raise ValueError("peaks не є об'єктом")
    if document.get("format_version") != FORMAT_VERSION:
        raise ValueError("інша версія формату peaks")
    if document.get("peaks_per_second") != PEAKS_PER_SECOND:
        raise ValueError("інша частота peaks")

    values = document.get("values")
    if not isinstance(values, list) or not 0 < len(values) <= MAX_PEAK_VALUES:
        raise ValueError("кількість значень peaks поза межами")

    checked: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PEAK_SCALE:
            raise ValueError("значення peaks поза діапазоном 0-255")
        checked.append(value)
    return checked


def ensure_peaks(
    media_root: Path, track_id: int, video_id: str, audio_path: Path, temporary_directory: Path
) -> Path | None:
    """writes the peaks file unless a valid one exists; a failure is a warning and `None`."""
    final_path = media_storage.peaks_file_path(media_root, track_id, video_id)
    if read_peaks(final_path) is not None:
        return final_path

    try:
        values = peak_values(decode_pcm(audio_path))
        if len(values) > MAX_PEAK_VALUES:
            raise ValueError(
                f"peaks: {len(values)} значень, читач приймає не більше {MAX_PEAK_VALUES}"
            )

        temporary_directory.mkdir(parents=True, exist_ok=True)
        staging = media_storage.staging_path(temporary_directory, final_path)
        staging.write_text(peaks_document(values), encoding="utf-8")
        media_storage.publish_atomically(staging, final_path)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning("audio_peaks_failed", track_id=track_id, video_id=video_id, exc_info=True)
        return None

    return final_path
