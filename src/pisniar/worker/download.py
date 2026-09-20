"""`yt-dlp` as a subprocess. The `videoId` comes from
`platform_links["youtubeMusic"]` and from nowhere else — `["youtube"]` may
hold a clip with an intro, an outro or a different mastering, and there is no
fallback under any condition (AGENTS.md, AS3). The URL is built here, from a
validated id, and never taken as a string out of the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pisniar.logging import get_logger
from pisniar.resolve.matching import DURATION_TOLERANCE_MS, is_valid_youtube_video_id
from pisniar.resolve.urls import UrlKind, classify_url
from pisniar.worker.ffmpeg import (
    MediaProbe,
    run_media_tool,
    run_media_tool_checked,
    stderr_tail,
    verify_media,
)

logger = get_logger(__name__)

YOUTUBE_MUSIC_PLATFORM = "youtubeMusic"
YTDLP_BINARY = "yt-dlp"

VERSION_TIMEOUT_SECONDS = 30
# yt-dlp names the file by the format it picked, so the extension is unknown
# until it has finished; a fixed stem is what makes finding it deterministic.
DOWNLOAD_STEM = "audio"
AUDIO_FORMAT_SELECTOR = "bestaudio/best"


class AudioDownloadError(RuntimeError):
    """Base for every download failure. No `isolated_source_call` here: a
    download that fails means there is no product, so it is loud (§4)."""


class MissingYouTubeMusicLinkError(AudioDownloadError):
    """No `youtubeMusic` link — `failed` + an alert, and the network is never
    touched. Falling back to `["youtube"]` is what this class forbids."""


class InvalidYouTubeVideoIdError(AudioDownloadError):
    """The stored link did not yield an id `is_valid_youtube_video_id()` accepts."""


class YtdlpExtractorBrokenError(AudioDownloadError):
    """D-80: the extraction path is deprecated upstream. When it goes, every
    track fails at once — and that must not read as "this song is missing"."""


class YtdlpVideoUnavailableError(AudioDownloadError):
    """This one recording is gone, private, geo-blocked or taken down."""


class DownloadedAudioMismatchError(AudioDownloadError):
    """`ffprobe` says the file is not the track the resolve measured."""


# Checked first, on purpose: the expensive misdiagnosis is a mass extractor
# failure read as a per-track problem, never the other way round (D-80).
_EXTRACTOR_BROKEN_MARKERS = (
    "unable to extract",
    "failed to extract any player response",
    "nsig extraction failed",
    "signature extraction failed",
    "no supported javascript runtime",
    "sign in to confirm you're not a bot",
    "sign in to confirm your age",
    "unable to download api page",
    "requested format is not available",
    "please report this issue",
    "http error 403",
)

_VIDEO_UNAVAILABLE_MARKERS = (
    "video unavailable",
    "this video is not available",
    "private video",
    "removed by the uploader",
    "has been terminated",
    "video has been removed",
    "is not available in your country",
    "does not exist",
)


@dataclass(frozen=True)
class DownloadedAudio:
    path: Path
    video_id: str
    probe: MediaProbe
    ytdlp_version: str


def youtube_music_video_id(platform_links: dict[str, object]) -> str:
    """Reads `["youtubeMusic"]` and nothing else. Mutating this to look at
    `["youtube"]` must fail a test — that is what V27 pins down."""
    link = platform_links.get(YOUTUBE_MUSIC_PLATFORM)
    if not isinstance(link, dict):
        raise MissingYouTubeMusicLinkError(
            "у треку немає посилання youtubeMusic — завантажити аудіо нізвідки, "
            "і брати youtube-кліп замість нього заборонено"
        )

    url = link.get("url")
    if not isinstance(url, str) or not url:
        raise MissingYouTubeMusicLinkError(
            "посилання youtubeMusic збережене без url — завантажити аудіо нізвідки"
        )

    classified = classify_url(url)
    video_id = classified.track_id
    if classified.kind is not UrlKind.TRACK or not is_valid_youtube_video_id(video_id):
        raise InvalidYouTubeVideoIdError(
            f"посилання youtubeMusic не дає валідного videoId: {url!r}"
        )

    assert video_id is not None
    return video_id


def build_watch_url(video_id: str) -> str:
    """Built by us from the validated id, never read back as a string from the
    database — the id is the only part of it a source ever supplied."""
    if not is_valid_youtube_video_id(video_id):
        raise InvalidYouTubeVideoIdError(f"videoId не пройшов валідацію: {video_id!r}")
    return f"https://www.youtube.com/watch?v={video_id}"


def read_ytdlp_version(*, timeout_seconds: float = VERSION_TIMEOUT_SECONDS) -> str:
    """Stored on the `audio_asset` row: when the extractor breaks, the version
    that was running is the first thing diagnosis needs (D-80)."""
    completed = run_media_tool_checked(YTDLP_BINARY, ["--version"], timeout_seconds=timeout_seconds)
    return completed.stdout.strip() or "unknown"


def build_ytdlp_arguments(url: str, output_template: str) -> list[str]:
    """An argument list, never a shell string. `--no-update` is explicit:
    a runtime self-update is prohibited (AS10), not merely not asked for."""
    return [
        "--no-update",
        "--no-playlist",
        "--no-progress",
        "--no-color",
        "--no-continue",
        "--no-cache-dir",
        "--newline",
        "--retries",
        "3",
        "--socket-timeout",
        "30",
        "--format",
        AUDIO_FORMAT_SELECTOR,
        "--output",
        output_template,
        "--",
        url,
    ]


def classify_ytdlp_failure(exit_code: int, stderr: str) -> AudioDownloadError:
    """Turns yt-dlp's stderr into the one distinction that matters: the
    extractor broke (every track fails) vs this track is gone (one does)."""
    tail = stderr_tail(stderr)
    lowered = tail.lower()

    if any(marker in lowered for marker in _EXTRACTOR_BROKEN_MARKERS):
        return YtdlpExtractorBrokenError(
            "yt-dlp не зміг витягти трек — схоже, зламався екстрактор YouTube, "
            "а не зник конкретний запис. Перевірте, чи падають ІНШІ треки теж: "
            f"якщо так, потрібне оновлення yt-dlp. Код {exit_code}: {tail}"
        )
    if any(marker in lowered for marker in _VIDEO_UNAVAILABLE_MARKERS):
        return YtdlpVideoUnavailableError(
            f"запис на YouTube недоступний (видалений, приватний або заблокований "
            f"за регіоном). Код {exit_code}: {tail}"
        )
    return AudioDownloadError(f"yt-dlp завершився з кодом {exit_code}: {tail}")


def download_audio(
    video_id: str, destination_directory: Path, *, timeout_seconds: float
) -> tuple[Path, str]:
    """Downloads into `destination_directory` (a tmp dir) and returns the
    file plus the yt-dlp version that produced it. Nothing is published here."""
    url = build_watch_url(video_id)
    ytdlp_version = read_ytdlp_version()

    destination_directory.mkdir(parents=True, exist_ok=True)
    output_template = str(destination_directory / f"{DOWNLOAD_STEM}.%(ext)s")

    logger.info("audio_download_started", video_id=video_id, ytdlp_version=ytdlp_version)
    completed = run_media_tool(
        YTDLP_BINARY,
        build_ytdlp_arguments(url, output_template),
        timeout_seconds=timeout_seconds,
    )

    if completed.returncode != 0:
        error = classify_ytdlp_failure(completed.returncode, completed.stderr)
        logger.error(
            "audio_download_failed",
            video_id=video_id,
            ytdlp_version=ytdlp_version,
            failure_class=type(error).__name__,
            exit_code=completed.returncode,
        )
        raise error

    downloaded = _locate_downloaded_file(destination_directory)
    logger.info(
        "audio_download_finished",
        video_id=video_id,
        path=str(downloaded),
        size_bytes=downloaded.stat().st_size,
    )
    return downloaded, ytdlp_version


def verify_downloaded_audio(path: Path, track_duration_ms: int | None) -> MediaProbe:
    """The loud check against a stub or a different recording (§4). The 23
    posts already in the database have `duration_ms IS NULL`, so that branch
    is explicit: the file is still probed, only the comparison is skipped."""
    probe = verify_media(path, require_audio=True)

    if track_duration_ms is None or track_duration_ms <= 0:
        logger.warning(
            "audio_duration_unverified",
            path=str(path),
            measured_duration_ms=probe.duration_ms,
            reason="track.duration_ms is unknown",
        )
        return probe

    difference_ms = abs(probe.duration_ms - track_duration_ms)
    if difference_ms > DURATION_TOLERANCE_MS:
        raise DownloadedAudioMismatchError(
            f"завантажене аудіо триває {probe.duration_ms} мс, а трек за даними "
            f"резолву — {track_duration_ms} мс (розбіжність {difference_ms} мс, "
            f"допуск {DURATION_TOLERANCE_MS} мс). Ймовірно, завантажено інший запис."
        )

    logger.info(
        "audio_duration_verified",
        measured_duration_ms=probe.duration_ms,
        expected_duration_ms=track_duration_ms,
        difference_ms=difference_ms,
    )
    return probe


def _locate_downloaded_file(destination_directory: Path) -> Path:
    candidates = sorted(
        path
        for path in destination_directory.glob(f"{DOWNLOAD_STEM}.*")
        if path.is_file() and not path.name.endswith(".part")
    )
    if not candidates:
        raise AudioDownloadError(
            f"yt-dlp завершився успішно, але файлу в {destination_directory} немає"
        )
    return candidates[0]
