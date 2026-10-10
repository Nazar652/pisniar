"""The plain-lyrics task: queued after the render's notice for the reasons that want it,
a refused enqueue is a log line, and the task reads, asks with no session open, writes.
No broker, no network, no database: every collaborator is replaced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from pisniar.db.models import Track
from pisniar.resolve.sources.lrclib import LyricsOutcome
from pisniar.worker import lyrics as worker_lyrics
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.celery_app import FETCH_PLAIN_LYRICS_TASK_NAME
from pisniar.worker.lyrics import LyricsPersistOutcome, LyricsPersistResult

TRACK_ID = 3
QUERY = worker_lyrics.LyricsQuery(track_id=TRACK_ID, artist_names=("A",), title="T", duration_ms=1_000, video_id=None)


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    logger = MagicMock()
    monkeypatch.setattr(worker_tasks, "logger", logger)
    return logger


@pytest.fixture
def send_task(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(worker_tasks.celery_app, "send_task", mock)
    return mock


@pytest.mark.parametrize("reason", ["lyrics_absent", "lyrics_rejected_by_matching", "lyrics_source_unavailable"])
async def test_a_missing_text_queues_the_lookup_by_track_id(
    send_task: MagicMock,
    fake_logger: MagicMock,
    reason: str,
) -> None:
    await worker_tasks.queue_plain_lyrics_lookup(TRACK_ID, reason)

    send_task.assert_called_once_with(FETCH_PLAIN_LYRICS_TASK_NAME, kwargs={"track_id": TRACK_ID})
    fake_logger.info.assert_called_once_with("plain_lyrics_task_dispatched", track_id=TRACK_ID, lyrics_reason=reason)


@pytest.mark.parametrize(
    "reason",
    ["lyrics_synced", "lyrics_instrumental", "lyrics_untimed", "lyrics_removed_by_admin"],
)
async def test_a_reason_that_needs_no_lookup_queues_nothing(send_task: MagicMock, reason: str) -> None:
    await worker_tasks.queue_plain_lyrics_lookup(TRACK_ID, reason)

    send_task.assert_not_called()


async def test_a_refused_enqueue_is_a_log_line_and_never_raises(send_task: MagicMock, fake_logger: MagicMock) -> None:
    send_task.side_effect = ConnectionError("redis is down")

    await worker_tasks.queue_plain_lyrics_lookup(TRACK_ID, "lyrics_absent")

    fake_logger.warning.assert_called_once()
    assert fake_logger.warning.call_args.args == ("plain_lyrics_task_dispatch_failed",)


async def test_the_soft_limit_while_enqueueing_is_logged_apart(send_task: MagicMock, fake_logger: MagicMock) -> None:
    send_task.side_effect = SoftTimeLimitExceeded()

    await worker_tasks.queue_plain_lyrics_lookup(TRACK_ID, "lyrics_absent")

    fake_logger.error.assert_called_once_with("plain_lyrics_task_dispatch_timed_out", track_id=TRACK_ID)


def test_the_task_carries_its_own_limits() -> None:
    task: Any = worker_tasks.fetch_plain_lyrics

    assert task.name == FETCH_PLAIN_LYRICS_TASK_NAME
    assert task.soft_time_limit == worker_tasks.settings.plain_lyrics_task_soft_time_limit_seconds
    assert task.time_limit == worker_tasks.settings.plain_lyrics_task_time_limit_seconds
    assert task.soft_time_limit < task.time_limit


def test_the_task_runs_the_body_by_track_id(monkeypatch: pytest.MonkeyPatch) -> None:
    run_async = MagicMock()
    perform = MagicMock(return_value="body")
    monkeypatch.setattr(worker_tasks, "run_async", run_async)
    monkeypatch.setattr(worker_tasks, "perform_fetch_plain_lyrics", perform)
    task: Any = worker_tasks.fetch_plain_lyrics

    task.run(TRACK_ID)

    perform.assert_called_once_with(TRACK_ID)
    run_async.assert_called_once_with("body")


def test_the_soft_limit_out_of_the_loop_is_a_log_line_and_not_a_task_error(
    monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
) -> None:
    monkeypatch.setattr(worker_tasks, "run_async", MagicMock(side_effect=SoftTimeLimitExceeded()))
    monkeypatch.setattr(worker_tasks, "perform_fetch_plain_lyrics", MagicMock())
    task: Any = worker_tasks.fetch_plain_lyrics

    task.run(TRACK_ID)

    fake_logger.error.assert_called_once_with("fetch_plain_lyrics_soft_time_limit", track_id=TRACK_ID)


def test_any_other_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_tasks, "run_async", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(worker_tasks, "perform_fetch_plain_lyrics", MagicMock())
    task: Any = worker_tasks.fetch_plain_lyrics

    with pytest.raises(RuntimeError, match="boom"):
        task.run(TRACK_ID)


class Body:
    """the task body with its engine, query, client and writer replaced."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, query: object | None) -> None:
        self.client = MagicMock()
        self.client.aclose = AsyncMock()
        self.store = AsyncMock(
            return_value=LyricsPersistResult(
                outcome=LyricsPersistOutcome.WRITTEN, source_outcome=LyricsOutcome.UNSYNCED
            )
        )

        @asynccontextmanager
        async def session_factory_scope(**kwargs: object) -> AsyncIterator[MagicMock]:
            yield MagicMock()

        monkeypatch.setattr(worker_tasks, "task_session_factory", session_factory_scope)
        monkeypatch.setattr(worker_tasks, "load_plain_lyrics_query", AsyncMock(return_value=query))
        monkeypatch.setattr(worker_tasks, "build_http_client", lambda: self.client)
        monkeypatch.setattr(worker_lyrics, "store_plain_lyrics", self.store)


