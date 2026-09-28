"""The filtergraph, the required encoder flags, the size ceiling with its single
retry, and `params_hash`, the input that decides whether ffmpeg runs at all.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from pisniar.worker import ffmpeg as media_tools
from pisniar.worker import render as video_render

AUDIO_PATH = Path("/data/audio/1/n4bPzdIs_nc.opus")
COVER_PATH = Path("/data/tmp/7/cover.img")
OUTPUT_PATH = Path("/data/tmp/7/abc.mp4.partial")


def parameters(subtitles_path: Path | None = None) -> video_render.RenderParameters:
    return video_render.RenderParameters(
        audio_path=AUDIO_PATH,
        cover_path=COVER_PATH,
        fragment_start_ms=0,
        fragment_end_ms=45_000,
        subtitles_path=subtitles_path,
    )


def test_the_scale_filter_converts_the_range_instead_of_only_naming_the_format() -> None:
    """Measured: the cover arrives from the Spotify CDN as full-range JPEG, and a
    naive `format=yuv420p` yields `yuvj420p` every time."""
    graph = video_render.build_filter_complex()

    assert "in_range=pc:out_range=tv" in graph
    assert "flags=lanczos" in graph
    assert "format=yuv420p" in graph


def test_the_frame_is_seven_twenty_by_twelve_eighty_with_square_pixels() -> None:
    graph = video_render.build_filter_complex()

    assert "scale=720:1280:force_original_aspect_ratio=decrease" in graph
    assert "pad=720:1280:(ow-iw)/2:100:color=black" in graph
    assert "setsar=1" in graph


def test_the_cover_is_lifted_instead_of_centred_so_the_text_band_is_black() -> None:
    """Centred, the cover sits under the side lines; lifted, the bottom band stays
    empty frame, at a constant offset rather than `(oh-ih)/2`."""
    graph = video_render.build_filter_complex()

    assert video_render.COVER_TOP_OFFSET_PX == 100
    assert "(oh-ih)/2" not in graph


def test_no_subtitle_filter_exists_in_4a() -> None:
    assert "subtitles=" not in video_render.build_filter_complex()


def test_a_subtitle_path_handed_in_from_outside_is_appended() -> None:
    """Subtitles come in as a path, not a flag flipped inside, so text is an
    addition to a verified filtergraph."""
    graph = video_render.build_filter_complex(Path("/data/tmp/7/lyrics.ass"))

    assert "subtitles=/data/tmp/7/lyrics.ass" in graph


def test_a_colon_in_a_subtitle_path_is_escaped_not_left_to_split_the_option() -> None:
    graph = video_render.build_filter_complex(Path("/data/tmp/7/a:b.ass"))

    assert "a\\:b.ass" in graph


def test_the_arguments_are_a_list_of_strings_with_the_output_last() -> None:
    arguments = video_render.build_ffmpeg_arguments(parameters(), OUTPUT_PATH)

    assert isinstance(arguments, list)
    assert all(isinstance(argument, str) for argument in arguments)
    assert arguments[-1] == str(OUTPUT_PATH)


def pairs(arguments: list[str]) -> set[tuple[str, str]]:
    return set(pairwise(arguments))


def test_every_encoder_flag_the_acceptance_criteria_name_is_present() -> None:
    arguments = video_render.build_ffmpeg_arguments(parameters(), OUTPUT_PATH)
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
        ),
        OUTPUT_PATH,
    )
    argument_pairs = pairs(arguments)

    assert ("-ss", "30.000") in argument_pairs
    assert ("-t", "45.000") in argument_pairs


def test_a_reduced_quality_pass_lowers_the_bitrate_ceiling() -> None:
    arguments = video_render.build_ffmpeg_arguments(
        parameters(), OUTPUT_PATH, quality=video_render.REDUCED_QUALITY
    )

    assert ("-maxrate", "1200k") in pairs(arguments)


def test_the_hash_is_stable_for_identical_inputs() -> None:
    first = video_render.compute_params_hash(
        video_id="n4bPzdIs_nc",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
    )
    second = video_render.compute_params_hash(
        video_id="n4bPzdIs_nc",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
    )

    assert first == second


@pytest.mark.parametrize(
    "overrides",
    [
        {"fragment_end_ms": 30_000},
        {"cover_url": "https://cdn/other.jpg"},
        {"video_id": "dQw4w9WgXcQ"},
        {"text_version": "lrclib:1"},
    ],
)
def test_every_input_the_frames_depend_on_changes_the_hash(overrides: dict[str, Any]) -> None:
    """`text_version` is an input even when it is `None`, so a render with text
    never reuses one without."""
    baseline: dict[str, Any] = {
        "video_id": "n4bPzdIs_nc",
        "audio_duration_ms": 212_000,
        "cover_url": "https://cdn/cover.jpg",
        "fragment_start_ms": 0,
        "fragment_end_ms": 45_000,
    }

    assert video_render.compute_params_hash(**baseline) != video_render.compute_params_hash(
        **{**baseline, **overrides}
    )


def test_the_hash_is_a_filename_safe_hex_digest() -> None:
    params_hash = video_render.compute_params_hash(
        video_id="n4bPzdIs_nc",
        audio_duration_ms=212_000,
        cover_url="https://cdn/cover.jpg",
        fragment_start_ms=0,
        fragment_end_ms=45_000,
    )

    assert len(params_hash) == 64
    assert set(params_hash) <= set("0123456789abcdef")


def make_probe(size_bytes: int, duration_ms: int = 45_000) -> media_tools.MediaProbe:
    return media_tools.MediaProbe(
        path=OUTPUT_PATH,
        size_bytes=size_bytes,
        duration_ms=duration_ms,
        streams=(
            media_tools.StreamInfo("video", "h264", 720, 1280, "yuv420p", "tv"),
            media_tools.StreamInfo("audio", "aac", None, None, None, None),
        ),
    )


def stub_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, probes: list[media_tools.MediaProbe]
) -> list[list[str]]:
    calls: list[list[str]] = []
    remaining = list(probes)

    def fake_run(binary: str, arguments: list[str], **kwargs: Any) -> Any:
        calls.append([binary, *arguments])
        return None

    monkeypatch.setattr(video_render, "run_media_tool_checked", fake_run)
    monkeypatch.setattr(video_render, "verify_media", lambda path, **kwargs: remaining.pop(0))
    return calls


def test_a_render_is_verified_by_ffprobe_not_by_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_ffmpeg(monkeypatch, [make_probe(2_260_000)])

    probe = video_render.render_fragment(
        parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
    )

    assert probe.size_bytes == 2_260_000
    assert calls[0][0] == video_render.FFMPEG_BINARY


def test_a_render_ffprobe_refuses_fails_the_whole_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg can exit 0 on an unusable file, so the verdict comes from
    `verify_media`, not from the exit code."""
    monkeypatch.setattr(
        video_render, "run_media_tool_checked", lambda binary, arguments, **kwargs: None
    )

    def refuse(path: Path, **kwargs: Any) -> media_tools.MediaProbe:
        raise media_tools.MediaProbeError("файл порожній (0 байт)")

    monkeypatch.setattr(video_render, "verify_media", refuse)

    with pytest.raises(media_tools.MediaProbeError):
        video_render.render_fragment(
            parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
        )


