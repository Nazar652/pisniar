"""The pure helpers `worker.tasks` decides with: what counts as a resolve that found something, how a source failure is
worded for the admin, what `persist_track` writes and in which order it merges, the degraded-draft alert, and
redaction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.config import get_settings
from pisniar.db.models import Track
from pisniar.db.states import PostStatus
from pisniar.logging import REDACTED_PLACEHOLDER
from pisniar.resolve.types import (
    USER_INPUT_SOURCE,
    ArtistInfo,
    MatchClass,
    PlatformLink,
    ResolvedTrack,
)
from pisniar.worker import tasks as worker_tasks


def make_track() -> Track:
    """the columns a loaded row always has; the schema defaults only apply on flush."""
    track = Track(source_url="https://open.spotify.com/track/x", source_url_normalized="x")
    track.artists = []
    track.platform_links = {}
    track.manual_fields = []
    track.removed_platforms = []
    return track


def make_link(source: str) -> PlatformLink:
    return PlatformLink(url="https://www.youtube.com/watch?v=n4bPzdIs_nc", source=source, match=MatchClass.MANUAL)


def test_a_user_input_link_alone_is_not_a_successful_resolve() -> None:
    """the admin's clip is written before any network call, so it must not count as proof that a source answered."""
    resolved = ResolvedTrack(platform_links={"youtube": make_link(USER_INPUT_SOURCE)})

    assert worker_tasks.resolve_found_anything(resolved) is False


def test_a_link_a_source_actually_returned_counts_as_found() -> None:
    resolved = ResolvedTrack(platform_links={"youtube": make_link("ytmusic")})

    assert worker_tasks.resolve_found_anything(resolved) is True


def test_a_title_or_an_artist_alone_counts_as_found() -> None:
    assert worker_tasks.resolve_found_anything(ResolvedTrack(title="Creep")) is True
    assert worker_tasks.resolve_found_anything(ResolvedTrack(artists=[ArtistInfo(name="X")])) is True


def test_source_failure_note_is_empty_when_every_source_answered() -> None:
    assert worker_tasks.source_failure_note([]) == ""


def test_source_failure_note_names_every_failed_source() -> None:
    note = worker_tasks.source_failure_note(["deezer:isrc: quota", "itunes: timeout"])

    assert "deezer:isrc: quota" in note
    assert "itunes: timeout" in note


def test_persist_track_writes_the_album_id_into_its_own_column() -> None:
    """the caption is rebuilt from the row, so the album id cannot live only inside a rendered `href`."""
    track = make_track()
    resolved = ResolvedTrack(title="Сумніви", album_id_spotify="4yYXqedvSKuJlgI2IWsB0j")

    worker_tasks.persist_track(track, resolved)

    assert track.album_id_spotify == "4yYXqedvSKuJlgI2IWsB0j"


def test_persist_track_writes_the_duration_into_its_own_column() -> None:
    """the download checks `ffprobe`'s answer against this column, so the duration
    cannot live only in the resolve's memory."""
    track = make_track()
    resolved = ResolvedTrack(title="Creep", duration_ms=238000)

    worker_tasks.persist_track(track, resolved)

    assert track.duration_ms == 238000


def test_persist_track_keeps_a_stored_duration_a_failed_resolve_lost() -> None:
    """end to end: `stored_scalars` reads the column only because `duration_ms`
    is in `STORED_SCALAR_FIELDS`; without it this `/retry` nulls a good duration."""
    track = make_track()
    track.duration_ms = 238000

    worker_tasks.persist_track(track, ResolvedTrack(title="Creep"))

    assert track.duration_ms == 238000


def test_persist_track_never_touches_the_languages_column() -> None:
    """only the Mini App writes languages; a resolve that rewrote them would undo
    the admin's correction on every `/retry`."""
    track = make_track()
    track.languages = ["англійська"]

    worker_tasks.persist_track(track, ResolvedTrack(title="Creep"))

    assert track.languages == ["англійська"]


