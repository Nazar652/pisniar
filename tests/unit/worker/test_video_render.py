"""The filtergraph, the required encoder flags, the size ceiling with its single
retry, and `params_hash`, the input that decides whether ffmpeg runs at all.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from pisniar.worker import ffmpeg as media_tools
from pisniar.worker import layout, turntable
from pisniar.worker import render as video_render

AUDIO_PATH = Path("/data/audio/1/n4bPzdIs_nc.opus")
COVER_PATH = Path("/data/tmp/7/cover.img")
OUTPUT_PATH = Path("/data/tmp/7/abc.mp4.partial")


DISPLAY_PATH = Path("/data/tmp/7/display.ass")
LYRICS_PATH = Path("/data/tmp/7/lyrics.ass")
LAYERS = turntable.TurntableLayers(
    background=Path("/data/tmp/7/bg.png"),
    vinyl=Path("/data/tmp/7/vinyl.png"),
    label_mask=Path("/data/tmp/7/label-mask.png"),
    hub=Path("/data/tmp/7/hub.png"),
    overlay=Path("/data/tmp/7/overlay.png"),
    overlay_x=170,
    overlay_y=92,
    overlay_width=504,
    overlay_height=370,
)


def parameters(subtitles_path: Path | None = None) -> video_render.RenderParameters:
    return video_render.RenderParameters(
        audio_path=AUDIO_PATH,
        cover_path=COVER_PATH,
        fragment_start_ms=0,
        fragment_end_ms=45_000,
        display_subtitles_path=DISPLAY_PATH,
        subtitles_path=subtitles_path,
    )


def graph_of(subtitles_path: Path | None = None) -> str:
    return video_render.build_filter_complex(LAYERS, DISPLAY_PATH, subtitles_path)


def test_the_cover_is_read_full_range_into_the_label_and_nowhere_else() -> None:
    """measured: the cover arrives from the Spotify CDN as full-range JPEG, and a
    naive `format=yuv420p` yields `yuvj420p` every time."""
    graph = graph_of()

    assert graph.count("in_range=pc") == 1
    assert graph.startswith(
        "[0:v]scale=190:190:force_original_aspect_ratio=increase:flags=lanczos:in_range=pc,"
        "crop=190:190,format=rgba[cover]"
    )
    assert "[cover][mask]alphamerge[label]" in graph


def test_every_pixel_is_converted_to_yuv_once_in_one_of_two_separate_areas() -> None:
    """the still floor once, the square the disc moves in on every frame; both with the
    tv range, and the square is laid back over the floor without a second conversion."""
    graph = graph_of()

    assert graph.count("out_range=tv") == 2
    assert "[floor_source]scale=out_range=tv,format=yuv420p," in graph
    assert "[square]scale=out_range=tv,format=yuv420p[square_yuv]" in graph
    assert "[floor][square_yuv]overlay=170:92:format=yuv420,setsar=1" in graph


def test_the_disc_square_is_the_even_box_around_the_disc_and_the_overlay() -> None:
    assert video_render.disc_square(LAYERS) == (170, 92, 504, 384)
    wider = turntable.TurntableLayers(
        background=LAYERS.background,
        vinyl=LAYERS.vinyl,
        label_mask=LAYERS.label_mask,
        hub=LAYERS.hub,
        overlay=LAYERS.overlay,
        overlay_x=50,
        overlay_y=70,
        overlay_width=700,
        overlay_height=700,
    )

    assert video_render.disc_square(wider) == (50, 70, 700, 700)
    for layers in (LAYERS, wider):
        assert all(number % 2 == 0 for number in video_render.disc_square(layers))


def test_the_disc_turns_once_from_rgba_with_transparent_corners() -> None:
    graph = graph_of()

    assert graph.count("rotate=") == 1
    assert "rotate=a=2*PI*t*100/180:c=none:ow=380:oh=380" in graph
    assert "[seated][hub]overlay=0:0:format=auto,loop=loop=-1:size=1,setpts=N/30/TB,rotate" in graph
    assert "[2:v]format=rgba[vinyl]" in graph
    assert "[3:v]format=rgba[hub]" in graph
    assert "[vinyl][label]overlay=(W-w)/2:(H-h)/2:format=auto[seated]" in graph


def test_the_square_is_cropped_looped_and_composed_in_planar_rgb() -> None:
    graph = graph_of()

    assert "[4:v]split[floor_source][square_source]" in graph
    assert "[square_source]crop=504:384:170:92,format=gbrp,loop=loop=-1:size=1,setpts=N/30/TB[ground]" in graph
    assert "[ground][spin]overlay=0:4:format=gbrp[disc]" in graph
    assert "[disc][top]overlay=0:0:format=gbrp[square]" in graph
    assert "[5:v]format=rgba[top]" in graph


def test_the_overlay_sits_where_the_drawing_said() -> None:
    moved = turntable.TurntableLayers(
        background=LAYERS.background,
        vinyl=LAYERS.vinyl,
        label_mask=LAYERS.label_mask,
        hub=LAYERS.hub,
        overlay=LAYERS.overlay,
        overlay_x=172,
        overlay_y=94,
        overlay_width=502,
        overlay_height=368,
    )

    graph = video_render.build_filter_complex(moved, DISPLAY_PATH)

    assert "[disc][top]overlay=2:0:format=gbrp[square]" in graph
    assert "[floor][square_yuv]overlay=170:94:format=yuv420" in graph


def test_the_display_is_drawn_once_on_the_still_floor_and_the_song_text_comes_last() -> None:
    plain = graph_of()
    floor = "[floor_source]scale=out_range=tv,format=yuv420p,subtitles=/data/tmp/7/display.ass,loop"

    assert floor in plain
    assert plain.count("subtitles=") == 1
    assert plain.endswith("setsar=1[v]")
    both = graph_of(LYRICS_PATH)

    assert floor in both
    assert both.endswith("setsar=1,subtitles=/data/tmp/7/lyrics.ass[v]")
    assert both.count("subtitles=") == 2


def test_a_colon_in_a_subtitle_path_is_escaped_not_left_to_split_the_option() -> None:
    graph = video_render.build_filter_complex(LAYERS, Path("/data/tmp/7/a:b.ass"), Path("/c:d.ass"))

    assert "a\\:b.ass" in graph
    assert "c\\:d.ass" in graph


def test_the_old_padded_cover_is_gone() -> None:
    graph = graph_of()

    assert "pad=" not in graph
    assert not hasattr(video_render, "COVER_TOP_OFFSET_PX")


def test_the_arguments_are_a_list_of_strings_with_the_output_last() -> None:
    arguments = video_render.build_ffmpeg_arguments(parameters(), LAYERS, OUTPUT_PATH)

    assert isinstance(arguments, list)
    assert all(isinstance(argument, str) for argument in arguments)
    assert arguments[-1] == str(OUTPUT_PATH)


def pairs(arguments: list[str]) -> set[tuple[str, str]]:
    return set(pairwise(arguments))


def test_six_stills_then_the_seeked_audio_are_the_inputs_and_none_is_looped() -> None:
    arguments = video_render.build_ffmpeg_arguments(parameters(), LAYERS, OUTPUT_PATH)
    inputs = [arguments[index + 1] for index, value in enumerate(arguments) if value == "-i"]

    assert inputs == [
        str(COVER_PATH),
        str(LAYERS.label_mask),
        str(LAYERS.vinyl),
        str(LAYERS.hub),
        str(LAYERS.background),
        str(LAYERS.overlay),
        str(AUDIO_PATH),
    ]
    assert "-loop" not in arguments
    assert arguments.index("-ss") == arguments.index(str(AUDIO_PATH)) - 3
    assert arguments.count("-ss") == 1
    assert ("-map", "[v]") in pairs(arguments)
    assert ("-map", "6:a") in pairs(arguments)


def test_every_encoder_flag_the_acceptance_criteria_name_is_present() -> None:
    arguments = video_render.build_ffmpeg_arguments(parameters(), LAYERS, OUTPUT_PATH)
    argument_pairs = pairs(arguments)

    assert ("-c:v", "libx264") in argument_pairs
    assert ("-c:a", "aac") in argument_pairs
    assert ("-pix_fmt", "yuv420p") in argument_pairs
    assert ("-color_range", "tv") in argument_pairs
    assert ("-movflags", "+faststart") in argument_pairs
    assert ("-maxrate", "2000k") in argument_pairs
    assert ("-bufsize", "4000k") in argument_pairs
    assert ("-r", "30") in argument_pairs
    assert ("-f", "mp4") in argument_pairs


def test_the_audio_is_seeked_to_the_absolute_fragment_start() -> None:
    arguments = video_render.build_ffmpeg_arguments(
        video_render.RenderParameters(
            audio_path=AUDIO_PATH,
            cover_path=COVER_PATH,
            fragment_start_ms=30_000,
            fragment_end_ms=75_000,
            display_subtitles_path=DISPLAY_PATH,
        ),
        LAYERS,
        OUTPUT_PATH,
    )
    argument_pairs = pairs(arguments)

    assert ("-ss", "30.000") in argument_pairs
    assert ("-t", "45.000") in argument_pairs


def test_a_reduced_quality_pass_lowers_the_bitrate_ceiling() -> None:
    arguments = video_render.build_ffmpeg_arguments(
        parameters(), LAYERS, OUTPUT_PATH, quality=video_render.REDUCED_QUALITY
    )

    assert ("-maxrate", "1200k") in pairs(arguments)


HASH_INPUTS: dict[str, Any] = {
    "video_id": "n4bPzdIs_nc",
    "audio_duration_ms": 212_000,
    "cover_url": "https://cdn/cover.jpg",
    "fragment_start_ms": 0,
    "fragment_end_ms": 45_000,
    "display_version": "0123456789abcdef",
}


def test_the_hash_is_stable_for_identical_inputs() -> None:
    first = video_render.compute_params_hash(**HASH_INPUTS)
    second = video_render.compute_params_hash(**HASH_INPUTS)

    assert first == second


@pytest.mark.parametrize(
    "overrides",
    [
        {"fragment_end_ms": 30_000},
        {"cover_url": "https://cdn/other.jpg"},
        {"video_id": "dQw4w9WgXcQ"},
        {"text_version": "lrclib:1"},
        {"display_version": "fedcba9876543210"},
    ],
)
def test_every_input_the_frames_depend_on_changes_the_hash(overrides: dict[str, Any]) -> None:
    """`text_version` is an input even when it is `None`, so a render with text
    never reuses one without; a new title or artist list changes `display_version`."""
    assert video_render.compute_params_hash(**HASH_INPUTS) != video_render.compute_params_hash(
        **{**HASH_INPUTS, **overrides}
    )


def test_the_renderer_version_is_an_input_to_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    before = video_render.compute_params_hash(**HASH_INPUTS)
    monkeypatch.setattr(video_render, "RENDERER_VERSION", "another")

    assert video_render.compute_params_hash(**HASH_INPUTS) != before


def test_the_hash_is_a_filename_safe_hex_digest() -> None:
    params_hash = video_render.compute_params_hash(**HASH_INPUTS)

    assert len(params_hash) == 64
    assert set(params_hash) <= set("0123456789abcdef")


def make_probe(size_bytes: int, duration_ms: int = 45_000) -> media_tools.MediaProbe:
    return media_tools.MediaProbe(
        path=OUTPUT_PATH,
        size_bytes=size_bytes,
        duration_ms=duration_ms,
        streams=(
            media_tools.StreamInfo("video", "h264", 720, 896, "yuv420p", "tv"),
            media_tools.StreamInfo("audio", "aac", None, None, None, None),
        ),
    )


@pytest.fixture(autouse=True)
def drawn_layers(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """the drawing is not under test here: it is replaced and its calls are recorded."""
    directories: list[Path] = []

    def fake_write_layers(directory: Path) -> turntable.TurntableLayers:
        directories.append(directory)
        return LAYERS

    monkeypatch.setattr(video_render.turntable, "write_layers", fake_write_layers)
    return directories


def stub_ffmpeg(monkeypatch: pytest.MonkeyPatch, probes: list[media_tools.MediaProbe]) -> list[list[str]]:
    calls: list[list[str]] = []
    remaining = list(probes)

    def fake_run(
        binary: str,
        arguments: list[str],
        **kwargs: Any,
    ) -> Any:
        calls.append([binary, *arguments])
        return None

    monkeypatch.setattr(video_render, "run_media_tool_checked", fake_run)
    monkeypatch.setattr(video_render, "verify_media", lambda path, **kwargs: remaining.pop(0))
    return calls


def test_a_render_is_verified_by_ffprobe_not_by_the_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_ffmpeg(monkeypatch, [make_probe(2_260_000)])

    probe = video_render.render_fragment(parameters(), LAYERS, tmp_path / "out.mp4.partial", timeout_seconds=180)

    assert probe.size_bytes == 2_260_000
    assert calls[0][0] == video_render.FFMPEG_BINARY
    assert str(LAYERS.overlay) in calls[0]


def test_a_render_ffprobe_refuses_fails_the_whole_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg can exit 0 on an unusable file, so the verdict comes from `verify_media`, not from the exit code."""
    monkeypatch.setattr(video_render, "run_media_tool_checked", lambda binary, arguments, **kwargs: None)

    def refuse(path: Path, **kwargs: Any) -> media_tools.MediaProbe:
        raise media_tools.MediaProbeError("файл порожній (0 байт)")

    monkeypatch.setattr(video_render, "verify_media", refuse)

    with pytest.raises(media_tools.MediaProbeError):
        video_render.render_fragment(parameters(), LAYERS, tmp_path / "out.mp4.partial", timeout_seconds=180)


