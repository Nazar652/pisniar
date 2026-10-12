"""The media pipeline's `/data` volume: paths built only from numeric ids, a validated video id and a hex hash
(never a track title, which is both path traversal and Cyrillic filenames), the pre-download free-space check, and
the atomic publish.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from pisniar.logging import get_logger
from pisniar.resolve.matching import is_valid_youtube_video_id

logger = get_logger(__name__)

AUDIO_DIRECTORY_NAME = "audio"
PEAKS_DIRECTORY_NAME = "peaks"
RENDER_DIRECTORY_NAME = "render"
TEMPORARY_DIRECTORY_NAME = "tmp"
SUBTITLES_FILE_NAME = "lyrics.ass"
DISPLAY_SUBTITLES_FILE_NAME = "display.ass"

# `.partial` is part of the idempotency guard: no lock is held during a download
# or render, so "the final name exists" is the only thing left to trust.
PARTIAL_SUFFIX = ".partial"

EXTENSION_PATTERN = re.compile(r"^[A-Za-z0-9]{1,8}$")
PARAMS_HASH_PATTERN = re.compile(r"^[0-9a-f]{16,64}$")


class MediaPathError(ValueError):
    """a path component that would escape `/data` or carry a track's name."""


class InsufficientDiskSpaceError(RuntimeError):
    def __init__(
        self,
        media_root: Path,
        free_bytes: int,
        required_bytes: int,
    ) -> None:
        self.free_bytes = free_bytes
        self.required_bytes = required_bytes
        super().__init__(
            f"на томі {media_root} вільно {free_bytes} байт, потрібно щонайменше "
            f"{required_bytes} — завантаження не починалось"
        )


def audio_directory(media_root: Path, track_id: int) -> Path:
    """per track, not per post: two posts of one recording must not download it twice."""
    return media_root / AUDIO_DIRECTORY_NAME / numeric_id(track_id, "track_id")


def audio_file_path(
    media_root: Path,
    track_id: int,
    video_id: str,
    extension: str,
) -> Path:
    return audio_directory(media_root, track_id) / f"{validated_video_id(video_id)}.{validated_extension(extension)}"


def peaks_file_path(
    media_root: Path,
    track_id: int,
    video_id: str,
) -> Path:
    """a subdirectory, so the `<video_id>.*` mask of `find_existing_audio` never sees it."""
    return audio_directory(media_root, track_id) / PEAKS_DIRECTORY_NAME / f"{validated_video_id(video_id)}.json"


def find_existing_audio(
    media_root: Path,
    track_id: int,
    video_id: str,
) -> Path | None:
    """yt-dlp picks the container, so the extension is only known after a
    download — the stored artifact is found by its stem instead."""
    directory = audio_directory(media_root, track_id)
    if not directory.is_dir():
        return None

    for candidate in sorted(directory.glob(f"{validated_video_id(video_id)}.*")):
        existing = existing_artifact(candidate)
        if existing is not None:
            return existing
    return None


def stored_audio_files(media_root: Path) -> list[tuple[int, str, Path]]:
    """every finished audio file as (track id, video id, path), by the stem mask of
    `find_existing_audio`; the peaks subdirectory and stray names never qualify."""
    root = media_root / AUDIO_DIRECTORY_NAME
    if not root.is_dir():
        return []

    found: list[tuple[int, str, Path]] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not directory.name.isdecimal() or int(directory.name) <= 0:
            continue

        track_id = int(directory.name)
        for video_id in sorted({entry.stem for entry in directory.iterdir() if entry.is_file()}):
            audio = find_existing_audio(media_root, track_id, video_id) if is_valid_youtube_video_id(video_id) else None
            if audio is not None:
                found.append((track_id, video_id, audio))
    return found


def render_directory(media_root: Path, post_id: int) -> Path:
    return media_root / RENDER_DIRECTORY_NAME / numeric_id(post_id, "post_id")


def render_file_path(
    media_root: Path,
    post_id: int,
    params_hash: str,
) -> Path:
    """per post: the caption and fragment bounds belong to the publication."""
    return render_directory(media_root, post_id) / f"{validated_params_hash(params_hash)}.mp4"


def thumbnail_file_path(
    media_root: Path,
    post_id: int,
    params_hash: str,
) -> Path:
    """beside its video and derived from the same hash, so it needs no column
    of its own and is removed by the same directory cleanup."""
    return render_directory(media_root, post_id) / f"{validated_params_hash(params_hash)}.jpg"