def test_a_render_under_the_ceiling_runs_ffmpeg_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_ffmpeg(monkeypatch, [make_probe(4_440_000)])

    probe, quality = video_render.render_fragment_within_size_limit(
        parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
    )

    assert len(calls) == 1
    assert quality is video_render.DEFAULT_QUALITY
    assert probe.size_bytes == 4_440_000


def test_an_oversized_render_gets_exactly_one_lower_bitrate_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_ffmpeg(monkeypatch, [make_probe(60 * 1024 * 1024), make_probe(20 * 1024 * 1024)])

    probe, quality = video_render.render_fragment_within_size_limit(
        parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
    )

    assert len(calls) == 2
    assert "1200k" in calls[1]
    assert quality is video_render.REDUCED_QUALITY
    assert probe.size_bytes == 20 * 1024 * 1024


def test_still_oversized_after_the_retry_fails_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size is checked before Telegram ever sees the file, and there is no
    branch here that sends one."""
    calls = stub_ffmpeg(monkeypatch, [make_probe(60 * 1024 * 1024), make_probe(55 * 1024 * 1024)])

    with pytest.raises(video_render.RenderTooLargeError, match="дефект рендера"):
        video_render.render_fragment_within_size_limit(
            parameters(), tmp_path / "out.mp4.partial", timeout_seconds=180
        )

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
    """`isolated_source_call` is for metadata sources, where a loss means a
    poorer post. No cover means no product."""
    client = build_cover_client(lambda request: httpx.Response(503))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await video_render.download_cover(client, "https://cdn/cover.jpg", tmp_path)
    finally:
        await client.aclose()


def test_the_thumbnail_is_taken_from_the_rendered_file_itself(tmp_path: Path) -> None:
    """From the video, not the raw cover: the frame it shows is the padded
    720x1280 one the subscriber sees, letterboxing included."""
    arguments = video_render.build_thumbnail_arguments(tmp_path / "a.mp4", tmp_path / "a.jpg")

    assert arguments[arguments.index("-i") + 1] == str(tmp_path / "a.mp4")
    assert arguments[arguments.index("-frames:v") + 1] == "1"
    assert "shell" not in arguments


def test_the_thumbnail_stays_inside_telegrams_320_pixel_cap(tmp_path: Path) -> None:
    arguments = video_render.build_thumbnail_arguments(tmp_path / "a.mp4", tmp_path / "a.jpg")
    scale = arguments[arguments.index("-vf") + 1]

    assert scale.startswith(
        f"scale={video_render.THUMBNAIL_MAX_EDGE}:{video_render.THUMBNAIL_MAX_EDGE}"
    )
    assert "force_original_aspect_ratio=decrease" in scale


def test_a_failing_thumbnail_returns_none_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cosmetic: Telegram builds its own preview, and losing the custom one is
    never a reason to fail a post whose video is already verified."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise media_tools.MediaToolFailedError("ffmpeg", 1, "boom")

    monkeypatch.setattr(video_render, "run_media_tool_checked", refuse)

    assert video_render.render_thumbnail(tmp_path / "a.mp4", tmp_path / "a.jpg") is None


def test_a_thumbnail_that_came_out_empty_is_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit code is never the verdict here either — a zero-byte JPEG
    attached to `send_video` is a 400 from Telegram, not a preview."""
    output_path = tmp_path / "a.jpg"

    def write_nothing(*args: object, **kwargs: object) -> object:
        output_path.write_bytes(b"")
        return None

    monkeypatch.setattr(video_render, "run_media_tool_checked", write_nothing)

    assert video_render.render_thumbnail(tmp_path / "a.mp4", output_path) is None


def test_a_real_thumbnail_is_returned_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "a.jpg"

    def write_jpeg(*args: object, **kwargs: object) -> object:
        output_path.write_bytes(b"jpeg bytes")
        return None

    monkeypatch.setattr(video_render, "run_media_tool_checked", write_jpeg)

    assert video_render.render_thumbnail(tmp_path / "a.mp4", output_path) == output_path
