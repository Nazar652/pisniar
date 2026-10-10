"""The 720x1280 filtergraph and the assertions that make its output trustworthy. `in_range=pc:out_range=tv` plus
`-color_range tv` is required: Spotify covers are full-range JPEG, and a plain `format=yuv420p` yields `yuvj420p`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from pisniar.logging import get_logger
from pisniar.worker.ffmpeg import FFMPEG_BINARY, MediaProbe, run_media_tool_checked, verify_media

logger = get_logger(__name__)

VIDEO_WIDTH = 720
VIDEO_HEIGHT = 1280
# the cover sits high, not centred: the band below holds the lyrics, so a long
# line overflows into black, not onto a face. `ass.TEXT_BAND_TOP_PX` builds on it.
COVER_TOP_OFFSET_PX = 100
VIDEO_FPS = 30
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"
AUDIO_SAMPLE_RATE = "44100"
AUDIO_CHANNELS = "2"

# `api.telegram.org` refuses more. The worst realistic input renders to ~4.4 MB,
# so hitting this means a render defect, not a big input.
TELEGRAM_VIDEO_SIZE_LIMIT_BYTES = 50 * 1024 * 1024
RENDER_DURATION_TOLERANCE_MS = 500

COVER_DOWNLOAD_TIMEOUT_SECONDS = 30.0
COVER_MAX_BYTES = 8 * 1024 * 1024
COVER_FILE_NAME = "cover.img"

# telegram caps a custom `thumbnail` at 320 px per edge. Cut from the rendered
# file, so it shows the padded frame subscribers see, not the raw square cover.
THUMBNAIL_MAX_EDGE = 320
THUMBNAIL_TIMEOUT_SECONDS = 30.0

# bumped whenever the filtergraph or the encoder settings change: it is an
# input to `params_hash`, so an old artifact stops being reused.
RENDERER_VERSION = "4B.1"


class RenderError(RuntimeError):
    """base for every render failure — loud, never isolated."""


class MissingCoverError(RenderError):
    """no cover to put in the frame, and the cover is the frame."""


class RenderTooLargeError(RenderError):
    """over the Telegram ceiling even after the one lower-bitrate attempt."""


@dataclass(frozen=True)
class EncoderQuality:
    crf: str
    maxrate: str
    bufsize: str
    preset: str


DEFAULT_QUALITY = EncoderQuality(crf="23", maxrate="2000k", bufsize="4000k", preset="veryfast")
# the single retry before the post fails; an oversized file is never sent.
REDUCED_QUALITY = EncoderQuality(crf="30", maxrate="1200k", bufsize="2400k", preset="veryfast")


@dataclass(frozen=True)
class RenderParameters:
    audio_path: Path
    cover_path: Path
    fragment_start_ms: int
    fragment_end_ms: int
    # passed in from outside, never decided here: subtitles stay an optional layer over the same filtergraph.
    subtitles_path: Path | None = None

    @property
    def length_ms(self) -> int:
        return self.fragment_end_ms - self.fragment_start_ms


def compute_params_hash(
    *,
    video_id: str,
    audio_duration_ms: int,
    cover_url: str,
    fragment_start_ms: int,
    fragment_end_ms: int,
    text_version: str | None = None,
    quality: EncoderQuality = DEFAULT_QUALITY,
) -> str:
    """must cover every input the frames depend on, or a stale render is reused."""
    payload = {
        "renderer_version": RENDERER_VERSION,
        "video_id": video_id,
        "audio_duration_ms": audio_duration_ms,
        "cover_url": cover_url,
        "fragment_start_ms": fragment_start_ms,
        "fragment_end_ms": fragment_end_ms,
        "text_version": text_version,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "fps": VIDEO_FPS,
        "video_codec": VIDEO_CODEC,
        "audio_codec": AUDIO_CODEC,
        "audio_bitrate": AUDIO_BITRATE,
        "crf": quality.crf,
        "maxrate": quality.maxrate,
        "bufsize": quality.bufsize,
        "preset": quality.preset,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


TEXT_VERSION_LENGTH = 16


def text_version_of(subtitles_text: str | None) -> str | None:
    """a digest of the `.ass` file itself, so a change to the lines, their timing,
    the fragment offset, the wrapping or the styles all change the hash."""
    if subtitles_text is None:
        return None
    return hashlib.sha256(subtitles_text.encode("utf-8")).hexdigest()[:TEXT_VERSION_LENGTH]


def build_filter_complex(subtitles_path: Path | None = None) -> str:
    """`in_range=pc:out_range=tv` handles full-range covers; `setsar=1` gives
    square pixels, so players do not stretch the frame."""
    steps = [
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease"
        f":flags=lanczos:in_range=pc:out_range=tv",
        "format=yuv420p",
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:{COVER_TOP_OFFSET_PX}:color=black",
        "setsar=1",
    ]
    if subtitles_path is not None:
        steps.append(f"subtitles={escape_filter_path(subtitles_path)}")
    return "[0:v]" + ",".join(steps) + "[v]"


def build_ffmpeg_arguments(
    parameters: RenderParameters,
    output_path: Path,
    *,
    quality: EncoderQuality = DEFAULT_QUALITY,
) -> list[str]:
    """`-f mp4` is explicit because the output is written under a `.partial`
    name, which no muxer would recognize from the extension."""
    return [
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(VIDEO_FPS),
        "-i",
        str(parameters.cover_path),
        "-ss",
        format_seconds(parameters.fragment_start_ms),
        "-i",
        str(parameters.audio_path),
        "-filter_complex",
        build_filter_complex(parameters.subtitles_path),
        "-map",
        "[v]",
        "-map",
        "1:a",
        "-t",
        format_seconds(parameters.length_ms),
        "-c:v",
        VIDEO_CODEC,
        "-preset",
        quality.preset,
        "-profile:v",
        "high",
        "-level",
        "4.0",
        "-crf",
        quality.crf,
        "-maxrate",
        quality.maxrate,
        "-bufsize",
        quality.bufsize,
        "-r",
        str(VIDEO_FPS),
        "-g",
        str(VIDEO_FPS * 2),
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-c:a",
        AUDIO_CODEC,
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        AUDIO_SAMPLE_RATE,
        "-ac",
        AUDIO_CHANNELS,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]


def render_fragment(
    parameters: RenderParameters,
    output_path: Path,
    *,
    timeout_seconds: float,
    quality: EncoderQuality = DEFAULT_QUALITY,
) -> MediaProbe:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "render_started",
        fragment_start_ms=parameters.fragment_start_ms,
        fragment_end_ms=parameters.fragment_end_ms,
        crf=quality.crf,
        maxrate=quality.maxrate,
    )
    run_media_tool_checked(
        FFMPEG_BINARY,
        build_ffmpeg_arguments(parameters, output_path, quality=quality),
        timeout_seconds=timeout_seconds,
    )

    probe = verify_media(
        output_path,
        require_video=True,
        require_audio=True,
        expected_duration_ms=parameters.length_ms,
        duration_tolerance_ms=RENDER_DURATION_TOLERANCE_MS,
    )

    logger.info(
        "render_finished",
        size_bytes=probe.size_bytes,
        duration_ms=probe.duration_ms,
        pix_fmt=probe.video_streams[0].pix_fmt,
        color_range=probe.video_streams[0].color_range,
    )
    return probe


def render_fragment_within_size_limit(
    parameters: RenderParameters,
    output_path: Path,
    *,
    timeout_seconds: float,
    size_limit_bytes: int = TELEGRAM_VIDEO_SIZE_LIMIT_BYTES,
) -> tuple[MediaProbe, EncoderQuality]:
    """one lower-bitrate retry, then the post fails: an oversized render is never sent."""
    probe = render_fragment(parameters, output_path, timeout_seconds=timeout_seconds)
    if probe.size_bytes <= size_limit_bytes:
        return probe, DEFAULT_QUALITY

    logger.warning(
        "render_over_size_limit",
        size_bytes=probe.size_bytes,
        size_limit_bytes=size_limit_bytes,
        attempt="default",
    )
    probe = render_fragment(parameters, output_path, timeout_seconds=timeout_seconds, quality=REDUCED_QUALITY)
    if probe.size_bytes <= size_limit_bytes:
        return probe, REDUCED_QUALITY

    raise RenderTooLargeError(
        f"рендер важить {probe.size_bytes} байт навіть після зниження бітрейта, "
        f"ліміт {size_limit_bytes} — це дефект рендера, а не нормальний вхід"
    )


def build_thumbnail_arguments(video_path: Path, output_path: Path) -> list[str]:
    return [
        "-y",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={THUMBNAIL_MAX_EDGE}:{THUMBNAIL_MAX_EDGE}:force_original_aspect_ratio=decrease",
        "-f",
        "image2",
        str(output_path),
    ]


def render_thumbnail(
    video_path: Path,
    output_path: Path,
    *,
    timeout_seconds: float = THUMBNAIL_TIMEOUT_SECONDS,
) -> Path | None:
    """cosmetic, so it returns `None` instead of raising: Telegram builds its own
    preview, and that is no reason to fail a post with a verified video."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_media_tool_checked(
            FFMPEG_BINARY,
            build_thumbnail_arguments(video_path, output_path),
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        logger.warning("render_thumbnail_failed", video_path=str(video_path), exc_info=True)
        return None

    if not output_path.is_file() or output_path.stat().st_size <= 0:
        logger.warning("render_thumbnail_empty", video_path=str(video_path))
        return None

    logger.info(
        "render_thumbnail_finished",
        path=str(output_path),
        size_bytes=output_path.stat().st_size,
    )
    return output_path


async def download_cover(
    http_client: httpx.AsyncClient,
    cover_url: str | None,
    destination_directory: Path,
) -> Path:
    """product-critical, so not wrapped in `isolated_source_call`: without the
    cover there is no frame, and the post has no video to show."""
    if not cover_url:
        raise MissingCoverError("у треку немає обкладинки — без неї відео не зібрати")

    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / COVER_FILE_NAME

    response = await http_client.get(cover_url, timeout=COVER_DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()

    content = response.content
    if not content:
        raise MissingCoverError(f"обкладинка завантажилась порожньою: {cover_url}")
    if len(content) > COVER_MAX_BYTES:
        raise MissingCoverError(f"обкладинка важить {len(content)} байт, ліміт {COVER_MAX_BYTES}: {cover_url}")

    destination.write_bytes(content)
    logger.info("render_cover_downloaded", size_bytes=len(content), path=str(destination))
    return destination


def format_seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def escape_filter_path(path: Path) -> str:
    """`:` and `\\` end a filter option unless escaped. POSIX form: the worker
    container is Linux whatever host the tests run on."""
    return path.as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
