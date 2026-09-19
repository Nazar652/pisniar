"""ISRC-first fan-out (contract p.12, the heart of the decision): exact-ISRC
sources write `platform_links` unconditionally; name-search sources must
pass `matching.is_match()` or the candidate is dropped with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from pisniar.logging import get_logger
from pisniar.resolve import matching, merge
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import deezer, itunes, musicbrainz, spotify, ytmusic
from pisniar.resolve.types import MatchClass, PlatformLink, ResolvedTrack, TrackCandidate
from pisniar.resolve.urls import classify_url

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolveContext:
    http_client: httpx.AsyncClient
    cache: SourceCache
    spotify_client_id: str
    spotify_client_secret: str
    itunes_country: str
    musicbrainz_contact: str | None


async def resolve_by_isrc(context: ResolveContext, isrc: str, track: ResolvedTrack) -> None:
    """Fan out to every ISRC-exact source. No `matching` call anywhere in
    this function — the identifier is the proof (accuracy-class test 1)."""
    deezer_result = await deezer.fetch_by_isrc(context.http_client, context.cache, isrc)
    if deezer_result.found:
        _apply_deezer_track_result(track, deezer_result)
        if deezer_result.album_id is not None:
            album_result = await deezer.fetch_album(
                context.http_client, context.cache, deezer_result.album_id
            )
            _apply_deezer_album_result(track, deezer_result.album_id, album_result)

    spotify_result = await spotify.search_by_isrc(
        context.http_client,
        context.cache,
        isrc,
        context.spotify_client_id,
        context.spotify_client_secret,
    )
    if spotify_result.found:
        _apply_spotify_track_result(track, spotify_result)

    musicbrainz_result = await musicbrainz.fetch_by_isrc(
        context.http_client, context.cache, isrc, context.musicbrainz_contact
    )
    if musicbrainz_result.found:
        for platform, url in musicbrainz_result.platform_links.items():
            merge.merge_platform_link(
                track,
                platform,
                PlatformLink(url=url, source="musicbrainz", match=MatchClass.EXACT_ISRC),
            )


async def resolve_by_name_with_matching(
    context: ResolveContext,
    artist: str,
    title: str,
    duration_ms: int | None,
    track: ResolvedTrack,
) -> None:
    """Name-search fallback sources — each candidate must pass `matching`
    or it never reaches `platform_links` (accuracy-class test 2)."""
    if "deezer" not in track.platform_links:
        # Fallback only used when the exact-ISRC lookup found nothing for
        # Deezer (accuracy-class test 4) — checked, not merely hoped for.
        candidates = await deezer.search_by_name(context.http_client, context.cache, artist, title)
        _match_and_merge(track, "deezer", candidates, artist, title, duration_ms, "deezer:search")

    itunes_candidates = await itunes.search_by_name(
        context.http_client, context.cache, artist, title, context.itunes_country
    )
    _match_and_merge(track, "appleMusic", itunes_candidates, artist, title, duration_ms, "itunes")

    ytmusic_candidates = await ytmusic.search_by_name(context.cache, artist, title)
    matched = _match_and_merge(
        track, "youtubeMusic", ytmusic_candidates, artist, title, duration_ms, "ytmusic"
    )
    if matched is not None:
        video_id = classify_url(matched.url).track_id
        if video_id:
            derived_youtube_url = f"https://www.youtube.com/watch?v={video_id}"
            merge.merge_platform_link(
                track,
                "youtube",
                PlatformLink(url=derived_youtube_url, source="ytmusic", match=MatchClass.CONFIRMED),
            )


def _match_and_merge(
    track: ResolvedTrack,
    platform: str,
    candidates: list[TrackCandidate],
    artist: str,
    title: str,
    duration_ms: int | None,
    source_label: str,
) -> TrackCandidate | None:
    for candidate in candidates:
        if matching.is_match(
            reference_artist=artist,
            reference_title=title,
            reference_duration_ms=duration_ms,
            candidate_artist=candidate.artist_name,
            candidate_title=candidate.title,
            candidate_duration_ms=candidate.duration_ms,
        ):
            merge.merge_platform_link(
                track,
                platform,
                PlatformLink(
                    url=candidate.url, source=candidate.source, match=MatchClass.CONFIRMED
                ),
            )
            return candidate

    if candidates:
        logger.warning(
            "name_search_candidates_rejected_by_matching",
            source=source_label,
            platform=platform,
            candidate_count=len(candidates),
            reference_artist=artist,
            reference_title=title,
        )
        track.warnings.append(
            f"{source_label}: {len(candidates)} кандидат(и) відхилено звіркою "
            f"для «{artist} — {title}»"
        )
    return None


def _apply_deezer_track_result(track: ResolvedTrack, result: deezer.DeezerTrackResult) -> None:
    if result.url:
        merge.merge_platform_link(
            track,
            "deezer",
            PlatformLink(url=result.url, source="deezer", match=MatchClass.EXACT_ISRC),
        )
    merge.merge_title(track, result.title, "deezer")
    if result.artist_name:
        merge.merge_artists(track, [result.artist_name], "deezer")


_DEEZER_RECORD_TYPE_NORMALIZATION = {"compile": "compilation"}


def _apply_deezer_album_result(
    track: ResolvedTrack, album_id: int, result: deezer.DeezerAlbumResult
) -> None:
    """Merges year/release type only — the genre list itself is built
    separately by `service.resolve_genres()` (contract p.9)."""
    if not result.found:
        return

    track.deezer_album_id = album_id
    if result.release_date and len(result.release_date) >= 4:
        merge.merge_year(track, int(result.release_date[:4]), "deezer")
    if result.record_type:
        record_type = _DEEZER_RECORD_TYPE_NORMALIZATION.get(result.record_type, result.record_type)
        merge.merge_release_type(track, record_type, "deezer")


def _apply_spotify_track_result(track: ResolvedTrack, result: spotify.SpotifyTrackResult) -> None:
    if result.url:
        merge.merge_platform_link(
            track,
            "spotify",
            PlatformLink(url=result.url, source="spotify", match=MatchClass.EXACT_ISRC),
        )
    merge.merge_isrc(track, result.isrc, "spotify")
    merge.merge_title(track, result.title, "spotify")
    merge.merge_artists(track, result.artist_names, "spotify")
    merge.merge_year(track, result.year, "spotify")
    merge.merge_release_type(track, result.release_type, "spotify")
    merge.merge_release_title(track, result.release_title, "spotify")
    if result.album_id and track.album_id_spotify is None:
        track.album_id_spotify = result.album_id
