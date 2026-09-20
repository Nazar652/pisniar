"""Everything the media pipeline touches on the `/data` volume: paths built
only from numeric ids, a validated video id and a hex hash (§5 — never from a
track title, which is both path traversal and Cyrillic filenames), the
free-space check that runs *before* a download, and the atomic publish.
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
RENDER_DIRECTORY_NAME = "render"
TMP_DIRECTORY_NAME = "tmp"
SUBTITLES_FILE_NAME = "lyrics.ass"

# `.partial` is part of the idempotency guard, not an optimization: with the
# lock gone (§2), "the final name exists" is the only thing left to trust.
PARTIAL_SUFFIX = ".partial"

_EXTENSION_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")
_PARAMS_HASH_RE = re.compile(r"^[0-9a-f]{16,64}$")


class MediaPathError(ValueError):
    """A path component that would escape `/data` or carry a track's name."""


class InsufficientDiskSpaceError(RuntimeError):
    def __init__(self, media_root: Path, free_bytes: int, required_bytes: int) -> None:
        self.free_bytes = free_bytes
        self.required_bytes = required_bytes
        super().__init__(
            f"на томі {media_root} вільно {free_bytes} байт, потрібно щонайменше "
            f"{required_bytes} — завантаження не починалось"
        )


def audio_directory(media_root: Path, track_id: int) -> Path:
    """Per track (D-61), not per post: two posts of one recording must not
    download it twice."""
    return media_root / AUDIO_DIRECTORY_NAME / _numeric_id(track_id, "track_id")


def audio_file_path(media_root: Path, track_id: int, video_id: str, extension: str) -> Path:
    """`/data/audio/<track_id>/<video_id>.<ext>`."""
    return audio_directory(media_root, track_id) / f"{_video_id(video_id)}.{_extension(extension)}"


def find_existing_audio(media_root: Path, track_id: int, video_id: str) -> Path | None:
    """yt-dlp picks the container, so the extension is only known after a
    download — the stored artifact is found by its stem instead."""
    directory = audio_directory(media_root, track_id)
    if not directory.is_dir():
        return None

    for candidate in sorted(directory.glob(f"{_video_id(video_id)}.*")):
        existing = existing_artifact(candidate)
        if existing is not None:
            return existing
    return None


def render_directory(media_root: Path, post_id: int) -> Path:
    return media_root / RENDER_DIRECTORY_NAME / _numeric_id(post_id, "post_id")


def render_file_path(media_root: Path, post_id: int, params_hash: str) -> Path:
    """`/data/render/<post_id>/<params_hash>.mp4` — per post: the caption and
    the fragment bounds belong to the publication, not to the recording."""
    return render_directory(media_root, post_id) / f"{_params_hash(params_hash)}.mp4"


def thumbnail_file_path(media_root: Path, post_id: int, params_hash: str) -> Path:
    """Beside its video and derived from the same hash, so it needs no column
    of its own and is removed by the same directory cleanup."""
    return render_directory(media_root, post_id) / f"{_params_hash(params_hash)}.jpg"


def task_tmp_directory(media_root: Path, post_id: int) -> Path:
    """`/data/tmp/<post_id>/` — removed in a `finally`, always."""
    return media_root / TMP_DIRECTORY_NAME / _numeric_id(post_id, "post_id")


def subtitles_file_path(media_root: Path, post_id: int) -> Path:
    """`/data/tmp/<post_id>/lyrics.ass` — a numeric id and a constant name,
    so the string that ends up inside the `subtitles=` filter argument can
    carry no `,`, `;`, `[` or `]` (§4 p.8: `_escape_filter_path` covers the
    other three, and this is what keeps the rest out of reach)."""
    return task_tmp_directory(media_root, post_id) / SUBTITLES_FILE_NAME


def staging_path(tmp_directory: Path, final_path: Path) -> Path:
    """The half-written file lives in tmp under a `.partial` name, so nothing
    with the final name can exist before `publish_atomically()` runs."""
    return tmp_directory / (final_path.name + PARTIAL_SUFFIX)


def ensure_free_space(media_root: Path, required_bytes: int) -> int:
    """Running out mid-render leaves a file that looks finished; this is the
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
    """The filesystem half of the idempotency guard: a non-empty file at the
    final name is a finished one, because only `os.replace()` creates it."""
    if path.is_file() and path.stat().st_size > 0:
        return path
    return None


def remove_post_artifacts(media_root: Path, post_id: int) -> None:
    """Called only **after** the deleting transaction committed (§5): a
    rollback would otherwise leave a row pointing at a file that is gone,
    which is worse than a file with no row."""
    directory = render_directory(media_root, post_id)
    if not directory.exists():
        return

    remove_tree_quietly(directory)
    logger.info("media_post_artifacts_removed", post_id=post_id, path=str(directory))


def remove_track_artifacts(media_root: Path, track_id: int) -> None:
    """The audio half of the same rule. Retention (Q8) keeps audio until step
    6; this is the deletion path, not a sweeper."""
    directory = audio_directory(media_root, track_id)
    if not directory.exists():
        return

    remove_tree_quietly(directory)
    logger.info("media_track_artifacts_removed", track_id=track_id, path=str(directory))


def remove_tree_quietly(path: Path) -> None:
    """Cleanup must never replace the real error with its own."""
    try:
        shutil.rmtree(path, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("media_tmp_cleanup_failed", path=str(path), exc_info=True)


def remove_file_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("media_file_removal_failed", path=str(path), exc_info=True)


def _numeric_id(value: int, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MediaPathError(f"{field_name} має бути додатним цілим, отримано {value!r}")
    return str(value)


def _video_id(value: str) -> str:
    if not is_valid_youtube_video_id(value):
        raise MediaPathError(f"video_id не пройшов валідацію: {value!r}")
    return value


def _extension(value: str) -> str:
    if not _EXTENSION_RE.match(value):
        raise MediaPathError(f"розширення не пройшло валідацію: {value!r}")
    return value


def _params_hash(value: str) -> str:
    if not _PARAMS_HASH_RE.match(value):
        raise MediaPathError(f"params_hash не пройшов валідацію: {value!r}")
    return value
