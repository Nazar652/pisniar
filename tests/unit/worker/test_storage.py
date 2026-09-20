"""Paths built only from numeric ids, a validated video id and a hex hash
(§5), the free-space refusal that happens *before* a download, and the
atomic publish V35 pins down.
"""

from __future__ import annotations

import os
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest

from pisniar.worker import storage as media_storage

VIDEO_ID = "n4bPzdIs_nc"
PARAMS_HASH = "a" * 64

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_the_audio_path_is_id_and_video_id_only(tmp_path: Path) -> None:
    path = media_storage.audio_file_path(tmp_path, 12, VIDEO_ID, "webm")

    assert path == tmp_path / "audio" / "12" / f"{VIDEO_ID}.webm"


def test_the_render_path_is_post_id_and_hash_only(tmp_path: Path) -> None:
    path = media_storage.render_file_path(tmp_path, 7, PARAMS_HASH)

    assert path == tmp_path / "render" / "7" / f"{PARAMS_HASH}.mp4"


def test_the_tmp_directory_is_per_post(tmp_path: Path) -> None:
    assert media_storage.task_tmp_directory(tmp_path, 7) == tmp_path / "tmp" / "7"


def test_the_subtitle_file_sits_in_the_post_tmp_directory_under_a_constant_name(
    tmp_path: Path,
) -> None:
    assert media_storage.subtitles_file_path(tmp_path, 7) == tmp_path / "tmp" / "7" / "lyrics.ass"


def test_the_subtitle_path_carries_nothing_the_filtergraph_would_read(tmp_path: Path) -> None:
    """§4 p.8: `render._escape_filter_path` covers a backslash, `:` and `'`
    but not `,`, `;`, `[` or `]` — a numeric id and a constant name is what
    keeps those four out of the `subtitles=` argument in the first place."""
    path = media_storage.subtitles_file_path(tmp_path, 12345).as_posix()

    assert not set(path) & set(",;[]")


@pytest.mark.parametrize("post_id", [0, -1, True])
def test_a_non_positive_post_id_never_becomes_a_subtitle_path(tmp_path: Path, post_id: Any) -> None:
    with pytest.raises(media_storage.MediaPathError):
        media_storage.subtitles_file_path(tmp_path, post_id)


@pytest.mark.parametrize("track_id", [0, -1, True])
def test_a_non_positive_id_never_becomes_a_path_component(tmp_path: Path, track_id: Any) -> None:
    with pytest.raises(media_storage.MediaPathError):
        media_storage.audio_file_path(tmp_path, track_id, VIDEO_ID, "webm")


@pytest.mark.parametrize(
    "video_id",
    ["../../etc/passwd", "n4bPzdIs_n;", "n4bPzdIs nc", "n4bPzdIs_n０", "", "n4bPzdIs_ncX"],
)
def test_a_video_id_that_is_not_eleven_url_safe_characters_is_refused(
    tmp_path: Path, video_id: str
) -> None:
    """V29: the same validation the URL goes through, applied again where the
    value would become a filename."""
    with pytest.raises(media_storage.MediaPathError):
        media_storage.audio_file_path(tmp_path, 1, video_id, "webm")


@pytest.mark.parametrize("extension", ["../mp4", "we bm", "", "webm.", "toolongextension"])
def test_an_extension_outside_the_alphabet_is_refused(tmp_path: Path, extension: str) -> None:
    with pytest.raises(media_storage.MediaPathError):
        media_storage.audio_file_path(tmp_path, 1, VIDEO_ID, extension)


@pytest.mark.parametrize("params_hash", ["../evil", "ZZZZ" * 16, "abc", "A" * 64])
def test_a_params_hash_that_is_not_lowercase_hex_is_refused(
    tmp_path: Path, params_hash: str
) -> None:
    with pytest.raises(media_storage.MediaPathError):
        media_storage.render_file_path(tmp_path, 1, params_hash)


def test_the_staging_name_is_the_final_name_plus_partial(tmp_path: Path) -> None:
    final_path = media_storage.render_file_path(tmp_path, 7, PARAMS_HASH)

    staging = media_storage.staging_path(tmp_path / "tmp" / "7", final_path)

    assert staging.name == f"{PARAMS_HASH}.mp4.partial"


def test_free_space_below_the_floor_is_a_loud_refusal_with_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_storage.shutil, "disk_usage", lambda path: DiskUsage(100, 90, 10))

    with pytest.raises(media_storage.InsufficientDiskSpaceError) as raised:
        media_storage.ensure_free_space(tmp_path, 2_000)

    assert raised.value.free_bytes == 10
    assert raised.value.required_bytes == 2_000


def test_enough_free_space_returns_the_number_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_storage.shutil, "disk_usage", lambda path: DiskUsage(100, 10, 90))

    assert media_storage.ensure_free_space(tmp_path, 50) == 90


def test_publishing_moves_the_file_under_its_final_name(tmp_path: Path) -> None:
    staging = tmp_path / "tmp" / "video.mp4.partial"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"rendered")
    final_path = media_storage.render_file_path(tmp_path, 7, PARAMS_HASH)

    media_storage.publish_atomically(staging, final_path)

    assert final_path.read_bytes() == b"rendered"
    assert not staging.exists()


