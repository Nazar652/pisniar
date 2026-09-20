"""`telegram.video`: the single `send_video` call both the preview and the
channel publication go through. Sharing it is what makes "the admin sees the
exact file that reaches the channel" structural instead of a promise."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pisniar.telegram.video import RenderedVideo, send_rendered_video

CAPTION = '<a href="https://open.spotify.com/track/x">Spotify</a> &amp; Co «Тест»'


def make_video(tmp_path: Path, *, with_thumbnail: bool) -> RenderedVideo:
    video_path = tmp_path / "render.mp4"
    video_path.write_bytes(b"video bytes")

    thumbnail_path = None
    if with_thumbnail:
        thumbnail_path = tmp_path / "render.jpg"
        thumbnail_path.write_bytes(b"jpeg bytes")

    return RenderedVideo(
        path=video_path,
        width=720,
        height=1280,
        duration_ms=45_000,
        thumbnail_path=thumbnail_path,
    )


@pytest.mark.parametrize(
    ("duration_ms", "expected_seconds"), [(45_000, 45), (45_400, 45), (45_600, 46), (31_000, 31)]
)
def test_the_duration_reaches_telegram_in_whole_seconds(
    duration_ms: int, expected_seconds: int
) -> None:
    video = RenderedVideo(path=Path("x.mp4"), width=720, height=1280, duration_ms=duration_ms)

    assert video.duration_seconds == expected_seconds


async def test_the_send_carries_the_dimensions_and_streaming_flag(tmp_path: Path) -> None:
    """Without `supports_streaming` Telegram may hand the file over as a
    download; without the dimensions it guesses the aspect ratio."""
    bot = AsyncMock()

    await send_rendered_video(bot, 999, CAPTION, make_video(tmp_path, with_thumbnail=False))

    sent = bot.send_video.await_args.kwargs
    assert sent["chat_id"] == 999
    assert sent["width"] == 720
    assert sent["height"] == 1280
    assert sent["duration"] == 45
    assert sent["supports_streaming"] is True


async def test_the_caption_goes_out_byte_for_byte_as_html(tmp_path: Path) -> None:
    """S4-A14: the caption is already rendered, escaped HTML under the same
    1024 media limit a photo caption fits (D-45) — it is never rebuilt."""
    bot = AsyncMock()

    await send_rendered_video(bot, 999, CAPTION, make_video(tmp_path, with_thumbnail=False))

    sent = bot.send_video.await_args.kwargs
    assert sent["caption"] == CAPTION
    assert sent["parse_mode"] == "HTML"


async def test_a_present_thumbnail_is_attached(tmp_path: Path) -> None:
    bot = AsyncMock()

    await send_rendered_video(bot, 999, CAPTION, make_video(tmp_path, with_thumbnail=True))

    assert bot.send_video.await_args.kwargs["thumbnail"] is not None


async def test_a_missing_thumbnail_file_is_not_a_send_failure(tmp_path: Path) -> None:
    """Cosmetic: Telegram builds its own preview, and a thumbnail that failed
    to render must never stop a verified video from going out."""
    bot = AsyncMock()
    video = make_video(tmp_path, with_thumbnail=True)
    assert video.thumbnail_path is not None
    video.thumbnail_path.unlink()

    await send_rendered_video(bot, 999, CAPTION, video)

    assert bot.send_video.await_args.kwargs["thumbnail"] is None
