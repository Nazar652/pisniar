"""The turntable smoke test: a real ffmpeg render of 3 s from generated inputs, read back through ffprobe and
raw frames. Runs only in the worker image, where ffmpeg lives (`make test-media`); the layout and the filtergraph
are pinned by unit tests, so this is what proves the pixels. No network: the audio and the cover are made by ffmpeg."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from pisniar.worker import ass, ffmpeg, layout, render, turntable

pytestmark = [pytest.mark.integration, pytest.mark.media]

FRAGMENT_START_MS = 1_000
FRAGMENT_END_MS = 4_000
LENGTH_MS = FRAGMENT_END_MS - FRAGMENT_START_MS
EMPTY_DISPLAY_MS = 1_000
TOOL_TIMEOUT_SECONDS = 120.0
QUIET = ["-nostdin", "-hide_banner", "-loglevel", "error", "-y"]

# `render.RENDERER_VERSION` and the sha256 of the five drawn layers, which `tobytes()` makes exact for one
# image and one Pillow: a drawing change needs a new version and a new digest here, in that order.
PINNED_RENDERER_VERSION = "turntable-1"
PINNED_LAYERS_DIGEST = "73b12a595326b96ccd7729deefbfa79316206647f1b29a46ed3ebc61c41d2f05"

# lyric lines in absolute track time: the note, a Cyrillic line and one more, so every slot is exercised.
LYRIC_LINES = (
    ass.LyricLine(line_index=0, start_ms=1_000, text="♪"),
    ass.LyricLine(line_index=1, start_ms=2_000, text="Привіт, світе"),
    ass.LyricLine(line_index=2, start_ms=3_000, text="Друга стрічка"),
)
# U+E000 is private use: no installed font has it, so libass draws the font's missing-glyph box.
MISSING_CODEPOINT = "\ue000"

INK_LUMA = 120
CURRENT_SLOT_BAND = (layout.SCREEN_BOX_PX[0], ass.CURRENT_SLOT_Y - 35, layout.SCREEN_BOX_PX[2], ass.CURRENT_SLOT_Y + 35)
NOTE_MIN_INK_PX = 15
LYRIC_MIN_INK_PX = 400
TOFU_MAX_OVERLAP = 0.5
STATIC_TOLERANCE = 12
GLASS_TOLERANCE = 10
AMBER_MIN_PX = 30
EMPTY_DISPLAY_MAX_AMBER_SHARE = 0.1

MID_Y = layout.FRAME_HEIGHT_PX // 2
# inside the paper label, clear of the spindle hub.
LABEL_SAMPLE_RADIUS_PX = layout.LABEL_DIAMETER_PX // 3
# inside the groove band, where the smoke blotches make every angle differ.
GROOVE_SAMPLE_RADIUS_PX = layout.VINYL_RADIUS_PX - 70
# the lower glass is flat; the upper left carries the diagonal highlight.
GLASS_POINT = (200, layout.SCREEN_BOX_PX[3] - 33)
BODY_LEFT_X = layout.BODY_BOX_PX[0] + 15
BODY_RIGHT_X = layout.BODY_BOX_PX[2] - 20
WOOD_POINT = (layout.BODY_BOX_PX[0] - 15, MID_Y)
BODY_POINT = (BODY_LEFT_X, MID_Y)
STATIC_HEIGHTS = (layout.VINYL_CENTER_PX[1], MID_Y, GLASS_POINT[1])
STATIC_POINTS = [
    *((x, y) for x in (BODY_LEFT_X, BODY_RIGHT_X) for y in STATIC_HEIGHTS),
    GLASS_POINT,
    (layout.FRAME_CENTER_X_PX + 140, GLASS_POINT[1]),
]
FRAME_BYTES = layout.FRAME_WIDTH_PX * layout.FRAME_HEIGHT_PX * 3

Frame = bytes
Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class Rendered:
    video: Path
    plain_video: Path
    empty_display_video: Path
    directory: Path
    cover: Path
    layers: turntable.TurntableLayers


def run_ffmpeg(arguments: list[str]) -> None:
    ffmpeg.run_media_tool_checked(ffmpeg.FFMPEG_BINARY, [*QUIET, *arguments], timeout_seconds=TOOL_TIMEOUT_SECONDS)


def frame_at(video: Path, seconds: float) -> Frame:
    completed = ffmpeg.run_media_tool_bytes(
        ffmpeg.FFMPEG_BINARY,
        [
            *QUIET,
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        timeout_seconds=TOOL_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0
    assert len(completed.stdout) == FRAME_BYTES
    return completed.stdout


def pixel(frame: Frame, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * layout.FRAME_WIDTH_PX + x) * 3
    return frame[offset], frame[offset + 1], frame[offset + 2]


def luma(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def ink_mask(frame: Frame, box: Box) -> set[tuple[int, int]]:
    left, top, right, bottom = box
    return {(x, y) for y in range(top, bottom) for x in range(left, right) if luma(pixel(frame, x, y)) > INK_LUMA}


def ring(centre: tuple[int, int], radius: int, count: int) -> list[tuple[int, int]]:
    return [
        (
            round(centre[0] + radius * math.cos(2 * math.pi * index / count)),
            round(centre[1] + radius * math.sin(2 * math.pi * index / count)),
        )
        for index in range(count)
    ]


def changed_points(first: Frame, second: Frame, points: list[tuple[int, int]], threshold: int) -> int:
    return sum(
        1
        for x, y in points
        if sum(abs(a - b) for a, b in zip(pixel(first, x, y), pixel(second, x, y), strict=True)) > threshold
    )


def amber_count(frame: Frame) -> int:
    left, top, right, bottom = layout.DISPLAY_BOX_PX
    return sum(
        1
        for y in range(top, bottom)
        for x in range(left, right)
        if pixel(frame, x, y)[0] > 180 and pixel(frame, x, y)[1] > 120
    )


def probe_json(video: Path) -> dict[str, object]:
    completed = ffmpeg.run_media_tool_checked(
        "ffprobe",
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(video),
        ],
        timeout_seconds=TOOL_TIMEOUT_SECONDS,
    )
    return json.loads(completed.stdout)


def top_level_boxes(video: Path) -> list[str]:
    data = video.read_bytes()
    names: list[str] = []
    offset = 0
    while offset + 8 <= len(data):
        size, name = struct.unpack(">I4s", data[offset : offset + 8])
        names.append(name.decode("latin-1"))
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        if size == 0:
            break
        offset += size
    return names


def layers_digest() -> str:
    """mode, size and raw bytes of each layer, in a fixed order."""
    overlay, offset = turntable.build_overlay()
    images = [
        turntable.build_background(),
        turntable.build_vinyl(),
        turntable.build_label_mask(),
        turntable.build_hub(),
        overlay,
    ]
    digest = hashlib.sha256(json.dumps(offset).encode())
    for image in images:
        digest.update(f"{image.mode}:{image.size}".encode())
        digest.update(image.tobytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> Rendered:
    directory = tmp_path_factory.mktemp("turntable")
    audio = directory / "audio.wav"
    cover = directory / render.COVER_FILE_NAME
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=44100", "-ac", "2", str(audio)])
    # a flat full-range red JPEG under the production name, as a Spotify cover that breaks a naive `yuv420p`.
    jpeg = directory / "cover.jpg"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=640x640:d=1", "-frames:v", "1", "-pix_fmt", "yuvj420p", str(jpeg)])
    jpeg.rename(cover)

    lyrics = ass.build_ass_subtitles(LYRIC_LINES, fragment_start_ms=FRAGMENT_START_MS, fragment_end_ms=FRAGMENT_END_MS)
    assert lyrics is not None
    lyrics_path = directory / "lyrics.ass"
    lyrics_path.write_text(lyrics, encoding="utf-8")
    display_path = directory / "display.ass"
    display_path.write_text(ass.build_display_subtitles("Назва пісні", ["Артист"], LENGTH_MS), encoding="utf-8")
    empty_display_path = directory / "empty-display.ass"
    empty_display_path.write_text(ass.build_display_subtitles("", [], EMPTY_DISPLAY_MS), encoding="utf-8")

    layers = turntable.write_layers(directory)

    def render_to(
        name: str, subtitles_path: Path | None, *, display: Path = display_path, length_ms: int = LENGTH_MS
    ) -> Path:
        output = directory / name
        render.render_fragment(
            render.RenderParameters(
                audio_path=audio,
                cover_path=cover,
                fragment_start_ms=FRAGMENT_START_MS,
                fragment_end_ms=FRAGMENT_START_MS + length_ms,
                display_subtitles_path=display,
                subtitles_path=subtitles_path,
            ),
            layers,
            output,
            timeout_seconds=TOOL_TIMEOUT_SECONDS,
        )
        return output

    return Rendered(
        video=render_to("with-text.mp4", lyrics_path),
        plain_video=render_to("without-text.mp4", None),
        empty_display_video=render_to(
            "empty-display.mp4", None, display=empty_display_path, length_ms=EMPTY_DISPLAY_MS
        ),
        directory=directory,
        cover=cover,
        layers=layers,
    )


def test_the_file_has_the_promised_streams_range_rate_and_faststart(rendered: Rendered) -> None:
    probe = ffmpeg.verify_media(
        rendered.video,
        require_video=True,
        require_audio=True,
        expected_duration_ms=LENGTH_MS,
        duration_tolerance_ms=render.RENDER_DURATION_TOLERANCE_MS,
    )
    video, audio = probe.video_streams[0], probe.audio_streams[0]

    assert probe.size_bytes > 0
    assert (video.codec_name, video.width, video.height) == ("h264", 720, 1280)
    assert (video.pix_fmt, video.color_range, audio.codec_name) == ("yuv420p", "tv", "aac")
    stream = probe_json(rendered.video)["streams"]
    assert isinstance(stream, list)
    assert stream[0]["avg_frame_rate"] == f"{render.VIDEO_FPS}/1"
    boxes = top_level_boxes(rendered.video)
    assert boxes.index("moov") < boxes.index("mdat")


def test_the_thumbnail_is_cut_from_the_rendered_frame(rendered: Rendered) -> None:
    thumbnail = render.render_thumbnail(rendered.video, rendered.directory / "thumb.jpg")

    assert thumbnail is not None
    assert thumbnail.stat().st_size > 0
    probe = ffmpeg.probe_media(thumbnail)
    width = probe.video_streams[0].width
    assert width is not None
    assert 0 < width <= render.THUMBNAIL_MAX_EDGE


def test_the_layers_stand_where_the_layout_says(rendered: Rendered) -> None:
    frame = frame_at(rendered.video, 0.5)
    needle_x, needle_y = layout.NEEDLE_POINT_PX
    tolerance = layout.NEEDLE_TOLERANCE_PX + 1
    needle = max(
        luma(pixel(frame, x, y))
        for y in range(needle_y - tolerance, needle_y + tolerance + 1)
        for x in range(needle_x - tolerance, needle_x + tolerance + 1)
    )

    for x, y in ring(layout.VINYL_CENTER_PX, LABEL_SAMPLE_RADIUS_PX, 8):
        red, green, blue = pixel(frame, x, y)
        assert red > 200 and green < 70 and blue < 70, (x, y)
    glass = pixel(frame, *GLASS_POINT)
    assert all(abs(a - b) <= GLASS_TOLERANCE for a, b in zip(glass, layout.GLASS_RGB, strict=True)), glass
    red, green, blue = pixel(frame, *WOOD_POINT)
    assert red > green > blue
    assert luma(pixel(frame, *BODY_POINT)) < 80
    assert needle >= 150
    assert amber_count(frame) >= AMBER_MIN_PX


def test_the_record_turns_while_the_body_and_the_glass_stand_still(rendered: Rendered) -> None:
    start, later = frame_at(rendered.video, 0.0), frame_at(rendered.video, 1.0)
    groove = ring(layout.VINYL_CENTER_PX, GROOVE_SAMPLE_RADIUS_PX, 72)

    assert changed_points(start, later, groove, STATIC_TOLERANCE) >= 10
    assert changed_points(start, later, STATIC_POINTS, STATIC_TOLERANCE) == 0


def test_the_note_is_drawn_as_a_glyph_and_not_as_a_missing_glyph_box(rendered: Rendered, tmp_path: Path) -> None:
    """the same slot is rendered on plain glass with a codepoint no font has, which libass draws as a hollow
    box; a real note has ink and a mask that mostly differs from that box's, an empty slot has no ink."""
    note = ink_mask(frame_at(rendered.video, 0.5), CURRENT_SLOT_BAND)

    reference_ass = ass.build_ass_subtitles(
        [ass.LyricLine(line_index=0, start_ms=0, text=MISSING_CODEPOINT)], fragment_start_ms=0, fragment_end_ms=1_000
    )
    assert reference_ass is not None
    (tmp_path / "tofu.ass").write_text(reference_ass, encoding="utf-8")
    glass = "color=c=0x05080B:s=720x1280:r=30:d=1"
    subtitles = f"subtitles={render.escape_filter_path(tmp_path / 'tofu.ass')}"
    run_ffmpeg(["-f", "lavfi", "-i", glass, "-vf", subtitles, "-frames:v", "1", str(tmp_path / "tofu.png")])
    tofu = ink_mask(frame_of_image(tmp_path / "tofu.png"), CURRENT_SLOT_BAND)

    assert len(tofu) > 0
    assert len(note) >= NOTE_MIN_INK_PX
    assert len(note & tofu) / len(note | tofu) < TOFU_MAX_OVERLAP
    xs = [x for x, _ in note]
    assert abs((min(xs) + max(xs)) / 2 - layout.FRAME_CENTER_X_PX) <= 30


