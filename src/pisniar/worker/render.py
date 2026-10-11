"""The 720x1280 turntable filtergraph and the assertions that make its output trustworthy.
The cover is read full range (`in_range=pc`), every YUV conversion is `out_range=tv`, and
`-color_range tv` is set: a plain `format=yuv420p` on a full-range JPEG yields `yuvj420p`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from pisniar.logging import get_logger
from pisniar.worker import layout, turntable
from pisniar.worker.ffmpeg import FFMPEG_BINARY, MediaProbe, run_media_tool_checked, verify_media

logger = get_logger(__name__)

VIDEO_WIDTH = layout.FRAME_WIDTH_PX
VIDEO_HEIGHT = layout.FRAME_HEIGHT_PX
VIDEO_FPS = 30
# 33 1/3 rpm, clockwise: the angle is `2*PI*t*100/180` radians at t seconds.
DISC_REVOLUTIONS_PER_SECOND = "100/180"
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
# file, so it shows the turntable frame subscribers see, not the raw square cover.
THUMBNAIL_MAX_EDGE = 320
THUMBNAIL_TIMEOUT_SECONDS = 30.0

# bumped with any change to the graph, encoder, `layout` or `turntable` drawing; it is an
# input to `params_hash`, and `test_video_render.py` pins it to a digest of graph and layout.
RENDERER_VERSION = "turntable-1"


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
    # the title and artists: the display is always drawn, even for a song without text.
    display_subtitles_path: Path
    # passed in from outside, never decided here: the song text stays an optional layer over the same filtergraph.
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
    display_version: str,
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
        "display_version": display_version,
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


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:TEXT_VERSION_LENGTH]


def text_version_of(subtitles_text: str | None) -> str | None:
    """a digest of the `.ass` file itself, so a change to the lines, their timing,
    the fragment offset, the wrapping or the styles all change the hash."""
    if subtitles_text is None:
        return None
    return text_digest(subtitles_text)


def build_filter_complex(
    layers: turntable.TurntableLayers,
    display_subtitles_path: Path,
    subtitles_path: Path | None = None,
) -> str:
    """the still floor is converted to YUV once with the static display drawn on it; only
    `disc_square`, everything that moves, is composed in `gbrp` and converted per frame."""
    label = layout.LABEL_DIAMETER_PX
    disc = layout.VINYL_DIAMETER_PX
    square_x, square_y, square_width, square_height = disc_square(layers)
    still = f"loop=loop=-1:size=1,setpts=N/{VIDEO_FPS}/TB"
    angle = f"2*PI*t*{DISC_REVOLUTIONS_PER_SECOND}"
    display = f"subtitles={escape_filter_path(display_subtitles_path)}"
    lyrics = [] if subtitles_path is None else [f"subtitles={escape_filter_path(subtitles_path)}"]
    chains = [
        f"[0:v]scale={label}:{label}:force_original_aspect_ratio=increase:flags=lanczos"
        f":in_range=pc,crop={label}:{label},format=rgba[cover]",
        "[1:v]format=gray[mask]",
        "[cover][mask]alphamerge[label]",
        "[2:v]format=rgba[vinyl]",
        "[3:v]format=rgba[hub]",
        "[vinyl][label]overlay=(W-w)/2:(H-h)/2:format=auto[seated]",
        f"[seated][hub]overlay=0:0:format=auto,{still},rotate=a={angle}:c=none:ow={disc}:oh={disc}[spin]",
        "[4:v]split[floor_source][square_source]",
        f"[floor_source]scale=out_range=tv,format=yuv420p,{display},{still}[floor]",
        f"[square_source]crop={square_width}:{square_height}:{square_x}:{square_y},format=gbrp,{still}[ground]",
        f"[ground][spin]overlay={layout.VINYL_OVERLAY_X_PX - square_x}"
        f":{layout.VINYL_OVERLAY_Y_PX - square_y}:format=gbrp[disc]",
        "[5:v]format=rgba[top]",
        f"[disc][top]overlay={layers.overlay_x - square_x}:{layers.overlay_y - square_y}:format=gbrp[square]",
        "[square]scale=out_range=tv,format=yuv420p[square_yuv]",
        "[floor][square_yuv]"
        f"overlay={square_x}:{square_y}:format=yuv420,setsar=1" + "".join(f",{item}" for item in lyrics) + "[v]",
    ]
    return ";".join(chains)


def disc_square(layers: turntable.TurntableLayers) -> tuple[int, int, int, int]:
    """(left, top, width, height) of the area the turning disc and the overlay cover; every
    number is even, so the chroma samples of the square line up with those of the frame."""
    left = min(layout.VINYL_OVERLAY_X_PX, layers.overlay_x)
    top = min(layout.VINYL_OVERLAY_Y_PX, layers.overlay_y)
    right = max(
        layout.VINYL_OVERLAY_X_PX + layout.VINYL_DIAMETER_PX,
        layers.overlay_x + layers.overlay_width,
    )
    bottom = max(
        layout.VINYL_OVERLAY_Y_PX + layout.VINYL_DIAMETER_PX,
        layers.overlay_y + layers.overlay_height,
    )
    return left, top, right - left, bottom - top


def build_ffmpeg_arguments(
    parameters: RenderParameters,
    layers: turntable.TurntableLayers,
    output_path: Path,
    *,
    quality: EncoderQuality = DEFAULT_QUALITY,
) -> list[str]:
    """`-f mp4` is explicit: the `.partial` name hides the format. The stills are single
    frames that `loop` repeats, so no input carries `-loop 1`."""
    still_inputs = [
        parameters.cover_path,
        layers.label_mask,
        layers.vinyl,
        layers.hub,
        layers.background,
        layers.overlay,
    ]
    return [
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *(argument for still in still_inputs for argument in ("-i", str(still))),
        "-ss",
        format_seconds(parameters.fragment_start_ms),
        "-i",
        str(parameters.audio_path),
        "-filter_complex",
        build_filter_complex(layers, parameters.display_subtitles_path, parameters.subtitles_path),
        "-map",
        "[v]",
        "-map",
        "6:a",
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
    layers: turntable.TurntableLayers,
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
        build_ffmpeg_arguments(parameters, layers, output_path, quality=quality),
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
    """one lower-bitrate retry, then the post fails: an oversized render is never sent.
    The layers are drawn once, beside the cover, and serve both attempts."""
    drawing_started = time.perf_counter()
    layers = turntable.write_layers(parameters.cover_path.parent)
    logger.info("render_layers_drawn", seconds=round(time.perf_counter() - drawing_started, 2))
    probe = render_fragment(parameters, layers, output_path, timeout_seconds=timeout_seconds)
    if probe.size_bytes <= size_limit_bytes:
        return probe, DEFAULT_QUALITY

    logger.warning(
        "render_over_size_limit",
        size_bytes=probe.size_bytes,
        size_limit_bytes=size_limit_bytes,
        attempt="default",
    )
    probe = render_fragment(parameters, layers, output_path, timeout_seconds=timeout_seconds, quality=REDUCED_QUALITY)
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