def task_temporary_directory(media_root: Path, post_id: int) -> Path:
    """removed in a `finally`, always."""
    return media_root / TEMPORARY_DIRECTORY_NAME / numeric_id(post_id, "post_id")


def subtitles_file_path(media_root: Path, post_id: int) -> Path:
    """a numeric id and a constant name keep `,` `;` `[` `]` out of the `subtitles=`
    argument; `escape_filter_path` covers the other three."""
    return task_temporary_directory(media_root, post_id) / SUBTITLES_FILE_NAME


def display_subtitles_file_path(media_root: Path, post_id: int) -> Path:
    """beside `lyrics.ass`, under the same constraints on the name."""
    return task_temporary_directory(media_root, post_id) / DISPLAY_SUBTITLES_FILE_NAME


def staging_path(temporary_directory: Path, final_path: Path) -> Path:
    """the half-written file lives in tmp under a `.partial` name, so nothing
    with the final name can exist before `publish_atomically()` runs."""
    return temporary_directory / (final_path.name + PARTIAL_SUFFIX)


def ensure_free_space(media_root: Path, required_bytes: int) -> int:
    """running out mid-render leaves a file that looks finished; this is the
    loud refusal that happens instead, with numbers, before any network call."""
    usage = shutil.disk_usage(media_root)
    if usage.free < required_bytes:
        logger.error(
            "media_disk_space_exhausted",
            media_root=str(media_root),
            free_bytes=usage.free,
            required_bytes=required_bytes,
        )
        raise InsufficientDiskSpaceError(media_root, usage.free, required_bytes)
    return usage.free


def publish_atomically(temporary_path: Path, final_path: Path) -> Path:
    """`os.replace()` within one filesystem — both paths live on the same
    volume, so a reader either sees the previous file or the complete one."""
    if not temporary_path.is_file():
        raise MediaPathError(f"тимчасового файлу не існує: {temporary_path}")

    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary_path, final_path)

    logger.info("media_file_published", path=str(final_path), size_bytes=final_path.stat().st_size)
    return final_path


def existing_artifact(path: Path) -> Path | None:
    """the filesystem half of the idempotency guard: a non-empty file at the
    final name is a finished one, because only `os.replace()` creates it."""
    if path.is_file() and path.stat().st_size > 0:
        return path
    return None


def remove_post_artifacts(media_root: Path, post_id: int) -> None:
    """only after the deleting commit: a rollback would otherwise leave a row
    pointing at a missing file, which is worse than a file with no row."""
    directory = render_directory(media_root, post_id)
    if not directory.exists():
        return

    remove_tree_quietly(directory)
    logger.info("media_post_artifacts_removed", post_id=post_id, path=str(directory))


def remove_render_files(media_root: Path, post_id: int, params_hash: str) -> None:
    """the video and its thumbnail of one render, nothing else; the row stays as the record."""
    for path in (
        render_file_path(media_root, post_id, params_hash),
        thumbnail_file_path(media_root, post_id, params_hash),
    ):
        path.unlink(missing_ok=True)
    logger.info("media_render_files_removed", post_id=post_id, params_hash=params_hash)


def remove_track_artifacts(media_root: Path, track_id: int) -> None:
    """the audio half of the same rule. Audio is otherwise kept: this is the deletion path, not a sweeper."""
    directory = audio_directory(media_root, track_id)
    if not directory.exists():
        return

    remove_tree_quietly(directory)
    logger.info("media_track_artifacts_removed", track_id=track_id, path=str(directory))


def remove_tree_quietly(path: Path) -> None:
    """cleanup must never replace the real error with its own."""
    try:
        shutil.rmtree(path, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("media_tmp_cleanup_failed", path=str(path), exc_info=True)


def numeric_id(value: int, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MediaPathError(f"{field_name} має бути додатним цілим, отримано {value!r}")
    return str(value)


def validated_video_id(value: str) -> str:
    if not is_valid_youtube_video_id(value):
        raise MediaPathError(f"video_id не пройшов валідацію: {value!r}")
    return value


def validated_extension(value: str) -> str:
    if not EXTENSION_PATTERN.match(value):
        raise MediaPathError(f"розширення не пройшло валідацію: {value!r}")
    return value


def validated_params_hash(value: str) -> str:
    if not PARAMS_HASH_PATTERN.match(value):
        raise MediaPathError(f"params_hash не пройшов валідацію: {value!r}")
    return value
