"""`service.resolve_track()`: the top-level orchestration — YouTube-input lock, ISRC-first dispatch, and the
YouTube<->Spotify bridge. Source calls are faked so this tests only the sequencing, not any single adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pisniar.resolve import service
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.registry import ResolveContext
from pisniar.resolve.sources.deezer import DeezerAlbumResult
from pisniar.resolve.sources.songlink import SonglinkResult
from pisniar.resolve.types import MatchClass, PlatformLink, ResolvedTrack, TrackCandidate
from pisniar.resolve.urls import classify_url


def make_context() -> ResolveContext:
    return ResolveContext(
        http_client=AsyncMock(),
        cache=AsyncMock(spec=SourceCache),
        spotify_client_id="id",
        spotify_client_secret="secret",
        itunes_country="UA",
        musicbrainz_contact=None,
    )


async def test_youtube_input_locks_youtube_slot_as_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.songlink, "fetch", AsyncMock(return_value=SonglinkResult(found=False)))
    monkeypatch.setattr(service, "resolve_by_name_with_matching", AsyncMock())

    classified = classify_url("https://www.youtube.com/watch?v=userClipId")
    track = await service.resolve_track(make_context(), classified)

    assert track.platform_links["youtube"].source == "user_input"
    assert track.platform_links["youtube"].match is MatchClass.MANUAL
    assert track.platform_links["youtube"].url == classified.normalized_url


async def test_spotify_input_with_isrc_triggers_isrc_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    songlink_result = SonglinkResult(
        found=True,
        provider="spotify",
        isrc="GBAYE9200070",
        title="Creep",
        artist_name="Radiohead",
        duration_ms=238000,
    )
    monkeypatch.setattr(service.songlink, "fetch", AsyncMock(return_value=songlink_result))
    resolve_by_isrc_mock = AsyncMock()
    monkeypatch.setattr(service, "resolve_by_isrc", resolve_by_isrc_mock)
    monkeypatch.setattr(service, "resolve_by_name_with_matching", AsyncMock())

    classified = classify_url("https://open.spotify.com/track/70LcF31zb1H0PyJoS1Sx1r")
    await service.resolve_track(make_context(), classified)

    resolve_by_isrc_mock.assert_awaited_once()
    assert resolve_by_isrc_mock.await_args is not None
    assert resolve_by_isrc_mock.await_args.args[1] == "GBAYE9200070"


async def test_spotify_input_still_runs_name_search_fallback_for_youtube(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """even from a Spotify input, YouTube never arrives from song.link, so the
    trailing name-search fan-out must still run."""
    songlink_result = SonglinkResult(
        found=True, provider="spotify", isrc="X", title="T", artist_name="A", duration_ms=1000
    )
    monkeypatch.setattr(service.songlink, "fetch", AsyncMock(return_value=songlink_result))
    monkeypatch.setattr(service, "resolve_by_isrc", AsyncMock())
    name_search_mock = AsyncMock()
    monkeypatch.setattr(service, "resolve_by_name_with_matching", name_search_mock)

    classified = classify_url("https://open.spotify.com/track/70LcF31zb1H0PyJoS1Sx1r")
    await service.resolve_track(make_context(), classified)

    name_search_mock.assert_awaited_once()
    assert name_search_mock.await_args is not None
    assert name_search_mock.await_args.args[1] == "A"
    assert name_search_mock.await_args.args[2] == "T"


async def test_youtube_input_without_isrc_bridges_to_spotify_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """re-resolving song.link on the bridged Spotify URL gives a YouTube input the
    same streaming cluster a Spotify input gets, not just the ISRC-exact sources."""
    youtube_entity_result = SonglinkResult(
        found=True,
        provider="youtube",
        isrc=None,
        title="Purely Video Title",
        artist_name="Burned Time Machine",
        duration_ms=225000,
    )
    bridged_spotify_entity_result = SonglinkResult(
        found=True,
        provider="spotify",
        platform_links={"pandora": "https://www.pandora.com/TR:1"},
    )

    def fetch_side_effect(
        client: object,
        session: object,
        url: str,
    ) -> SonglinkResult:
        if url == "https://open.spotify.com/track/x":
            return bridged_spotify_entity_result
        return youtube_entity_result

    songlink_fetch_mock = AsyncMock(side_effect=fetch_side_effect)
    monkeypatch.setattr(service.songlink, "fetch", songlink_fetch_mock)

    matched_candidate = TrackCandidate(
        platform="spotify",
        url="https://open.spotify.com/track/x",
        source="spotify",
        artist_name="Burned Time Machine",
        title="Purely Video Title",
        duration_ms=224000,
        isrc="BRIDGEDISRC01",
    )
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(return_value=[matched_candidate]))
    resolve_by_isrc_mock = AsyncMock()
    monkeypatch.setattr(service, "resolve_by_isrc", resolve_by_isrc_mock)
    monkeypatch.setattr(service, "resolve_by_name_with_matching", AsyncMock())

    classified = classify_url("https://www.youtube.com/watch?v=someId")
    track = await service.resolve_track(make_context(), classified)

    resolve_by_isrc_mock.assert_awaited_once()
    assert resolve_by_isrc_mock.await_args is not None
    assert resolve_by_isrc_mock.await_args.args[1] == "BRIDGEDISRC01"
    assert songlink_fetch_mock.await_count == 2
    assert track.platform_links["pandora"].url == "https://www.pandora.com/TR:1"


async def test_youtube_bridge_rejected_candidate_adds_warning_not_isrc_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    songlink_result = SonglinkResult(
        found=True,
        provider="youtube",
        isrc=None,
        title="Video Title",
        artist_name="Some Artist",
        duration_ms=225000,
    )
    monkeypatch.setattr(service.songlink, "fetch", AsyncMock(return_value=songlink_result))

    wrong_candidate = TrackCandidate(
        platform="spotify",
        url="https://open.spotify.com/track/wrong",
        source="spotify",
        artist_name="Completely Different Artist",
        title="Unrelated Song",
        duration_ms=999999,
        isrc="WRONGISRC0001",
    )
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(return_value=[wrong_candidate]))
    resolve_by_isrc_mock = AsyncMock()
    monkeypatch.setattr(service, "resolve_by_isrc", resolve_by_isrc_mock)
    monkeypatch.setattr(service, "resolve_by_name_with_matching", AsyncMock())

    classified = classify_url("https://www.youtube.com/watch?v=someId")
    track = await service.resolve_track(make_context(), classified)

    resolve_by_isrc_mock.assert_not_awaited()
    assert any("spotify" in warning for warning in track.warnings)


async def test_missing_reference_metadata_adds_warning_and_skips_name_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.songlink, "fetch", AsyncMock(return_value=SonglinkResult(found=False)))
    name_search_mock = AsyncMock()
    monkeypatch.setattr(service, "resolve_by_name_with_matching", name_search_mock)

    classified = classify_url("https://www.deezer.com/track/12345")
    track = await service.resolve_track(make_context(), classified)

    name_search_mock.assert_not_awaited()
    assert any("метадан" in warning for warning in track.warnings)


def test_apply_songlink_result_sets_duration_only_once() -> None:
    track = ResolvedTrack(duration_ms=111)
    result = SonglinkResult(found=True, provider="spotify", duration_ms=222)

    service.apply_songlink_result(track, result)

    assert track.duration_ms == 111


def test_apply_songlink_result_populates_platform_links_as_songlink_source() -> None:
    track = ResolvedTrack()
    result = SonglinkResult(
        found=True,
        provider="spotify",
        platform_links={"tidal": "https://listen.tidal.com/track/1"},
    )

    service.apply_songlink_result(track, result)

    assert track.platform_links["tidal"].source == "songlink"
    assert track.platform_links["tidal"].match is MatchClass.AGGREGATOR


def test_apply_songlink_result_does_not_set_isrc_from_youtube_entity() -> None:
    track = ResolvedTrack()
    result = SonglinkResult(found=True, provider="youtube", isrc="SHOULD-NOT-APPLY", title="T")

    service.apply_songlink_result(track, result)

    assert track.isrc is None


async def test_resolve_genres_returns_empty_without_a_deezer_album_id() -> None:
    track = ResolvedTrack()

    genres = await service.resolve_genres(make_context(), track)

    assert genres == []


async def test_resolve_genres_maps_known_ids_and_skips_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    album_result = DeezerAlbumResult(
        found=True,
        genre_ids=[152, 464, 999999],
        genre_english_names={152: "Rock", 464: "Metal", 999999: "Some New Genre"},
    )
    monkeypatch.setattr(service.deezer, "fetch_album", AsyncMock(return_value=album_result))

    track = ResolvedTrack(deezer_album_id=14880711)
    genres = await service.resolve_genres(make_context(), track)

    assert [genre.name for genre in genres] == ["рок", "метал"]
    assert all(genre.source == "deezer" for genre in genres)


async def test_resolve_genres_returns_empty_when_album_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.deezer, "fetch_album", AsyncMock(return_value=DeezerAlbumResult(found=False)))

    track = ResolvedTrack(deezer_album_id=1)
    genres = await service.resolve_genres(make_context(), track)

    assert genres == []


def test_apply_songlink_result_replaces_a_zero_duration() -> None:
    """song.link's YouTube entity answers `duration: 0`, which is not `None`; kept,
    it would discard the real Spotify duration and switch the duration check off."""
    track = ResolvedTrack(duration_ms=0)
    result = SonglinkResult(found=True, provider="spotify", duration_ms=293133)

    service.apply_songlink_result(track, result)

    assert track.duration_ms == 293133


def test_apply_songlink_result_never_writes_a_zero_duration() -> None:
    track = ResolvedTrack()
    result = SonglinkResult(found=True, provider="youtube", duration_ms=0)

    service.apply_songlink_result(track, result)

    assert track.duration_ms is None


async def test_a_failing_bridged_songlink_keeps_what_the_isrc_fan_out_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """by then the ISRC fan-out has filled the track, so raising here would throw it
    all away. The primary fetch stays loud on purpose."""
    youtube_entity_result = SonglinkResult(
        found=True,
        provider="youtube",
        title="Purely Video Title",
        artist_name="Burned Time Machine",
        duration_ms=225000,
    )

    def fetch_side_effect(
        client: object,
        cache: object,
        url: str,
    ) -> SonglinkResult:
        if url == "https://open.spotify.com/track/x":
            raise RuntimeError("odesli quota exceeded")
        return youtube_entity_result

    monkeypatch.setattr(service.songlink, "fetch", AsyncMock(side_effect=fetch_side_effect))

    matched_candidate = TrackCandidate(
        platform="spotify",
        url="https://open.spotify.com/track/x",
        source="spotify",
        artist_name="Burned Time Machine",
        title="Purely Video Title",
        duration_ms=224000,
        isrc="BRIDGEDISRC01",
    )
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(return_value=[matched_candidate]))

    async def fill_from_isrc(
        context: object,
        isrc: str,
        track: ResolvedTrack,
    ) -> None:
        track.platform_links["deezer"] = PlatformLink(
            url="https://www.deezer.com/track/1", source="deezer", match=MatchClass.EXACT_ISRC
        )

    monkeypatch.setattr(service, "resolve_by_isrc", AsyncMock(side_effect=fill_from_isrc))
    monkeypatch.setattr(service, "resolve_by_name_with_matching", AsyncMock())

    classified = classify_url("https://www.youtube.com/watch?v=someId")
    track = await service.resolve_track(make_context(), classified)

    assert track.platform_links["deezer"].url == "https://www.deezer.com/track/1"
    assert any("odesli quota exceeded" in failure for failure in track.source_failures)


async def test_a_failing_spotify_bridge_leaves_the_raw_youtube_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the isolated bridge lets the resolve survive this failure, so the uploader's
    name and clip title are what reaches `persist_track`."""
    monkeypatch.setattr(
        service.songlink,
        "fetch",
        AsyncMock(
            return_value=SonglinkResult(
                found=True,
                provider="youtube",
                isrc=None,
                title="Bruise Pristine (Official Video)",
                artist_name="Валентин Стрыкало-Бувайте",
                duration_ms=225000,
            )
        ),
    )
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(side_effect=RuntimeError("HTTP 503")))
    monkeypatch.setattr(service, "resolve_by_name_with_matching", AsyncMock())

    track = await service.resolve_track(make_context(), classify_url("https://www.youtube.com/watch?v=someId"))

    assert track.title == "Bruise Pristine (Official Video)"
    assert [artist.name for artist in track.artists] == ["Валентин Стрыкало-Бувайте"]
    assert any(failure.startswith("spotify:bridge: ") for failure in track.source_failures)
