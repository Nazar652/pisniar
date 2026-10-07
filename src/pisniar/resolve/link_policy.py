"""Which links may enter `track.platform_links`, and the one parser of the album link field.

Pure: no network, no database. An automatic candidate is checked for scheme and host
against the platform's own domains; a manual link additionally has to be a *track* link.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from pisniar.resolve.matching import is_valid_spotify_id, is_valid_youtube_video_id
from pisniar.resolve.urls import UrlKind, classify_url, is_within_length_limit

ALBUM_PLATFORM = "spotify"
AMAZON_TLDS = ("com", "co.uk", "de", "fr", "it", "es", "ca", "com.au", "co.jp", "in", "com.br")

# exact hostnames per platform key: the ones the sources returned, plus the platform's
# own alternates (apex, `www`, regional domains)
ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "spotify": frozenset({"open.spotify.com"}),
    "youtube": frozenset({"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"}),
    "youtubeMusic": frozenset({"music.youtube.com"}),
    "appleMusic": frozenset({"music.apple.com", "geo.music.apple.com"}),
    "itunes": frozenset({"itunes.apple.com"}),
    "deezer": frozenset({"www.deezer.com", "deezer.com"}),
    "tidal": frozenset({"listen.tidal.com", "tidal.com"}),
    "pandora": frozenset({"www.pandora.com", "pandora.com"}),
    "amazonMusic": frozenset(f"music.amazon.{tld}" for tld in AMAZON_TLDS),
    "amazonStore": frozenset(
        host for tld in AMAZON_TLDS for host in (f"amazon.{tld}", f"www.amazon.{tld}")
    ),
    "napster": frozenset({"play.napster.com", "napster.com", "www.napster.com"}),
    "anghami": frozenset({"play.anghami.com", "anghami.com", "www.anghami.com"}),
    "bandcamp": frozenset({"bandcamp.com"}),
    "soundcloud": frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}),
    "audius": frozenset({"audius.co"}),
    "boomplay": frozenset({"www.boomplay.com", "boomplay.com"}),
    "qobuz": frozenset({"open.qobuz.com", "play.qobuz.com", "www.qobuz.com"}),
    "spinrilla": frozenset({"spinrilla.com", "www.spinrilla.com"}),
    "audiomack": frozenset({"audiomack.com", "www.audiomack.com"}),
}
# every Bandcamp artist has its own subdomain
ALLOWED_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {"bandcamp": (".bandcamp.com",)}

# platforms whose track links `classify_url` recognises; a manual link must be one of them
CLASSIFIED_PLATFORMS = frozenset(
    {
        "spotify",
        "youtube",
        "youtubeMusic",
        "appleMusic",
        "deezer",
        "tidal",
        "bandcamp",
        "soundcloud",
    }
)
VIDEO_PLATFORMS = frozenset({"youtube", "youtubeMusic"})


@dataclass(frozen=True)
class LinkCheck:
    """`url` is the form to store; `problem` is a Ukrainian sentence, set when it is refused."""

    url: str | None
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(frozen=True)
class AlbumLinkCheck:
    album_id: str | None
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def host_of(url: str) -> str | None:
    """Lowercase hostname, or `None` when the URL carries a port or credentials."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if port is not None or parts.username is not None or parts.password is not None:
        return None
    return parts.hostname.lower() if parts.hostname else None


def host_is_allowed(platform: str, host: str) -> bool:
    if host in ALLOWED_HOSTS.get(platform, frozenset()):
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES.get(platform, ()))


def automatic_link_problem(platform: str, url: str) -> str | None:
    """Why an automatically found link may not be stored, or `None`."""
    if platform not in ALLOWED_HOSTS:
        return f"невідома платформа «{platform}»"
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return "посилання не розібрано"
    if scheme != "https":
        return "не https"
    host = host_of(url)
    if host is None:
        return "некоректний хост"
    if not host_is_allowed(platform, host):
        return f"хост {host} не належить платформі"
    if not is_within_length_limit(url):
        return "посилання задовге"
    return None


def check_manual_link(platform: str, raw_url: str) -> LinkCheck:
    """A hand-entered link: https, this platform's host, bounded, and a track link."""
    url = raw_url.strip()
    problem = automatic_link_problem(platform, url)
    if problem is not None:
        return LinkCheck(None, f"Посилання не підходить: {problem}.")

    if platform not in CLASSIFIED_PLATFORMS:
        return LinkCheck(url)

    classified = classify_url(url)
    if classified.kind is not UrlKind.TRACK:
        return LinkCheck(None, "Це не посилання на трек (альбом, артист чи плейлист не підходять).")
    if classified.platform != platform:
        return LinkCheck(None, "Посилання веде на іншу платформу.")
    if platform in VIDEO_PLATFORMS and not is_valid_youtube_video_id(classified.track_id):
        return LinkCheck(None, "У посиланні некоректний ідентифікатор відео.")
    if not is_within_length_limit(classified.normalized_url):
        return LinkCheck(None, "Посилання задовге.")
    return LinkCheck(classified.normalized_url)


def parse_spotify_album_link(raw_url: str) -> AlbumLinkCheck:
    """`https://open.spotify.com/[intl-xx/]album/<id>` to the id; the id is what is stored."""
    url = raw_url.strip()
    if not url:
        return AlbumLinkCheck(None, "Порожнє посилання.")
    if host_of(url) != "open.spotify.com" or urlsplit(url).scheme.lower() != "https":
        return AlbumLinkCheck(None, "Потрібне посилання вигляду https://open.spotify.com/album/…")

    classified = classify_url(url)
    if classified.kind is not UrlKind.NOT_TRACK or classified.not_track_reason != "album":
        return AlbumLinkCheck(None, "Це не посилання на альбом Spotify.")

    album_id = urlsplit(classified.normalized_url).path.rsplit("/", 1)[-1]
    if not is_valid_spotify_id(album_id):
        return AlbumLinkCheck(None, "У посиланні некоректний ідентифікатор альбому.")
    return AlbumLinkCheck(album_id)
