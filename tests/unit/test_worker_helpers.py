"""The pure helpers `worker.tasks` decides with: what counts as a resolve
that found something (R3-2), how a source failure is worded for the admin
(D-55), how `post.genres` is read back (R3-9), and error redaction (S3-3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.config import get_settings
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


def make_link(source: str) -> PlatformLink:
    return PlatformLink(
        url="https://www.youtube.com/watch?v=n4bPzdIs_nc", source=source, match=MatchClass.MANUAL
    )


def test_a_user_input_link_alone_is_not_a_successful_resolve() -> None:
    """R3-2: the admin's own clip is written into the result before any
    network call, so it made both "nothing usable" checks dead — and a
    completely failed resolve was delivered as a draft."""
    resolved = ResolvedTrack(platform_links={"youtube": make_link(USER_INPUT_SOURCE)})

    assert worker_tasks._resolve_found_anything(resolved) is False


def test_a_link_a_source_actually_returned_counts_as_found() -> None:
    resolved = ResolvedTrack(platform_links={"youtube": make_link("ytmusic")})

    assert worker_tasks._resolve_found_anything(resolved) is True


def test_a_title_or_an_artist_alone_counts_as_found() -> None:
    assert worker_tasks._resolve_found_anything(ResolvedTrack(title="Creep")) is True
    assert (
        worker_tasks._resolve_found_anything(ResolvedTrack(artists=[ArtistInfo(name="X")])) is True
    )


def test_source_failure_note_is_empty_when_every_source_answered() -> None:
    assert worker_tasks._source_failure_note([]) == ""


def test_source_failure_note_names_every_failed_source() -> None:
    note = worker_tasks._source_failure_note(["deezer:isrc: quota", "itunes: timeout"])

    assert "deezer:isrc: quota" in note
    assert "itunes: timeout" in note


def test_genres_from_stored_reads_the_jsonb_column_back() -> None:
    genres = worker_tasks._genres_from_stored(
        [{"name": "рок", "hashtag": None, "source": "deezer"}]
    )

    assert [(genre.name, genre.hashtag, genre.source) for genre in genres] == [
        ("рок", None, "deezer")
    ]


def test_genres_from_stored_skips_entries_without_a_usable_name() -> None:
    genres = worker_tasks._genres_from_stored(["рок", {"hashtag": "x"}, {"name": ""}])

    assert genres == []


def test_redacted_scrubs_a_configured_secret_from_an_admin_facing_message() -> None:
    bot_token = get_settings().bot_token.get_secret_value()

    redacted = worker_tasks._redacted(f"telegram said no to {bot_token}")

    assert bot_token not in redacted
    assert REDACTED_PLACEHOLDER in redacted


async def test_a_task_arriving_at_a_settled_post_says_so_in_the_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The early return of `_redeliver_if_resolved` logged nothing at all,
    in a system whose main failure mode is silence (D-51)."""
    fake_logger = MagicMock()
    monkeypatch.setattr(worker_tasks, "logger", fake_logger)

    post = MagicMock()
    post.id = 7
    post.status = PostStatus.FAILED

    await worker_tasks._redeliver_if_resolved(AsyncMock(), post)

    fake_logger.info.assert_called_once()
    assert fake_logger.info.call_args.args[0] == "resolve_post_task_arrived_at_settled_post"
    assert fake_logger.info.call_args.kwargs["status"] == "failed"