def frame_of_image(path: Path) -> Frame:
    completed = ffmpeg.run_media_tool_bytes(
        ffmpeg.FFMPEG_BINARY,
        [*QUIET, "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        timeout_seconds=TOOL_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0
    assert len(completed.stdout) == FRAME_BYTES
    return completed.stdout


def test_the_cyrillic_line_is_drawn_in_the_current_slot(rendered: Rendered) -> None:
    ink = ink_mask(frame_at(rendered.video, 1.5), CURRENT_SLOT_BAND)
    xs = [x for x, _ in ink]

    assert len(ink) >= LYRIC_MIN_INK_PX
    assert max(xs) - min(xs) > 100
    assert abs((min(xs) + max(xs)) / 2 - layout.FRAME_CENTER_X_PX) <= 30


def test_a_render_without_song_text_leaves_the_glass_empty_but_keeps_the_display(rendered: Rendered) -> None:
    frame = frame_at(rendered.plain_video, 1.5)
    left_edge, top_edge, right_edge, bottom_edge = layout.SCREEN_BOX_PX

    assert len(ink_mask(frame, (left_edge + 10, top_edge + 10, right_edge - 10, bottom_edge - 10))) == 0
    assert amber_count(frame) >= AMBER_MIN_PX


def test_an_empty_title_and_artists_leave_the_display_without_amber(rendered: Rendered) -> None:
    """the negative control of the amber check: it must see the text, not the display's own colours."""
    normal = amber_count(frame_at(rendered.video, 0.5))
    empty = amber_count(frame_at(rendered.empty_display_video, 0.5))

    assert empty <= normal * EMPTY_DISPLAY_MAX_AMBER_SHARE


def test_the_layers_and_the_renderer_version_are_pinned_together() -> None:
    assert render.RENDERER_VERSION == PINNED_RENDERER_VERSION
    assert layers_digest() == PINNED_LAYERS_DIGEST


def test_the_pin_follows_a_drawn_pixel(monkeypatch: pytest.MonkeyPatch) -> None:
    before = layers_digest()
    original = turntable.build_label_mask

    def altered() -> Image.Image:
        image = original()
        image.putpixel((0, 0), 255)
        return image

    monkeypatch.setattr(turntable, "build_label_mask", altered)

    assert layers_digest() != before
