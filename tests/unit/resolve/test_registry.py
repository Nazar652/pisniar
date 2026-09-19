"""The four required accuracy-class tests (contract verification section) —
the heart of "ISRC-first": exact-ISRC writes bypass `matching` entirely,
a rejected name-search candidate never reaches `platform_links`, and the
Deezer name-search fallback is skipped once `deezer:isrc` already answered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from pisniar.resolve import registry
from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources.deezer import DeezerAlbumResult, DeezerTrackResult
from pisniar.resolve.sources.musicbrainz import MusicBrainzRecordingResult
from pisniar.resolve.sources.spotify import SpotifyTrackResult
from pisniar.resolve.types import MatchClass, ResolvedTrack, TrackCandidate


def make_context() -> registry.ResolveContext:
    return registry.ResolveContext(
        http_client=AsyncMock(),
        cache=AsyncMock(spec=SourceCache),
        spotify_client_id="id",
        spotify_client_secret="secret",
        itunes_country="UA",
        musicbrainz_contact=None,
    )


async def test_deezer_isrc_link_is_written_without_calling_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accuracy-class test 1: `deezer:isrc` records the link with zero
    calls into `matching` — the identifier alone is the proof."""
    matching_was_called = False

    def spy_is_match(**kwargs: object) -> bool:
        nonlocal matching_was_called
        matching_was_called = True
        return True

    monkeypatch.setattr(registry.matching, "is_match", spy_is_match)
    monkeypatch.setattr(
        registry.deezer,
        "fetch_by_isrc",
        AsyncMock(
            return_value=DeezerTrackResult(
                found=True, url="https://www.deezer.com/track/1", title="X", artist_name="Y"
            )
        ),
    )
    monkeypatch.setattr(
        registry.spotify, "search_by_isrc", AsyncMock(return_value=SpotifyTrackResult(found=False))
    )
    monkeypatch.setattr(
        registry.musicbrainz,
        "fetch_by_isrc",
        AsyncMock(return_value=MusicBrainzRecordingResult(found=False)),
    )

    track = ResolvedTrack()
    await registry.resolve_by_isrc(make_context(), "GBAYE9200070", track)

    assert track.platform_links["deezer"].url == "https://www.deezer.com/track/1"
    assert track.platform_links["deezer"].match is MatchClass.EXACT_ISRC
    assert matching_was_called is False


async def test_rejected_itunes_candidate_never_reaches_platform_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accuracy-class test 2: a candidate that fails `matching` is dropped,
    not written — and a warning is recorded so the failure isn't silent."""
    wrong_candidate = TrackCandidate(
        platform="appleMusic",
        url="https://music.apple.com/wrong",
        source="itunes",
        artist_name="A Completely Different Artist",
        title="An Unrelated Song",
        duration_ms=999999,
    )
    monkeypatch.setattr(registry.deezer, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        registry.itunes, "search_by_name", AsyncMock(return_value=[wrong_candidate])
    )
    monkeypatch.setattr(registry.ytmusic, "search_by_name", AsyncMock(return_value=[]))

    track = ResolvedTrack()
    await registry.resolve_by_name_with_matching(
        make_context(), "Krovna", "Berserker", 263000, track
    )

    assert "appleMusic" not in track.platform_links
    assert len(track.warnings) == 1
    assert "itunes" in track.warnings[0]