async def test_the_body_stores_plain_lyrics_and_closes_the_client(
    monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
) -> None:
    body = Body(monkeypatch, query=QUERY)

    await worker_tasks.perform_fetch_plain_lyrics(TRACK_ID)

    assert body.store.await_args is not None
    assert body.store.await_args.args[3] is QUERY
    body.client.aclose.assert_awaited_once()
    fake_logger.info.assert_called_once_with("plain_lyrics_task_done", track_id=TRACK_ID, persist_outcome="written")


async def test_a_lookup_that_wrote_nothing_is_logged_as_such(
    monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
) -> None:
    body = Body(monkeypatch, query=QUERY)
    body.store.return_value = None

    await worker_tasks.perform_fetch_plain_lyrics(TRACK_ID)

    fake_logger.info.assert_called_once_with("plain_lyrics_task_done", track_id=TRACK_ID, persist_outcome=None)


async def test_the_client_is_closed_when_the_writer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    body = Body(monkeypatch, query=QUERY)
    body.store.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await worker_tasks.perform_fetch_plain_lyrics(TRACK_ID)

    body.client.aclose.assert_awaited_once()


async def test_a_deleted_track_is_a_warning_and_no_request(
    monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
) -> None:
    body = Body(monkeypatch, query=None)

    await worker_tasks.perform_fetch_plain_lyrics(TRACK_ID)

    body.store.assert_not_awaited()
    fake_logger.warning.assert_called_once_with("plain_lyrics_track_missing", track_id=TRACK_ID)


class FakeSession:
    def __init__(self, track: Track | None) -> None:
        self.track = track

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception: object) -> bool:
        return False

    async def get(self, model: object, primary_key: object) -> Track | None:
        return self.track


async def test_the_query_is_read_out_as_plain_values() -> None:
    track = Track()
    track.id = TRACK_ID
    track.artists = [{"name": "Burned Time Machine"}, {"name": "  "}, "junk"]
    track.title = "Покаяння"
    track.duration_ms = 212_000

    query = await worker_tasks.load_plain_lyrics_query(cast(Any, lambda: FakeSession(track)), TRACK_ID)

    assert query == worker_lyrics.LyricsQuery(
        track_id=TRACK_ID,
        artist_names=("Burned Time Machine",),
        title="Покаяння",
        duration_ms=212_000,
        video_id=None,
    )


async def test_a_missing_track_has_no_query() -> None:
    query = await worker_tasks.load_plain_lyrics_query(cast(Any, lambda: FakeSession(None)), TRACK_ID)

    assert query is None
