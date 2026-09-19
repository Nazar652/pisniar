"""Top-level orchestration (contract p.27 addendum): resolve the input's
own song.link entity, bridge YouTube input to Spotify when needed, then
fan out ISRC-exact and name-search sources. The name-search fan-out always
runs — even from a Spotify input, YouTube never arrives any other way (D-27).
"""

from __future__ import annotations

from pisniar.logging import get_logger
from pisniar.resolve import matching, merge
from pisniar.resolve.genres import deezer_genre_name
from pisniar.resolve.registry import ResolveContext, resolve_by_isrc, resolve_by_name_with_matching
from pisniar.resolve.sources import deezer, songlink, spotify
from pisniar.resolve.types import GenreInfo, MatchClass, PlatformLink, ResolvedTrack
from pisniar.resolve.urls import ClassifiedUrl

logger = get_logger(__name__)


async def resolve_track(context: ResolveContext, classified_input: ClassifiedUrl) -> ResolvedTrack:
    track = ResolvedTrack()

    if classified_input.platform == "youtube":
        # The clip the admin sent stays exactly as given — never overwritten
        # by an automatically found `videoId` (contract addendum, rule 1/4).
        link = PlatformLink(
            url=classified_input.normalized_url, source="user_input", match=MatchClass.MANUAL
        )
        merge.merge_platform_link(track, "youtube", link)

    songlink_result = await songlink.fetch(
        context.http_client, context.cache, classified_input.normalized_url
    )
    _apply_songlink_result(track, songlink_result)

    if songlink_result.isrc:
        await resolve_by_isrc(context, songlink_result.isrc, track)
    elif songlink_result.provider == "youtube" and track.title and track.artists:
        await _bridge_youtube_to_spotify(context, track)

    if track.title and track.artists:
        reference_artist = ", ".join(artist.name for artist in track.artists)
        await resolve_by_name_with_matching(
            context, reference_artist, track.title, track.duration_ms, track
        )
    else:
        logger.warning(
            "resolve_track_missing_reference_metadata", url=classified_input.normalized_url
        )
        track.warnings.append("Недостатньо метаданих (артист/назва) для пошуку за назвою")

    return track


async def resolve_genres(context: ResolveContext, track: ResolvedTrack) -> list[GenreInfo]:
    """Deezer album genres, keyed by `track.deezer_album_id` — set only by
    the ISRC-exact path (contract p.9/D-33). No album id -> no genres, the
    genre line simply stays out of the caption rather than guessing."""
    if track.deezer_album_id is None:
        return []

    album_result = await deezer.fetch_album(
        context.http_client, context.cache, track.deezer_album_id
    )
    if not album_result.found:
        return []

    genres: list[GenreInfo] = []
    for genre_id in album_result.genre_ids:
        english_name = album_result.genre_english_names.get(genre_id, "")
        ukrainian_name = deezer_genre_name(genre_id, english_name)
        if ukrainian_name is not None:
            genres.append(GenreInfo(name=ukrainian_name, hashtag=None, source="deezer"))
    return genres


async def _bridge_youtube_to_spotify(context: ResolveContext, track: ResolvedTrack) -> None:
    """From a YouTube entity (dirty title, no ISRC), search Spotify by
    name; once matched, its own ISRC drives the exact-ISRC fan-out."""
    reference_artist = ", ".join(artist.name for artist in track.artists)
    assert track.title is not None
    candidates = await spotify.search_by_name(
        context.http_client,
        context.cache,
        reference_artist,
        track.title,
        context.spotify_client_id,
        context.spotify_client_secret,
    )

    for candidate in candidates:
        if matching.is_match(
            reference_artist=reference_artist,
            reference_title=track.title,
            reference_duration_ms=track.duration_ms,
            candidate_artist=candidate.artist_name,
            candidate_title=candidate.title,
            candidate_duration_ms=candidate.duration_ms,
        ):
            if candidate.isrc:
                await resolve_by_isrc(context, candidate.isrc, track)
            if candidate.url:
                # song.link on the *bridged* Spotify URL gives the same
                # streaming cluster a direct Spotify input would (D-27).
                bridged_result = await songlink.fetch(
                    context.http_client, context.cache, candidate.url
                )
                _apply_songlink_result(track, bridged_result)
            return

    if candidates:
        track.warnings.append(
            f"spotify: {len(candidates)} кандидат(и) відхилено звіркою "
            f"для «{reference_artist} — {track.title}»"
        )


def _apply_songlink_result(track: ResolvedTrack, result: songlink.SonglinkResult) -> None:
    if not result.found:
        return

    source_tag = f"songlink:{result.provider}" if result.provider else "songlink:unknown"
    if result.provider == "spotify":
        merge.merge_isrc(track, result.isrc, source_tag)
        merge.merge_year(track, result.year, "songlink")

    merge.merge_title(track, result.title, source_tag)
    if result.artist_name:
        merge.merge_artists(track, [result.artist_name], source_tag)
    merge.merge_cover_url(track, result.cover_url, source_tag)

    if track.duration_ms is None:
        track.duration_ms = result.duration_ms
    if result.album_id_spotify and track.album_id_spotify is None:
        track.album_id_spotify = result.album_id_spotify

    for platform, url in result.platform_links.items():
        # AGGREGATOR, not EXACT_ISRC (D-39): we trust Odesli's clustering
        # here, we never verified the identifier ourselves.
        link = PlatformLink(url=url, source="songlink", match=MatchClass.AGGREGATOR)
        merge.merge_platform_link(track, platform, link)
