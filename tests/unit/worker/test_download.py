"""The `videoId` source, id validation, argument lists, the duration check
against a stub or a different recording, and the distinction between "the
extractor broke" and "this track is gone".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from pisniar.worker import download as audio_download
from pisniar.worker import ffmpeg as media_tools

MUSIC_VIDEO_ID = "n4bPzdIs_nc"
CLIP_VIDEO_ID = "dQw4w9WgXcQ"


def links(**platforms: str) -> dict[str, object]:
    return {
        platform: {"url": url, "source": "ytmusic", "match": "confirmed"}
        for platform, url in platforms.items()
    }


def test_the_video_id_comes_from_youtube_music() -> None:
    platform_links = links(youtubeMusic=f"https://music.youtube.com/watch?v={MUSIC_VIDEO_ID}")

    assert audio_download.youtube_music_video_id(platform_links) == MUSIC_VIDEO_ID


def test_a_clip_link_never_stands_in_for_the_platform_link() -> None:
    """The clip may carry an intro, an outro or another mastering, so
    `["youtube"]` is not a fallback: it is a different recording."""
    platform_links = links(
        youtube=f"https://www.youtube.com/watch?v={CLIP_VIDEO_ID}",
        youtubeMusic=f"https://music.youtube.com/watch?v={MUSIC_VIDEO_ID}",
    )

    assert audio_download.youtube_music_video_id(platform_links) == MUSIC_VIDEO_ID


def test_only_a_clip_link_is_a_loud_refusal_not_a_fallback() -> None:
    platform_links = links(youtube=f"https://www.youtube.com/watch?v={CLIP_VIDEO_ID}")

    with pytest.raises(audio_download.MissingYouTubeMusicLinkError):
        audio_download.youtube_music_video_id(platform_links)


def test_no_platform_links_at_all_is_the_same_refusal() -> None:
    with pytest.raises(audio_download.MissingYouTubeMusicLinkError):
        audio_download.youtube_music_video_id({})


def test_a_platform_link_stored_without_a_url_is_refused() -> None:
    with pytest.raises(audio_download.MissingYouTubeMusicLinkError):
        audio_download.youtube_music_video_id({"youtubeMusic": {"source": "ytmusic"}})


def test_a_platform_link_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(audio_download.MissingYouTubeMusicLinkError):
        audio_download.youtube_music_video_id({"youtubeMusic": "https://music.youtube.com/x"})


@pytest.mark.parametrize(
    "stored_url",
    [
        "https://music.youtube.com/watch?v=../../etc/passwd",
        "https://music.youtube.com/watch?v=n4bPzdIs_n;",
        "https://music.youtube.com/watch?v=n4bPzdIs n",
        "https://music.youtube.com/watch?v=n4bPzdIs_n０",
        "https://music.youtube.com/playlist",
        "https://example.com/track/1",
    ],
)
def test_an_id_that_fails_validation_never_reaches_yt_dlp(stored_url: str) -> None:
    with pytest.raises(audio_download.AudioDownloadError):
        audio_download.youtube_music_video_id(links(youtubeMusic=stored_url))


def test_the_watch_url_is_built_here_from_the_validated_id() -> None:
    assert (
        audio_download.build_watch_url(MUSIC_VIDEO_ID)
        == f"https://www.youtube.com/watch?v={MUSIC_VIDEO_ID}"
    )


def test_the_watch_url_refuses_an_id_that_did_not_pass_validation() -> None:
    with pytest.raises(audio_download.InvalidYouTubeVideoIdError):
        audio_download.build_watch_url("../../etc")


def test_the_arguments_are_a_list_with_the_url_last_and_no_self_update() -> None:
    """A runtime self-update is prohibited, so `--no-update` is passed rather
    than merely not asked for."""
    arguments = audio_download.build_ytdlp_arguments(
        "https://www.youtube.com/watch?v=n4bPzdIs_nc", "/data/tmp/7/audio.%(ext)s"
    )

    assert isinstance(arguments, list)
    assert all(isinstance(argument, str) for argument in arguments)
    assert arguments[-2:] == ["--", "https://www.youtube.com/watch?v=n4bPzdIs_nc"]
    assert "--no-update" in arguments
    assert "--no-playlist" in arguments
    assert "/data/tmp/7/audio.%(ext)s" in arguments


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube] abc: Unable to extract player response",
        "WARNING: No supported JavaScript runtime could be found",
        "ERROR: nsig extraction failed: Some formats may be missing",
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
        "ERROR: unable to download API page",
    ],
)
def test_a_broken_extractor_gets_its_own_loud_class(stderr: str) -> None:
    """When the deprecated path goes, every track fails at once. If that reads
    as "this song is missing", diagnosis goes the wrong way for days."""
    error = audio_download.classify_ytdlp_failure(1, stderr)

    assert isinstance(error, audio_download.YtdlpExtractorBrokenError)
    assert "ІНШІ треки" in str(error)


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube] abc: Video unavailable",
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] abc: This video has been removed by the uploader",
        "ERROR: [youtube] abc: The video is not available in your country",
    ],
)
def test_a_missing_recording_gets_the_per_track_class(stderr: str) -> None:
    error = audio_download.classify_ytdlp_failure(1, stderr)

    assert isinstance(error, audio_download.YtdlpVideoUnavailableError)


def test_extractor_breakage_wins_when_both_markers_are_present() -> None:
    """The expensive misdiagnosis is a mass failure read as one track's
    problem, so ambiguity resolves towards the loud class."""
    error = audio_download.classify_ytdlp_failure(
        1, "ERROR: Video unavailable\nERROR: Unable to extract player response"
    )

    assert isinstance(error, audio_download.YtdlpExtractorBrokenError)


def test_an_unrecognized_failure_is_still_an_error_never_a_success() -> None:
    error = audio_download.classify_ytdlp_failure(2, "ERROR: something new upstream")

    assert type(error) is audio_download.AudioDownloadError


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["yt-dlp"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def stub_ytdlp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stderr: str = "",
    produces: Path | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(binary: str, arguments: list[str], **kwargs: Any) -> Any:
        calls.append([binary, *arguments])
        if produces is not None:
            produces.write_bytes(b"audio bytes")
        return completed(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(audio_download, "run_media_tool", fake_run)
    monkeypatch.setattr(audio_download, "read_ytdlp_version", lambda **kwargs: "2025.12.8")
    return calls


def test_a_download_returns_the_file_and_the_version_that_made_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_ytdlp(monkeypatch, produces=tmp_path / "audio.opus")

    path, version = audio_download.download_audio(MUSIC_VIDEO_ID, tmp_path, timeout_seconds=30)

    assert path == tmp_path / "audio.opus"
    assert version == "2025.12.8"


def test_a_failed_download_raises_the_classified_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_ytdlp(monkeypatch, returncode=1, stderr="ERROR: Unable to extract player response")

    with pytest.raises(audio_download.YtdlpExtractorBrokenError):
        audio_download.download_audio(MUSIC_VIDEO_ID, tmp_path, timeout_seconds=30)


def test_exit_zero_with_no_file_on_disk_is_still_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_ytdlp(monkeypatch)

    with pytest.raises(audio_download.AudioDownloadError, match="файлу"):
        audio_download.download_audio(MUSIC_VIDEO_ID, tmp_path, timeout_seconds=30)


def test_a_partially_written_file_is_not_taken_for_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_ytdlp(monkeypatch, produces=tmp_path / "audio.opus.part")

    with pytest.raises(audio_download.AudioDownloadError, match="файлу"):
        audio_download.download_audio(MUSIC_VIDEO_ID, tmp_path, timeout_seconds=30)


def test_the_version_is_read_from_yt_dlp_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audio_download,
        "run_media_tool_checked",
        lambda binary, arguments, **kwargs: completed(stdout="2025.12.8\n"),
    )

    assert audio_download.read_ytdlp_version() == "2025.12.8"


def stub_probe(monkeypatch: pytest.MonkeyPatch, duration_ms: int) -> None:
    probe = media_tools.MediaProbe(
        path=Path("/data/audio/1/x.opus"),
        size_bytes=1024,
        duration_ms=duration_ms,
        streams=(
            media_tools.StreamInfo(
                codec_type="audio",
                codec_name="opus",
                width=None,
                height=None,
                pix_fmt=None,
                color_range=None,
            ),
        ),
    )
    monkeypatch.setattr(audio_download, "verify_media", lambda path, **kwargs: probe)


def test_a_duration_far_from_the_resolve_fails_rather_than_being_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is what catches yt-dlp handing back a stub or a different recording,
    the one thing that would otherwise be completely silent."""
    stub_probe(monkeypatch, duration_ms=10_000)

    with pytest.raises(audio_download.DownloadedAudioMismatchError, match="інший запис"):
        audio_download.verify_downloaded_audio(tmp_path / "audio.opus", 212_000)


def test_a_duration_inside_the_matching_tolerance_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_probe(monkeypatch, duration_ms=212_900)

    probe = audio_download.verify_downloaded_audio(tmp_path / "audio.opus", 212_000)

    assert probe.duration_ms == 212_900


@pytest.mark.parametrize("stored_duration", [None, 0])
def test_an_unknown_stored_duration_skips_the_comparison_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored_duration: int | None
) -> None:
    """Rows resolved before `duration_ms` existed hold NULL; that branch is
    handled, not crashed through."""
    stub_probe(monkeypatch, duration_ms=212_900)

    probe = audio_download.verify_downloaded_audio(tmp_path / "audio.opus", stored_duration)

    assert probe.duration_ms == 212_900