def test_the_layer_drawing_time_is_logged_once_for_both_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(video_render, "logger", fake_logger)
    stub_ffmpeg(monkeypatch, [make_probe(60 * 1024 * 1024), make_probe(20 * 1024 * 1024)])

    video_render.render_fragment_within_size_limit(parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180)

    drawn = [call.kwargs for call in fake_logger.info.call_args_list if call.args == ("render_layers_drawn",)]
    assert len(drawn) == 1
    assert drawn[0]["seconds"] >= 0


def test_a_render_under_the_ceiling_runs_ffmpeg_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drawn_layers: list[Path],
) -> None:
    calls = stub_ffmpeg(monkeypatch, [make_probe(4_440_000)])

    probe, quality = video_render.render_fragment_within_size_limit(
        parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
    )

    assert len(calls) == 1
    assert drawn_layers == [COVER_PATH.parent]
    assert quality is video_render.DEFAULT_QUALITY
    assert probe.size_bytes == 4_440_000


def test_an_oversized_render_gets_exactly_one_lower_bitrate_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drawn_layers: list[Path],
) -> None:
    calls = stub_ffmpeg(monkeypatch, [make_probe(60 * 1024 * 1024), make_probe(20 * 1024 * 1024)])

    probe, quality = video_render.render_fragment_within_size_limit(
        parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
    )

    assert len(calls) == 2
    assert len(drawn_layers) == 1
    assert "1200k" in calls[1]
    assert quality is video_render.REDUCED_QUALITY
    assert probe.size_bytes == 20 * 1024 * 1024