async def test_deezer_search_fallback_is_skipped_when_isrc_already_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accuracy-class test 4: `deezer:search` must not even be called once
    `deezer:isrc` already populated the `deezer` slot."""
    search_by_name_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(registry.deezer, "search_by_name", search_by_name_mock)
    monkeypatch.setattr(registry.itunes, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(registry.ytmusic, "search_by_name", AsyncMock(return_value=[]))

    track = ResolvedTrack()
    track.platform_links["deezer"] = registry.PlatformLink(
        url="https://www.deezer.com/track/1", source="deezer", match=MatchClass.EXACT_ISRC
    )
    track.platform_link_priorities["deezer"] = registry.merge.platform_link_priority(
        track.platform_links["deezer"]
    )

    await registry.resolve_by_name_with_matching(
        make_context(), "Burned Time Machine", "Покаяння", None, track
    )

    search_by_name_mock.assert_not_called()


async def test_deezer_search_fallback_runs_when_isrc_lookup_found_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric check for test 4: the fallback does fire when the exact-ISRC
    slot is genuinely empty — proving the skip above is conditional, not dead code."""
    search_by_name_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(registry.deezer, "search_by_name", search_by_name_mock)
    monkeypatch.setattr(registry.itunes, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(registry.ytmusic, "search_by_name", AsyncMock(return_value=[]))

    track = ResolvedTrack()
    await registry.resolve_by_name_with_matching(
        make_context(), "Burned Time Machine", "Покаяння", None, track
    )

    search_by_name_mock.assert_awaited_once()


async def test_confirmed_ytmusic_candidate_derives_a_youtube_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched_candidate = TrackCandidate(
        platform="youtubeMusic",
        url="https://music.youtube.com/watch?v=n4bPzdIs_nc",
        source="ytmusic",
        artist_name="Burned Time Machine",
        title="Покаяння",
        duration_ms=225000,
    )
    monkeypatch.setattr(registry.deezer, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(registry.itunes, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        registry.ytmusic, "search_by_name", AsyncMock(return_value=[matched_candidate])
    )

    track = ResolvedTrack()
    await registry.resolve_by_name_with_matching(
        make_context(), "Burned Time Machine", "Покаяння", 224000, track
    )

    assert track.platform_links["youtubeMusic"].url == matched_candidate.url
    assert track.platform_links["youtube"].url == "https://www.youtube.com/watch?v=n4bPzdIs_nc"


async def test_derived_youtube_link_does_not_overwrite_user_supplied_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric rule from the contract addendum: the derived `youtube`
    link only fills an empty slot — a user-supplied clip always wins."""
    matched_candidate = TrackCandidate(
        platform="youtubeMusic",
        url="https://music.youtube.com/watch?v=n4bPzdIs_nc",
        source="ytmusic",
        artist_name="Burned Time Machine",
        title="Покаяння",
        duration_ms=225000,
    )
    monkeypatch.setattr(registry.deezer, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(registry.itunes, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        registry.ytmusic, "search_by_name", AsyncMock(return_value=[matched_candidate])
    )

    track = ResolvedTrack()
    user_clip = registry.PlatformLink(
        url="https://www.youtube.com/watch?v=userClipId",
        source="user_input",
        match=MatchClass.MANUAL,
    )
    track.platform_links["youtube"] = user_clip
    track.platform_link_priorities["youtube"] = registry.merge.platform_link_priority(user_clip)

    await registry.resolve_by_name_with_matching(
        make_context(), "Burned Time Machine", "Покаяння", 224000, track
    )

    assert track.platform_links["youtube"].url == "https://www.youtube.com/watch?v=userClipId"


async def test_resolve_by_isrc_fetches_album_for_year_and_release_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Deezer ISRC hit with an `album_id` triggers exactly one more
    Deezer call (album details) — the "2 requests per track" cap."""
    monkeypatch.setattr(
        registry.deezer,
        "fetch_by_isrc",
        AsyncMock(
            return_value=DeezerTrackResult(
                found=True, url="https://www.deezer.com/track/1", album_id=14880711
            )
        ),
    )
    fetch_album_mock = AsyncMock(
        return_value=DeezerAlbumResult(found=True, release_date="1993-02-22", record_type="album")
    )
    monkeypatch.setattr(registry.deezer, "fetch_album", fetch_album_mock)
    monkeypatch.setattr(
        registry.spotify, "search_by_isrc", AsyncMock(return_value=SpotifyTrackResult(found=False))
    )
    monkeypatch.setattr(
        registry.musicbrainz,
        "fetch_by_isrc",
        AsyncMock(return_value=MusicBrainzRecordingResult(found=False)),
    )

    track = ResolvedTrack()
    await registry.resolve_by_isrc(make_context(), "GBAYE9200070", track)

    fetch_album_mock.assert_awaited_once()
    assert track.deezer_album_id == 14880711
    assert track.year == 1993
    assert track.release_type == "album"


async def test_resolve_by_isrc_skips_album_fetch_without_an_album_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry.deezer,
        "fetch_by_isrc",
        AsyncMock(return_value=DeezerTrackResult(found=True, url="https://www.deezer.com/track/1")),
    )
    fetch_album_mock = AsyncMock()
    monkeypatch.setattr(registry.deezer, "fetch_album", fetch_album_mock)
    monkeypatch.setattr(
        registry.spotify, "search_by_isrc", AsyncMock(return_value=SpotifyTrackResult(found=False))
    )
    monkeypatch.setattr(
        registry.musicbrainz,
        "fetch_by_isrc",
        AsyncMock(return_value=MusicBrainzRecordingResult(found=False)),
    )

    track = ResolvedTrack()
    await registry.resolve_by_isrc(make_context(), "X", track)

    fetch_album_mock.assert_not_called()
    assert track.deezer_album_id is None


@pytest.mark.parametrize(
    ("release_date", "expected_year"),
    [("1993-02-22", 1993), ("2025", 2025), ("0000-00-00", None), ("N/A", None), (None, None)],
)
def test_release_year_treats_a_non_numeric_or_zero_date_as_unknown(
    release_date: str | None, expected_year: int | None
) -> None:
    """S3-4: Deezer answers `"0000-00-00"`, and `int("0000")` is a `0` that
    `merge_field` writes straight over a correct stored year."""
    assert registry._release_year(release_date) == expected_year


async def test_a_failing_source_no_longer_takes_the_other_sources_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-55: one Deezer quota error used to fail the whole post, even though
    Spotify would have answered — recorded as a failure the admin hears about."""
    monkeypatch.setattr(
        registry.deezer, "fetch_by_isrc", AsyncMock(side_effect=RuntimeError("quota exceeded"))
    )
    monkeypatch.setattr(
        registry.spotify,
        "search_by_isrc",
        AsyncMock(
            return_value=SpotifyTrackResult(found=True, url="https://open.spotify.com/track/ok")
        ),
    )
    monkeypatch.setattr(
        registry.musicbrainz,
        "fetch_by_isrc",
        AsyncMock(return_value=MusicBrainzRecordingResult(found=False)),
    )

    track = ResolvedTrack()
    await registry.resolve_by_isrc(make_context(), "GBAYE9200070", track)

    assert track.platform_links["spotify"].url == "https://open.spotify.com/track/ok"
    assert any("quota exceeded" in failure for failure in track.source_failures)


async def test_a_failing_name_search_source_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched_candidate = TrackCandidate(
        platform="youtubeMusic",
        url="https://music.youtube.com/watch?v=n4bPzdIs_nc",
        source="ytmusic",
        artist_name="Burned Time Machine",
        title="Покаяння",
        duration_ms=225000,
    )
    monkeypatch.setattr(
        registry.deezer, "search_by_name", AsyncMock(side_effect=RuntimeError("deezer down"))
    )
    monkeypatch.setattr(registry.itunes, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        registry.ytmusic, "search_by_name", AsyncMock(return_value=[matched_candidate])
    )

    track = ResolvedTrack()
    await registry.resolve_by_name_with_matching(
        make_context(), "Burned Time Machine", "Покаяння", 224000, track
    )

    assert track.platform_links["youtubeMusic"].url == matched_candidate.url
    assert any("deezer down" in failure for failure in track.source_failures)


async def test_a_ytmusic_candidate_with_an_unusable_video_id_derives_no_youtube_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3-6: `videoId` is source-supplied and goes into a URL — and into
    `yt-dlp` at step 4 — so it is validated like the ISRC next to it."""
    matched_candidate = TrackCandidate(
        platform="youtubeMusic",
        url="https://music.youtube.com/watch?v=../../watch",
        source="ytmusic",
        artist_name="Burned Time Machine",
        title="Покаяння",
        duration_ms=225000,
    )
    monkeypatch.setattr(registry.deezer, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(registry.itunes, "search_by_name", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        registry.ytmusic, "search_by_name", AsyncMock(return_value=[matched_candidate])
    )

    track = ResolvedTrack()
    await registry.resolve_by_name_with_matching(
        make_context(), "Burned Time Machine", "Покаяння", 224000, track
    )

    assert "youtube" not in track.platform_links


async def test_a_database_failure_inside_a_source_call_is_not_a_source_failure() -> None:
    """P1: `SourceCache` opens a Postgres session inside every one of these
    calls, so swallowing `SQLAlchemyError` turned one dead database into six
    failed sources — an empty resolve whose own handler then never ran."""
    track = ResolvedTrack()

    async def cache_is_down() -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    with pytest.raises(OperationalError):
        await registry.isolated_source_call(track, "deezer:isrc", cache_is_down(), None)

    assert track.source_failures == []


async def test_a_source_that_fails_twice_is_listed_to_the_admin_once() -> None:
    """`deezer:album` is fetched twice — year/release type here, genres in
    `service.resolve_genres` — so one quota error was reported twice."""
    track = ResolvedTrack()

    async def quota_exceeded() -> None:
        raise RuntimeError("quota limit exceeded")

    await registry.isolated_source_call(track, "deezer:album", quota_exceeded(), None)
    await registry.isolated_source_call(track, "deezer:album", quota_exceeded(), None)

    assert track.source_failures == ["deezer:album: quota limit exceeded"]
