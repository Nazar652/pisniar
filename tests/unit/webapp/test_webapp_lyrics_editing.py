"""Reading and replacing the lyrics with the database faked: the order of the lock, the checks and
the writes, every refusal leaving the old text alone, and the log lines without a word of the song.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete, Insert
from support import ADMIN_ID

from pisniar.db.models import (
    MAX_LYRIC_LINES,
    MAX_LYRIC_TEXT_LENGTH,
    MAX_LYRICS_TOTAL_CHARACTERS,
    AudioAsset,
    Lyric,
    Post,
    Track,
)
from pisniar.db.states import PostStatus
from pisniar.webapp import lyrics_editing
from pisniar.webapp.editing import STALE_DATA_MESSAGE
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.lyrics_editing import (
    LYRICS_BLOCKING_STATUSES,
    LyricsReplaceError,
    fetch_lyrics_view,
    lyrics_version_of,
    replace_lyrics,
    view_of,
    write_lines,
)
from pisniar.webapp.lyrics_rules import LineDraft
from pisniar.webapp.schemas import LyricLineInput, LyricsUpdate

POST_ID = 7
TRACK_ID = 3
DURATION_MS = 180_000
MARKER = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SECRET = "слова пісні, яких немає в журналі"


class RecordingLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def record(event: str, **fields: Any) -> None:
            self.entries.append({"event": event, "log_level": level, **fields})

        return record


def make_lyric(
    index: int,
    start_ms: int | None,
    text: str,
    source: str = "lrclib",
) -> Lyric:
    return Lyric(track_id=TRACK_ID, line_index=index, start_ms=start_ms, text=text, source=source)


STORED = [make_lyric(0, 1_000, "перший"), make_lyric(1, None, "другий")]


def make_post(status: PostStatus = PostStatus.RENDERED, **fields: Any) -> Post:
    return Post(id=POST_ID, track_id=TRACK_ID, status=status, **fields)


def make_update(*lines: tuple[str, int | None], stored: list[Lyric] = STORED) -> LyricsUpdate:
    """a save made against `stored`, the text the editor was opened with."""
    return LyricsUpdate(
        lines=[LyricLineInput(text=text, start_ms=start_ms) for text, start_ms in lines],
        expected_lyrics_version=lyrics_version_of(stored, None),
    )


class FakeSession:
    """records what is executed and in which order relative to the fakes of the locks."""

    def __init__(
        self,
        post: Post | None,
        track: Track | None,
        order: list[str],
    ) -> None:
        self.post = post
        self.track = track
        self.order = order
        self.statements: list[Any] = []
        self.inserted_rows: int | None = None

    async def get(self, model: type, key: int) -> Post | Track | None:
        self.order.append("get")
        return self.track if model is Track else self.post

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if isinstance(statement, Delete):
            self.order.append("delete")
            return MagicMock()
        assert isinstance(statement, Insert)
        self.order.append("insert")
        rows = len([name for name in statement.compile().params if name.startswith("line_index")])
        result = MagicMock()
        result.all.return_value = [
            (index,) for index in range(rows if self.inserted_rows is None else self.inserted_rows)
        ]
        return result

    async def flush(self) -> None:
        self.order.append("flush")

    async def refresh(self, row: object) -> None:
        self.order.append("refresh")


class World:
    """the faked rows around one save; `track` and `post` live on the session that serves them."""

    def __init__(self, post: Post | None, order: list[str]) -> None:
        self.session = FakeSession(post, Track(id=TRACK_ID, lyrics_edited_at=None), order)
        self.order = order
        self.media_posts = 0
        self.asset: AudioAsset | None = AudioAsset(duration_ms=DURATION_MS, video_id="n4bPzdIs_nc")
        self.stored: list[Lyric] = list(STORED)
        self.counted_statuses: Any = None

    @property
    def db(self) -> AsyncSession:
        return cast("AsyncSession", self.session)

    @property
    def track(self) -> Track | None:
        return self.session.track

    @track.setter
    def track(self, track: Track | None) -> None:
        self.session.track = track

    @property
    def post(self) -> Post | None:
        return self.session.post

    @post.setter
    def post(self, post: Post | None) -> None:
        self.session.post = post


@pytest.fixture
def logger(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(lyrics_editing, "logger", recorder)
    return recorder


def build_world(monkeypatch: pytest.MonkeyPatch, post: Post | None = None) -> World:
    order: list[str] = []
    world = World(post if post is not None else make_post(), order)

    async def lock_track(_: object, track_id: int) -> Track | None:
        order.append("lock")
        return world.track

    async def count_posts(
        _: object,
        track_id: int,
        statuses: Any = None,
    ) -> int:
        order.append("count")
        world.counted_statuses = statuses
        return world.media_posts

    async def current_asset(_: object, track: Track) -> AudioAsset | None:
        order.append("audio")
        return world.asset

    async def read_lines(_: object, track_id: int) -> list[Lyric]:
        order.append("read")
        # after a delete with no insert the table is empty
        if "delete" in order and "insert" not in order:
            return []
        return world.stored

    async def other_posts(
        _: object,
        track_id: int,
        post_id: int,
    ) -> list[int]:
        return [11, 12]

    monkeypatch.setattr(lyrics_editing, "lock_track_for_write", lock_track)
    monkeypatch.setattr(lyrics_editing, "count_media_posts", count_posts)
    monkeypatch.setattr(lyrics_editing, "current_audio_asset", current_asset)
    monkeypatch.setattr(lyrics_editing, "read_stored_lines", read_lines)
    monkeypatch.setattr(lyrics_editing, "other_post_ids_of", other_posts)
    return world


def inserted_rows_of(session: FakeSession) -> list[tuple[Any, ...]]:
    """(line_index, start_ms, text, source) of every row the one INSERT carried."""
    insert_statement = next(item for item in session.statements if isinstance(item, Insert))
    params = insert_statement.compile().params
    count = len([name for name in params if name.startswith("line_index")])
    return [
        tuple(params[f"{column}_m{index}"] for column in ("line_index", "start_ms", "text", "source"))
        for index in range(count)
    ]


async def save(world: World, update: LyricsUpdate) -> Any:
    return await replace_lyrics(world.db, POST_ID, update, ADMIN_ID)


async def refusal_of(world: World, update: LyricsUpdate) -> ApiProblem:
    with pytest.raises(ApiProblem) as refusal:
        await save(world, update)
    return refusal.value


def assert_nothing_written(world: World) -> None:
    assert "delete" not in world.order
    assert "insert" not in world.order


def test_the_version_is_deterministic_and_changes_with_anything_stored() -> None:
    first = lyrics_version_of(STORED, None)

    assert lyrics_version_of(list(STORED), None) == first
    assert len(first) == 64
    assert lyrics_version_of(STORED, MARKER) != first
    assert lyrics_version_of([], None) != first
    assert lyrics_version_of([STORED[0]], None) != first
    assert lyrics_version_of(STORED[::-1], None) != first
    assert lyrics_version_of([make_lyric(0, 1_001, "перший"), STORED[1]], None) != first
    assert lyrics_version_of([make_lyric(0, 1_000, "перший!"), STORED[1]], None) != first
    assert lyrics_version_of([make_lyric(0, 1_000, "перший", "manual"), STORED[1]], None) != first
    assert lyrics_version_of([make_lyric(0, None, "перший"), STORED[1]], None) != first


def test_the_view_carries_the_lines_the_marker_the_neighbours_and_the_limits() -> None:
    view = view_of(STORED, MARKER, [11, 12])

    assert [(line.text, line.start_ms, line.source) for line in view.lines] == [
        ("перший", 1_000, "lrclib"),
        ("другий", None, "lrclib"),
    ]
    assert view.edited_by_admin is True
    assert view.other_post_ids == [11, 12]
    assert view.lyrics_version == lyrics_version_of(STORED, MARKER)
    assert (view.max_lines, view.max_line_length, view.max_total_characters) == (
        MAX_LYRIC_LINES,
        MAX_LYRIC_TEXT_LENGTH,
        MAX_LYRICS_TOTAL_CHARACTERS,
    )
    assert view_of([], None, []).edited_by_admin is False


async def test_a_replacement_locks_the_track_before_it_deletes_and_marks_the_text(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)
    update = make_update(("  раз  два ", 1_000), ("три", None), ("", 5_000))

    view = await save(world, update)

    assert world.order[:3] == ["get", "lock", "count"]
    assert world.order.index("lock") < world.order.index("delete") < world.order.index("insert")
    assert world.order.count("delete") == 1 and world.order.count("insert") == 1
    assert inserted_rows_of(world.session) == [
        (0, 1_000, "раз два", "manual"),
        (1, None, "три", "manual"),
        (2, 5_000, "", "manual"),
    ]
    assert world.track is not None and world.track.lyrics_edited_at is not None
    assert view.other_post_ids == [11, 12]
    assert len([entry for entry in logger.entries if entry["event"] == "lyrics_saved"]) == 1


async def test_the_media_guard_counts_downloading_downloaded_and_rendering_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(monkeypatch)

    await save(world, make_update(("раз", 1_000)))

    assert set(world.counted_statuses) == {
        PostStatus.DOWNLOADING,
        PostStatus.DOWNLOADED,
        PostStatus.RENDERING,
    }
    assert PostStatus.EDITING not in LYRICS_BLOCKING_STATUSES


async def test_an_empty_text_deletes_and_inserts_nothing_and_is_logged_as_cleared(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)

    await save(world, make_update())

    assert world.order.count("delete") == 1
    assert "insert" not in world.order
    assert world.track is not None and world.track.lyrics_edited_at is not None
    entry = next(entry for entry in logger.entries if entry["event"] == "lyrics_saved")
    assert (entry["line_count"], entry["timed_count"]) == (0, 0)
    assert entry["cleared"] is True


async def test_the_saved_log_line_counts_the_lines_and_never_carries_a_word(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)
    world.stored = [make_lyric(0, 1_000, SECRET, "manual"), make_lyric(1, None, SECRET, "manual")]

    await save(world, make_update((SECRET, 1_000), (SECRET, None), stored=world.stored))

    entry = next(entry for entry in logger.entries if entry["event"] == "lyrics_saved")
    assert {key: entry[key] for key in ("user_id", "post_id", "track_id", "line_count", "timed_count")} == {
        "user_id": ADMIN_ID,
        "post_id": POST_ID,
        "track_id": TRACK_ID,
        "line_count": 2,
        "timed_count": 1,
    }
    assert entry["cleared"] is False
    assert SECRET not in repr(logger.entries)


async def test_a_missing_post_or_track_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world(monkeypatch)
    world.post = None
    assert await save(world, make_update()) is None

    world = build_world(monkeypatch)
    world.track = None
    assert await save(world, make_update()) is None
    assert_nothing_written(world)


@pytest.mark.parametrize(
    "status", [PostStatus.RESOLVED, PostStatus.PUBLISHED, PostStatus.DOWNLOADING, PostStatus.DRAFT]
)
async def test_a_post_whose_fragment_cannot_be_chosen_is_not_choosable_and_nothing_is_locked(
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
    status: PostStatus,
) -> None:
    world = build_world(monkeypatch, make_post(status))

    problem = await refusal_of(world, make_update())

    assert (problem.status_code, problem.code) == (409, "not_choosable")
    assert world.order == ["get"]
    assert logger.entries[0]["event"] == "lyrics_save_refused"
    assert logger.entries[0]["reason"] == "not_choosable"
    assert status.value not in problem.message


@pytest.mark.parametrize("status", [PostStatus.RENDERED, PostStatus.EDITING])
async def test_the_text_of_a_rendered_post_and_of_one_awaiting_a_choice_is_editable(
    monkeypatch: pytest.MonkeyPatch, status: PostStatus
) -> None:
    world = build_world(monkeypatch, make_post(status))

    assert await save(world, make_update()) is not None


async def test_a_sibling_post_in_a_media_state_is_media_in_progress_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)
    world.media_posts = 1

    problem = await refusal_of(world, make_update(("раз", 1_000)))

    assert (problem.status_code, problem.code) == (409, "media_in_progress")
    assert_nothing_written(world)
    assert logger.entries[-1]["reason"] == "media_in_progress"


async def test_no_audio_of_the_current_recording_is_audio_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(monkeypatch)
    world.asset = None

    problem = await refusal_of(world, make_update(("раз", 1_000)))

    assert (problem.status_code, problem.code) == (409, "audio_missing")
    assert_nothing_written(world)


async def test_a_text_that_breaks_the_rule_is_422_with_the_line_number_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)

    problem = await refusal_of(world, make_update(("раз", 1_000), ("два", DURATION_MS)))

    assert (problem.status_code, problem.code) == (422, "invalid_lyrics")
    assert "2" in problem.message and str(DURATION_MS) in problem.message
    assert_nothing_written(world)
    assert logger.entries[-1]["reason"] == "invalid_lyrics"
    assert logger.entries[-1]["line_number"] == 2


async def test_a_line_of_501_characters_is_422_with_its_number_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(monkeypatch)

    problem = await refusal_of(world, make_update(("раз", 1_000), ("а" * (MAX_LYRIC_TEXT_LENGTH + 1), None)))

    assert (problem.status_code, problem.code) == (422, "invalid_lyrics")
    assert "2" in problem.message and "501" in problem.message
    assert_nothing_written(world)


async def test_a_line_long_only_in_spaces_is_accepted_once_they_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(monkeypatch)
    raw = "а" * 249 + " " * 400 + "б" * 250
    assert len(raw) > MAX_LYRIC_TEXT_LENGTH

    await save(world, make_update((raw, None)))

    stored = inserted_rows_of(world.session)
    assert len(stored[0][2]) == MAX_LYRIC_TEXT_LENGTH


async def test_a_text_changed_since_the_editor_opened_is_stale_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)
    world.stored = [make_lyric(0, 1_000, "прийшов LRCLIB")]

    problem = await refusal_of(world, make_update(("раз", 1_000)))

    assert (problem.status_code, problem.code, problem.message) == (
        409,
        "stale_data",
        STALE_DATA_MESSAGE,
    )
    assert_nothing_written(world)
    assert logger.entries[-1]["reason"] == "stale_data"


async def test_a_version_read_before_the_marker_was_set_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(monkeypatch)
    world.track = Track(id=TRACK_ID, lyrics_edited_at=MARKER)

    problem = await refusal_of(world, make_update(("раз", 1_000)))

    assert problem.code == "stale_data"


async def test_the_lines_are_written_by_one_statement_after_the_delete() -> None:
    world = World(None, [])

    await write_lines(world.db, TRACK_ID, [LineDraft("раз", 1_000), LineDraft("два", None)])

    assert world.order == ["delete", "insert"]


async def test_a_write_that_lands_short_raises_so_the_transaction_rolls_back() -> None:
    world = World(None, [])
    world.session.inserted_rows = 1

    with pytest.raises(LyricsReplaceError):
        await write_lines(world.db, TRACK_ID, [LineDraft("раз", 1_000), LineDraft("два", None)])


async def test_nothing_but_the_delete_runs_for_an_empty_text() -> None:
    world = World(None, [])

    await write_lines(world.db, TRACK_ID, [])

    assert world.order == ["delete"]


async def test_the_view_of_a_text_reads_without_a_lock_and_logs_counts_only(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch)
    world.stored = [make_lyric(0, 1_000, SECRET), make_lyric(1, None, SECRET)]

    view = await fetch_lyrics_view(world.db, POST_ID, ADMIN_ID)

    assert view is not None and len(view.lines) == 2
    assert "lock" not in world.order
    viewed = next(entry for entry in logger.entries if entry["event"] == "lyrics_viewed")
    assert (viewed["user_id"], viewed["post_id"], viewed["track_id"], viewed["line_count"]) == (
        ADMIN_ID,
        POST_ID,
        TRACK_ID,
        2,
    )
    assert SECRET not in repr(logger.entries)


async def test_the_view_of_a_post_that_is_not_choosable_is_a_409(
    monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger
) -> None:
    world = build_world(monkeypatch, make_post(PostStatus.PUBLISHED))

    with pytest.raises(ApiProblem) as refusal:
        await fetch_lyrics_view(world.db, POST_ID, ADMIN_ID)

    assert refusal.value.code == "not_choosable"
    assert logger.entries[0]["event"] == "lyrics_view_refused"


async def test_the_view_of_a_missing_post_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world(monkeypatch)
    world.post = None

    assert await fetch_lyrics_view(world.db, POST_ID, ADMIN_ID) is None