def test_still_oversized_after_the_retry_fails_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the size is checked before Telegram ever sees the file, and there is no branch here that sends one."""
    calls = stub_ffmpeg(monkeypatch, [make_probe(60 * 1024 * 1024), make_probe(55 * 1024 * 1024)])

    with pytest.raises(video_render.RenderTooLargeError, match="дефект рендера"):
        video_render.render_fragment_within_size_limit(parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180)

    assert len(calls) == 2


def build_cover_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_track_without_a_cover_is_a_loud_refusal(tmp_path: Path) -> None:
    client = build_cover_client(lambda request: httpx.Response(200, content=b"jpeg"))
    try:
        with pytest.raises(video_render.MissingCoverError):
            await video_render.download_cover(client, None, tmp_path)
    finally:
        await client.aclose()


async def test_the_cover_is_written_into_the_tmp_directory(tmp_path: Path) -> None:
    client = build_cover_client(lambda request: httpx.Response(200, content=b"jpeg bytes"))
    try:
        path = await video_render.download_cover(client, "https://cdn/cover.jpg", tmp_path)
    finally:
        await client.aclose()

    assert path.read_bytes() == b"jpeg bytes"


async def test_an_empty_cover_body_is_refused(tmp_path: Path) -> None:
    client = build_cover_client(lambda request: httpx.Response(200, content=b""))
    try:
        with pytest.raises(video_render.MissingCoverError, match="порожньою"):
            await video_render.download_cover(client, "https://cdn/cover.jpg", tmp_path)
    finally:
        await client.aclose()


async def test_an_oversized_cover_is_refused_before_it_reaches_ffmpeg(tmp_path: Path) -> None:
    body = b"x" * (video_render.COVER_MAX_BYTES + 1)
    client = build_cover_client(lambda request: httpx.Response(200, content=body))
    try:
        with pytest.raises(video_render.MissingCoverError, match="ліміт"):
            await video_render.download_cover(client, "https://cdn/cover.jpg", tmp_path)
    finally:
        await client.aclose()


async def test_a_failed_cover_request_propagates_rather_than_being_isolated(
    tmp_path: Path,
) -> None:
    """`isolated_source_call` is for metadata sources, where a loss means a poorer post. No cover means no product."""
    client = build_cover_client(lambda request: httpx.Response(503))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await video_render.download_cover(client, "https://cdn/cover.jpg", tmp_path)
    finally:
        await client.aclose()


def test_the_thumbnail_is_taken_from_the_rendered_file_itself(tmp_path: Path) -> None:
    """from the video, not the raw cover: the frame it shows is the padded
    720x896 one the subscriber sees, letterboxing included."""
    arguments = video_render.build_thumbnail_arguments(tmp_path / "a.mp4", tmp_path / "a.jpg")

    assert arguments[arguments.index("-i") + 1] == str(tmp_path / "a.mp4")
    assert arguments[arguments.index("-frames:v") + 1] == "1"
    assert "shell" not in arguments


def test_the_thumbnail_stays_inside_telegrams_320_pixel_cap(tmp_path: Path) -> None:
    arguments = video_render.build_thumbnail_arguments(tmp_path / "a.mp4", tmp_path / "a.jpg")
    scale = arguments[arguments.index("-vf") + 1]

    assert scale.startswith(f"scale={video_render.THUMBNAIL_MAX_EDGE}:{video_render.THUMBNAIL_MAX_EDGE}")
    assert "force_original_aspect_ratio=decrease" in scale


def test_a_failing_thumbnail_returns_none_instead_of_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cosmetic: Telegram builds its own preview, and losing the custom one is
    never a reason to fail a post whose video is already verified."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise media_tools.MediaToolFailedError("ffmpeg", 1, "boom")

    monkeypatch.setattr(video_render, "run_media_tool_checked", refuse)

    assert video_render.render_thumbnail(tmp_path / "a.mp4", tmp_path / "a.jpg") is None


def test_a_thumbnail_that_came_out_empty_is_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the exit code is never the verdict here either — a zero-byte JPEG
    attached to `send_video` is a 400 from Telegram, not a preview."""
    output_path = tmp_path / "a.jpg"

    def write_nothing(*args: object, **kwargs: object) -> object:
        output_path.write_bytes(b"")
        return None

    monkeypatch.setattr(video_render, "run_media_tool_checked", write_nothing)

    assert video_render.render_thumbnail(tmp_path / "a.mp4", output_path) is None


