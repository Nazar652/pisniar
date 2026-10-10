"""The one `send_video` call both the admin preview and the channel publication go through: the caption, `parse_mode`
and every dimension reach Telegram from a single place, so the preview and the published post cannot diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message


@dataclass(frozen=True)
class RenderedVideo:
    """plain values read out of the `render` row and the render constants — `telegram/` never imports `worker/`, so the
    dimensions come in as data."""

    path: Path
    width: int
    height: int
    duration_ms: int
    # telegram builds its own preview from the first frame when this is missing, so a thumbnail that failed to render
    # is never a send failure.
    thumbnail_path: Path | None = None

    @property
    def duration_seconds(self) -> int:
        return round(self.duration_ms / 1000)


async def send_rendered_video(
    bot: Bot,
    chat_id: int,
    caption: str,
    video: RenderedVideo,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """the caption goes out verbatim: the video caption limit is the same 1024 the photo caption already fits."""
    thumbnail = (
        FSInputFile(video.thumbnail_path)
        if video.thumbnail_path is not None and video.thumbnail_path.is_file()
        else None
    )

    return await bot.send_video(
        chat_id=chat_id,
        video=FSInputFile(video.path),
        caption=caption,
        parse_mode="HTML",
        # without it Telegram may hand the file over as a download instead of playing it inline, which is the whole
        # product here.
        supports_streaming=True,
        width=video.width,
        height=video.height,
        duration=video.duration_seconds,
        thumbnail=thumbnail,
        reply_markup=reply_markup,
    )