def test_a_crash_between_writing_and_renaming_leaves_no_file_with_the_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V35: the `.partial` name is what makes "the final name exists" mean
    "the file is complete" — the guard moved from the lock to the filesystem."""
    staging = tmp_path / "tmp" / "video.mp4.partial"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"half a render")
    final_path = media_storage.render_file_path(tmp_path, 7, PARAMS_HASH)

    def crash(source: object, destination: object) -> None:
        raise OSError("worker killed")

    monkeypatch.setattr(media_storage.os, "replace", crash)

    with pytest.raises(OSError, match="worker killed"):
        media_storage.publish_atomically(staging, final_path)

    assert not final_path.exists()
    assert media_storage.existing_artifact(final_path) is None


def test_publishing_a_temporary_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(media_storage.MediaPathError, match="тимчасового файлу"):
        media_storage.publish_atomically(
            tmp_path / "absent.partial", media_storage.render_file_path(tmp_path, 7, PARAMS_HASH)
        )


def test_an_empty_file_at_the_final_name_does_not_count_as_an_artifact(tmp_path: Path) -> None:
    empty = tmp_path / "video.mp4"
    empty.touch()

    assert media_storage.existing_artifact(empty) is None


def test_a_non_empty_file_at_the_final_name_is_the_artifact(tmp_path: Path) -> None:
    present = tmp_path / "video.mp4"
    present.write_bytes(b"x")

    assert media_storage.existing_artifact(present) == present


def test_existing_audio_is_found_by_its_stem_whatever_the_container(tmp_path: Path) -> None:
    directory = media_storage.audio_directory(tmp_path, 3)
    directory.mkdir(parents=True)
    (directory / f"{VIDEO_ID}.opus").write_bytes(b"audio")

    assert (
        media_storage.find_existing_audio(tmp_path, 3, VIDEO_ID) == directory / f"{VIDEO_ID}.opus"
    )


def test_an_empty_stored_audio_file_is_not_reused(tmp_path: Path) -> None:
    directory = media_storage.audio_directory(tmp_path, 3)
    directory.mkdir(parents=True)
    (directory / f"{VIDEO_ID}.opus").touch()

    assert media_storage.find_existing_audio(tmp_path, 3, VIDEO_ID) is None


def test_no_stored_audio_directory_means_no_artifact(tmp_path: Path) -> None:
    assert media_storage.find_existing_audio(tmp_path, 3, VIDEO_ID) is None


def test_removing_a_tmp_tree_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    media_storage.remove_tree_quietly(tmp_path / "never-created")


def test_removing_a_tmp_tree_takes_its_contents_with_it(tmp_path: Path) -> None:
    directory = tmp_path / "tmp" / "7"
    directory.mkdir(parents=True)
    (directory / "cover.img").write_bytes(b"jpeg")

    media_storage.remove_tree_quietly(directory)

    assert not directory.exists()


def test_a_failing_tmp_cleanup_never_replaces_the_real_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "tmp" / "7"
    directory.mkdir(parents=True)

    def refuse(path: object, ignore_errors: bool = False) -> None:
        raise PermissionError("busy")

    monkeypatch.setattr(media_storage.shutil, "rmtree", refuse)

    media_storage.remove_tree_quietly(directory)


def test_removing_a_file_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    media_storage.remove_file_quietly(tmp_path / "absent.mp4")


def test_removing_a_file_takes_it_off_disk(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x")

    media_storage.remove_file_quietly(path)

    assert not path.exists()


def test_os_replace_is_what_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Atomicity is `os.replace` and nothing else: a copy-then-delete would
    expose a half-written file under the final name."""
    calls: list[tuple[object, object]] = []
    original_replace = os.replace

    def recording_replace(source: Any, destination: Any) -> None:
        calls.append((source, destination))
        original_replace(source, destination)

    staging = tmp_path / "video.mp4.partial"
    staging.write_bytes(b"done")
    final_path = tmp_path / "out" / "video.mp4"
    monkeypatch.setattr(media_storage.os, "replace", recording_replace)

    media_storage.publish_atomically(staging, final_path)

    assert calls == [(staging, final_path)]


def test_the_thumbnail_sits_beside_its_video_under_the_same_hash(tmp_path: Path) -> None:
    """No column of its own: the path is derived, so the same directory
    cleanup removes both and neither can outlive the other."""
    params_hash = "a" * 64

    video = media_storage.render_file_path(tmp_path, 7, params_hash)
    thumbnail = media_storage.thumbnail_file_path(tmp_path, 7, params_hash)

    assert thumbnail.parent == video.parent
    assert thumbnail.stem == video.stem
    assert thumbnail.suffix == ".jpg"


def test_a_thumbnail_path_validates_its_hash_like_every_other_path(tmp_path: Path) -> None:
    with pytest.raises(media_storage.MediaPathError):
        media_storage.thumbnail_file_path(tmp_path, 7, "../../etc/passwd")


def test_removing_a_posts_artifacts_takes_the_whole_render_directory(tmp_path: Path) -> None:
    directory = media_storage.render_directory(tmp_path, 7)
    directory.mkdir(parents=True)
    (directory / "a.mp4").write_bytes(b"video")
    (directory / "a.jpg").write_bytes(b"thumb")

    media_storage.remove_post_artifacts(tmp_path, 7)

    assert not directory.exists()


def test_removing_a_tracks_artifacts_takes_the_whole_audio_directory(tmp_path: Path) -> None:
    directory = media_storage.audio_directory(tmp_path, 3)
    directory.mkdir(parents=True)
    (directory / "n4bPzdIs_nc.opus").write_bytes(b"audio")

    media_storage.remove_track_artifacts(tmp_path, 3)

    assert not directory.exists()


def test_removing_artifacts_of_a_post_that_never_had_any_is_not_an_error(tmp_path: Path) -> None:
    """It runs after a delete commits, where raising would turn a successful
    cleanup into an alert about a failure that did not happen."""
    media_storage.remove_post_artifacts(tmp_path, 7)
    media_storage.remove_track_artifacts(tmp_path, 3)