def test_a_real_thumbnail_is_returned_by_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "a.jpg"

    def write_jpeg(*args: object, **kwargs: object) -> object:
        output_path.write_bytes(b"jpeg bytes")
        return None

    monkeypatch.setattr(video_render, "run_media_tool_checked", write_jpeg)

    assert video_render.render_thumbnail(tmp_path / "a.mp4", output_path) == output_path


def test_no_subtitles_means_no_text_version() -> None:
    assert video_render.text_version_of(None) is None


def test_the_text_version_is_a_short_digest_of_the_subtitle_file() -> None:
    version = video_render.text_version_of("[Script Info]\nDialogue: раз\n")

    assert version is not None
    assert len(version) == video_render.TEXT_VERSION_LENGTH
    assert version == video_render.text_version_of("[Script Info]\nDialogue: раз\n")
    assert version != video_render.text_version_of("[Script Info]\nDialogue: два\n")


def test_text_digest_is_short_stable_and_follows_the_text() -> None:
    digest = video_render.text_digest("[Script Info]\nDialogue: раз\n")

    assert len(digest) == video_render.TEXT_VERSION_LENGTH
    assert digest == video_render.text_digest("[Script Info]\nDialogue: раз\n")
    assert digest != video_render.text_digest("[Script Info]\nDialogue: два\n")