def test_persist_track_merges_in_the_documented_order_with_manual_fields_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the steps after `apply_manual_fields` would refill what it restored, so its place
    at the end is pinned by the call order itself."""
    calls: list[str] = []
    for name in (
        "merge_stored_platform_links",
        "drop_removed_platforms",
        "keep_stored_title_and_artists_after_source_failure",
        "merge_stored_artists",
        "merge_stored_scalars",
        "apply_manual_fields",
    ):
        monkeypatch.setattr(worker_tasks.merge, name, lambda *args, step=name, **kwargs: calls.append(step))

    worker_tasks.persist_track(make_track(), ResolvedTrack(title="Creep"))

    assert calls == [
        "merge_stored_platform_links",
        "drop_removed_platforms",
        "keep_stored_title_and_artists_after_source_failure",
        "merge_stored_artists",
        "merge_stored_scalars",
        "apply_manual_fields",
    ]


def test_persist_track_keeps_a_manually_emptied_year_against_a_fresh_one() -> None:
    """the merge steps refill a `NULL` from the stored row or the fresh result; only
    `apply_manual_fields`, run last, can leave the value empty."""
    track = make_track()
    track.year = None
    track.manual_fields = ["year"]

    worker_tasks.persist_track(track, ResolvedTrack(title="Creep", year=1992))

    assert track.year is None


def test_persist_track_keeps_a_manual_title_and_artists_with_their_hashtags() -> None:
    track = make_track()
    track.title = "Свій заголовок"
    track.artists = [{"name": "Свій артист", "hashtag": "own", "spotify_id": None}]
    track.manual_fields = ["title", "artists"]
    resolved = ResolvedTrack(title="Fresh", artists=[ArtistInfo(name="Fresh Artist", spotify_id="abc")])

    worker_tasks.persist_track(track, resolved)

    assert track.title == "Свій заголовок"
    assert track.artists == [{"name": "Свій артист", "hashtag": "own", "spotify_id": None}]


def test_persist_track_replaces_a_field_that_is_not_listed_as_manual() -> None:
    track = make_track()
    track.release_title = "Old"
    track.year = 2000

    worker_tasks.persist_track(track, ResolvedTrack(title="Creep", year=1992))

    assert track.year == 1992
    assert track.release_title == "Old"


def test_persist_track_does_not_bring_back_a_removed_platform() -> None:
    track = make_track()
    track.removed_platforms = ["tidal"]
    track.platform_links = {
        "tidal": {
            "url": "https://listen.tidal.com/track/1",
            "source": "songlink",
            "match": "aggregator",
        }
    }
    fresh_link = PlatformLink(url="https://listen.tidal.com/track/2", source="songlink", match=MatchClass.AGGREGATOR)
    resolved = ResolvedTrack(title="Creep", platform_links={"tidal": fresh_link})

    worker_tasks.persist_track(track, resolved)

    assert "tidal" not in track.platform_links


def test_persist_track_keeps_a_manual_link_over_an_automatic_one() -> None:
    track = make_track()
    track.platform_links = {"tidal": {"url": "https://tidal.com/track/9", "source": "manual", "match": "manual"}}
    fresh_link = PlatformLink(url="https://listen.tidal.com/track/2", source="songlink", match=MatchClass.AGGREGATOR)

    worker_tasks.persist_track(track, ResolvedTrack(title="Creep", platform_links={"tidal": fresh_link}))

    assert track.platform_links["tidal"] == {
        "url": "https://tidal.com/track/9",
        "source": "manual",
        "match": "manual",
    }


async def test_the_degraded_draft_alert_names_the_refused_links_and_the_failed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = AsyncMock()
    monkeypatch.setattr(worker_tasks, "alert_admin_and_draft_chat", alert)
    post = MagicMock(id=7)

    await worker_tasks.warn_about_source_failures(
        post, ["itunes: timeout"], ["tidal: хост evil.example не належить платформі (songlink)"]
    )

    text = alert.await_args_list[0].args[1]
    assert "itunes: timeout" in text
    assert "evil.example" in text


async def test_a_refused_link_alone_is_enough_to_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alert = AsyncMock()
    monkeypatch.setattr(worker_tasks, "alert_admin_and_draft_chat", alert)

    await worker_tasks.warn_about_source_failures(MagicMock(id=7), [], ["tidal: не https (x)"])

    text = alert.await_args_list[0].args[1]
    assert "не https" in text
    assert "/retry" not in text


async def test_a_clean_resolve_sends_no_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alert = AsyncMock()
    monkeypatch.setattr(worker_tasks, "alert_admin_and_draft_chat", alert)

    await worker_tasks.warn_about_source_failures(MagicMock(id=7), [], [])

    alert.assert_not_awaited()


def test_redacted_scrubs_a_configured_secret_from_an_admin_facing_message() -> None:
    bot_token = get_settings().bot_token.get_secret_value()

    redacted = worker_tasks.redacted_text(f"telegram said no to {bot_token}")

    assert bot_token not in redacted
    assert REDACTED_PLACEHOLDER in redacted


async def test_a_task_arriving_at_a_settled_post_says_so_in_the_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the system's main failure mode is silence, so the early return of `redeliver_if_resolved` must log too."""
    fake_logger = MagicMock()
    monkeypatch.setattr(worker_tasks, "logger", fake_logger)

    post = MagicMock()
    post.id = 7
    post.status = PostStatus.FAILED

    await worker_tasks.redeliver_if_resolved(AsyncMock(), post)

    fake_logger.info.assert_called_once()
    assert fake_logger.info.call_args.args[0] == "resolve_post_task_arrived_at_settled_post"
    assert fake_logger.info.call_args.kwargs["status"] == "failed"
