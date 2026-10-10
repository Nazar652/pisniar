"""Waveform of a downloaded track: ffmpeg decodes mono 8 kHz PCM, 20 buckets a second hold
the RMS level smoothed over about a second, scaled to 0-255. A derived file, never required;
`python -m pisniar.worker.peaks` writes it for audio already on the volume.
"""

from __future__ import annotations

import math
import time
from array import array
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from pisniar.config import get_settings, redaction_secrets
from pisniar.logging import configure_logging, get_logger
from pisniar.worker import storage as media_storage
from pisniar.worker.ffmpeg import (
    FFMPEG_BINARY,
    MediaToolError,
    MediaToolFailedError,
    run_media_tool_bytes,
    stderr_tail,
)
from pisniar.worker.peaks_format import (
    MAX_PEAK_VALUES,
    PEAK_SCALE,
    PEAKS_PER_SECOND,
    peaks_document,
    read_peaks,
)

logger = get_logger(__name__)

SAMPLE_RATE_HZ = 8000
BUCKET_SAMPLES = SAMPLE_RATE_HZ // PEAKS_PER_SECOND
PEAKS_TIMEOUT_SECONDS = 30
SMOOTHING_RADIUS_BUCKETS = 10
REFERENCE_PERCENTILE = 0.99
BACKFILL_DIRECTORY_NAME = "peaks-backfill"


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


def bucket_energies(samples: array[int]) -> list[int]:
    """the sum of squared samples of each bucket."""
    return [
        sum(sample * sample for sample in samples[start : start + BUCKET_SAMPLES])
        for start in range(0, len(samples), BUCKET_SAMPLES)
    ]


def windowed_powers(energies: list[int], sample_count: int) -> list[float]:
    """the mean square over the samples within the smoothing radius of each bucket."""
    prefix = [0, *accumulate(energies)]
    powers: list[float] = []
    for index in range(len(energies)):
        low = max(0, index - SMOOTHING_RADIUS_BUCKETS)
        high = min(len(energies), index + SMOOTHING_RADIUS_BUCKETS + 1)
        covered = min(high * BUCKET_SAMPLES, sample_count) - low * BUCKET_SAMPLES
        powers.append((prefix[high] - prefix[low]) / covered)
    return powers


def loudness_values(pcm: bytes) -> list[int]:
    """smoothed RMS per bucket, scaled to the 99th percentile and clipped; silence is all zeros."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // samples.itemsize * samples.itemsize])
    if not samples:
        raise MediaToolError("ffmpeg не віддав жодного семпла")

    levels = [math.sqrt(power) for power in windowed_powers(bucket_energies(samples), len(samples))]
    reference = sorted(levels)[min(len(levels) - 1, int(len(levels) * REFERENCE_PERCENTILE))]
    if reference == 0:
        return [0] * len(levels)
    return [min(PEAK_SCALE, round(level * PEAK_SCALE / reference)) for level in levels]


def ensure_peaks(
    media_root: Path, track_id: int, video_id: str, audio_path: Path, temporary_directory: Path
) -> Path | None:
    """writes the peaks file unless a valid one exists; a failure is a warning and `None`."""
    final_path = media_storage.peaks_file_path(media_root, track_id, video_id)
    if read_peaks(final_path) is not None:
        return final_path

    try:
        values = loudness_values(decode_pcm(audio_path))
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


@dataclass(frozen=True)
class BackfillSummary:
    written: int
    current: int
    failed: int


def backfill_peaks(media_root: Path) -> BackfillSummary:
    """writes the file for every stored audio whose peaks are missing or of another version."""
    temporary_directory = (
        media_root / media_storage.TEMPORARY_DIRECTORY_NAME / BACKFILL_DIRECTORY_NAME
    )
    written = current = failed = 0
    try:
        for track_id, video_id, audio_path in media_storage.stored_audio_files(media_root):
            final_path = media_storage.peaks_file_path(media_root, track_id, video_id)
            if read_peaks(final_path) is not None:
                current += 1
                logger.info("audio_peaks_current", track_id=track_id, video_id=video_id)
                continue

            started = time.monotonic()
            result = ensure_peaks(media_root, track_id, video_id, audio_path, temporary_directory)
            if result is None:
                failed += 1
                continue

            written += 1
            logger.info(
                "audio_peaks_backfilled",
                track_id=track_id,
                video_id=video_id,
                seconds=round(time.monotonic() - started, 2),
                size_bytes=result.stat().st_size,
            )
    finally:
        media_storage.remove_tree_quietly(temporary_directory)

    logger.info("audio_peaks_backfill_finished", written=written, current=current, failed=failed)
    return BackfillSummary(written=written, current=current, failed=failed)


def main() -> int:
    settings = get_settings()
    configure_logging(
        log_format=settings.log_format,
        log_level=settings.log_level,
        secrets=redaction_secrets(settings),
    )
    return 1 if backfill_peaks(settings.media_root).failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