# the digest of the graph, the encoder arguments and every `layout` constant. A change to any of
# them, or to the drawing in `turntable`, needs a new `RENDERER_VERSION` and a new digest here.
PINNED_RENDERER_VERSION = "turntable-2"
PINNED_DIGEST = "ea5a25a51beea282a9bb8cb43662848758420ff99ba22e222b396c79539543fa"


def renderer_material() -> str:
    constants = {name: value for name, value in vars(layout).items() if name.isupper()}
    return json.dumps(
        {
            "graph": video_render.build_filter_complex(LAYERS, DISPLAY_PATH, LYRICS_PATH),
            "arguments": [
                argument.replace("\\", "/")
                for argument in video_render.build_ffmpeg_arguments(parameters(LYRICS_PATH), LAYERS, OUTPUT_PATH)
            ],
            "layout": constants,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def test_the_renderer_version_is_pinned_to_the_graph_the_encoder_and_the_layout() -> None:
    digest = hashlib.sha256(renderer_material().encode("utf-8")).hexdigest()

    assert video_render.RENDERER_VERSION == PINNED_RENDERER_VERSION
    assert digest == PINNED_DIGEST


def test_changing_a_layout_constant_changes_the_pinned_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = renderer_material()
    monkeypatch.setattr(layout, "GLASS_RGB", (6, 8, 11))

    assert renderer_material() != before


def test_changing_the_graph_changes_the_pinned_material(monkeypatch: pytest.MonkeyPatch) -> None:
    before = renderer_material()
    monkeypatch.setattr(video_render, "DISC_REVOLUTIONS_PER_SECOND", "101/180")

    assert renderer_material() != before
