"""The two loops that turn name-search candidates into links: `registry.match_and_merge`
and `service.bridge_youtube_to_spotify`. Matching is never fuzzy, and a rejected
candidate is audible as a warning instead of silently dropped.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from pisniar.resolve import registry, service
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.registry import ResolveContext
from pisniar.resolve.sources.songlink import SonglinkResult
from pisniar.resolve.types import ArtistInfo, MatchClass, ResolvedTrack, TrackCandidate


def make_candidate(
    artist: str = "Radiohead",
    title: str = "Creep",
    duration_ms: int | None = 238_000,
    url: str = "https://www.deezer.com/track/1",
    isrc: str | None = None,
) -> TrackCandidate:
    return TrackCandidate(
        platform="deezer",
        url=url,
        source="deezer",
        artist_name=artist,
        title=title,
        duration_ms=duration_ms,
        isrc=isrc,
    )


def match(track: ResolvedTrack, candidates: list[TrackCandidate]) -> TrackCandidate | None:
    return registry.match_and_merge(track, "deezer", candidates, "Radiohead", "Creep", 238_000, "deezer:search")


def test_a_matching_candidate_becomes_a_confirmed_link_and_is_returned() -> None:
    track = ResolvedTrack()
    candidate = make_candidate()

    assert match(track, [candidate]) is candidate

    link = track.platform_links["deezer"]
    assert (link.url, link.source, link.match) == (candidate.url, "deezer", MatchClass.CONFIRMED)
    assert track.warnings == []


def test_the_first_matching_candidate_wins_over_later_ones() -> None:
    track = ResolvedTrack()
    wrong = make_candidate(title="Creep (Live)", url="https://www.deezer.com/track/9")
    first = make_candidate(url="https://www.deezer.com/track/1")
    second = make_candidate(url="https://www.deezer.com/track/2")

    assert match(track, [wrong, first, second]) is first
    assert track.platform_links["deezer"].url == first.url


def test_rejected_candidates_leave_no_link_but_a_warning_and_a_log_line() -> None:
    track = ResolvedTrack()
    candidates = [make_candidate(artist="Other"), make_candidate(title="Other Song")]

    with structlog.testing.capture_logs() as captured:
        assert match(track, candidates) is None

    assert track.platform_links == {}
    assert track.warnings == ["deezer:search: 2 кандидат(и) відхилено звіркою для «Radiohead — Creep»"]
    rejected = [entry for entry in captured if entry["event"] == "name_search_candidates_rejected_by_matching"]
    assert rejected[0]["candidate_count"] == 2
    assert rejected[0]["platform"] == "deezer"


def test_no_candidates_means_no_warning() -> None:
    track = ResolvedTrack()

    assert match(track, []) is None
    assert track.warnings == []


def test_a_duration_far_from_the_reference_is_a_mismatch() -> None:
    track = ResolvedTrack()

    assert match(track, [make_candidate(duration_ms=60_000)]) is None
    assert len(track.warnings) == 1


def test_an_unknown_candidate_duration_does_not_block_a_match() -> None:
    track = ResolvedTrack()

    assert match(track, [make_candidate(duration_ms=None)]) is not None


def make_context() -> ResolveContext:
    return ResolveContext(
        http_client=AsyncMock(),
        cache=AsyncMock(spec=SourceCache),
        spotify_client_id="id",
        spotify_client_secret="secret",
        itunes_country="UA",
        musicbrainz_contact=None,
    )


def make_youtube_track() -> ResolvedTrack:
    track = ResolvedTrack(title="Creep", duration_ms=238_000)
    track.artists = [ArtistInfo(name="Radiohead")]
    return track


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    fan_out = AsyncMock()
    songlink_fetch = AsyncMock(return_value=SonglinkResult(found=False))
    monkeypatch.setattr(service, "resolve_by_isrc", fan_out)
    monkeypatch.setattr(service.songlink, "fetch", songlink_fetch)
    return SimpleNamespace(fan_out=fan_out, songlink_fetch=songlink_fetch)


def spotify_candidate(
    artist_name: str = "Radiohead",
    title: str = "Creep",
    isrc: str | None = "GBAYE9200070",
) -> TrackCandidate:
    return TrackCandidate(
        platform="spotify",
        url="https://open.spotify.com/track/x",
        source="spotify",
        artist_name=artist_name,
        title=title,
        duration_ms=238_000,
        isrc=isrc,
    )


async def test_a_matched_spotify_candidate_drives_the_isrc_fan_out_and_the_songlink_fetch(
    monkeypatch: pytest.MonkeyPatch, bridge: SimpleNamespace
) -> None:
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(return_value=[spotify_candidate()]))
    track = make_youtube_track()

    await service.bridge_youtube_to_spotify(make_context(), track)

    assert bridge.fan_out.await_args.args[1] == "GBAYE9200070"
    bridge.songlink_fetch.assert_awaited_once()
    assert bridge.songlink_fetch.await_args.args[2] == "https://open.spotify.com/track/x"
    assert track.warnings == []


async def test_a_candidate_without_an_isrc_still_gets_its_songlink_cluster(
    monkeypatch: pytest.MonkeyPatch, bridge: SimpleNamespace
) -> None:
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(return_value=[spotify_candidate(isrc=None)]))

    await service.bridge_youtube_to_spotify(make_context(), make_youtube_track())

    bridge.fan_out.assert_not_awaited()
    bridge.songlink_fetch.assert_awaited_once()


async def test_rejected_spotify_candidates_leave_a_warning_and_no_fan_out(
    monkeypatch: pytest.MonkeyPatch, bridge: SimpleNamespace
) -> None:
    monkeypatch.setattr(
        service.spotify,
        "search_by_name",
        AsyncMock(return_value=[spotify_candidate(title="Other"), spotify_candidate(artist_name="X")]),
    )
    track = make_youtube_track()

    await service.bridge_youtube_to_spotify(make_context(), track)

    bridge.fan_out.assert_not_awaited()
    bridge.songlink_fetch.assert_not_awaited()
    assert track.warnings == ["spotify: 2 кандидат(и) відхилено звіркою для «Radiohead — Creep»"]


async def test_no_spotify_candidates_leave_the_track_untouched(
    monkeypatch: pytest.MonkeyPatch, bridge: SimpleNamespace
) -> None:
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(return_value=[]))
    track = make_youtube_track()

    await service.bridge_youtube_to_spotify(make_context(), track)

    bridge.fan_out.assert_not_awaited()
    assert track.warnings == []


async def test_a_failing_spotify_search_is_recorded_as_a_source_failure(
    monkeypatch: pytest.MonkeyPatch, bridge: SimpleNamespace
) -> None:
    monkeypatch.setattr(service.spotify, "search_by_name", AsyncMock(side_effect=RuntimeError("quota")))
    track = make_youtube_track()

    await service.bridge_youtube_to_spotify(make_context(), track)

    assert any(failure.startswith("spotify:bridge: ") for failure in track.source_failures)
    bridge.fan_out.assert_not_awaited()
