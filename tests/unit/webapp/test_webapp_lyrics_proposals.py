"""The two lookups behind «Знайти синхронізацію» and «Знайти просто текст»: the normalization of
what a source found, the read that precedes the network, the one time limit and the routes.
Every source is replaced; nothing is written, and no word of a song reaches a log line.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast, get_args
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from support import ADMIN_ID, NOW
from test_webapp_posts_routes import bearer

from pisniar.config import Settings
from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH, AudioAsset, Post, Track
from pisniar.db.states import PostStatus
from pisniar.resolve import lyrics_lookup
from pisniar.resolve.lyrics_lookup import PLAIN_TEXT_SOURCES, LyricsLookupQuery, SourceAnswer
from pisniar.resolve.sources.lrclib import LyricsLine, LyricsOutcome, LyricsResult
from pisniar.webapp import lyrics_proposals
from pisniar.webapp.app import create_webapp
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.lyrics_proposals import (
    SYNCED_SCOPE,
    LookupScope,
    LookupSubject,
    ProposalOverLimitsError,
    artist_names_of,
    fetch_lookup_subject,
    fitted_text,
    known_source,
    plain_proposal,
    synced_proposal,
)
from pisniar.webapp.lyrics_rules import LineDraft, plan_lyrics
from pisniar.webapp.schemas import LyricsSourceName

POST_ID = 7
TRACK_ID = 3
DURATION_MS = 180_000
SECRET = "слова пісні, яких немає в журналі"
QUERY = LyricsLookupQuery(
    track_id=TRACK_ID,
    artist_names=("Артист",),
    title="Тема",
    duration_ms=DURATION_MS,
    video_id="n4bPzdIs_nc",
)
SUBJECT = LookupSubject(query=QUERY, audio_duration_ms=DURATION_MS)


def timed(*lines: tuple[int, str], source: str = "ytmusic") -> SourceAnswer:
    return SourceAnswer(
        source,
        LyricsResult(
            outcome=LyricsOutcome.SYNCED,
            lines=tuple(LyricsLine(start_ms=start_ms, text=text) for start_ms, text in lines),
        ),
    )


def plain_result(*texts: str) -> LyricsResult:
    return LyricsResult(
        outcome=LyricsOutcome.UNSYNCED,
        lines=tuple(LyricsLine(start_ms=None, text=text) for text in texts),
    )


def drafts_of(proposal_lines: list[Any]) -> list[LineDraft]:
    return [LineDraft(text=line.text, start_ms=line.start_ms) for line in proposal_lines]


class RecordingLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def record(event: str, **fields: Any) -> None:
            self.entries.append({"event": event, "log_level": level, **fields})

        return record

    def events(self, name: str) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry["event"] == name]


@pytest.fixture
def logger(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(lyrics_proposals, "logger", recorder)
    return recorder


def test_the_source_names_are_the_ones_an_admin_can_pick() -> None:
    assert set(get_args(LyricsSourceName)) == {source.name for source in PLAIN_TEXT_SOURCES}
    assert [known_source(source.name) for source in PLAIN_TEXT_SOURCES] == [
        source.name for source in PLAIN_TEXT_SOURCES
    ]
    with pytest.raises(ValueError, match="невідоме джерело"):
        known_source("nope")


def test_artist_names_are_the_stored_names_without_blanks() -> None:
    stored = [{"name": "А", "hashtag": None}, {"name": "  "}, {"hashtag": "x"}, "Б", {"name": 5}]

    assert artist_names_of(stored) == ("А",)


@pytest.mark.parametrize(
    ("raw", "text", "cut"),
    [
        ("  раз \t два ", "раз два", False),
        ("а" * MAX_LYRIC_TEXT_LENGTH, "а" * MAX_LYRIC_TEXT_LENGTH, False),
        ("а" * (MAX_LYRIC_TEXT_LENGTH + 1), "а" * MAX_LYRIC_TEXT_LENGTH, True),
        ("", "", False),
    ],
)
def test_a_long_line_is_cut_and_never_dropped(
    raw: str,
    text: str,
    cut: bool,
) -> None:
    assert fitted_text(raw) == (text, cut)


def test_synced_lines_keep_their_times_and_pass_the_save_rule() -> None:
    proposal = synced_proposal(timed((1_000, " раз  два "), (2_000, "♪"), (3_000, "")), DURATION_MS)

    assert (proposal.kind, proposal.source) == ("synced", "ytmusic")
    assert [(line.text, line.start_ms) for line in proposal.lines] == [
        ("раз два", 1_000),
        ("♪", 2_000),
        ("", 3_000),
    ]
    assert (proposal.dropped_line_count, proposal.truncated_line_count) == (0, 0)
    assert plan_lyrics(drafts_of(proposal.lines), DURATION_MS)


def test_lines_at_one_time_are_told_apart_ten_milliseconds_each() -> None:
    proposal = synced_proposal(timed((1_000, "а"), (1_000, "б"), (1_000, "в"), (1_005, "г"), (2_000, "д")), DURATION_MS)

    assert [line.start_ms for line in proposal.lines] == [1_000, 1_010, 1_020, 1_030, 2_000]
    assert plan_lyrics(drafts_of(proposal.lines), DURATION_MS)


def test_lines_given_out_of_order_are_sorted_before_they_are_made_rising() -> None:
    proposal = synced_proposal(timed((3_000, "в"), (1_000, "а"), (2_000, "б")), DURATION_MS)

    assert [line.text for line in proposal.lines] == ["а", "б", "в"]


def test_lines_at_or_past_the_end_of_the_audio_are_counted_and_left_out() -> None:
    proposal = synced_proposal(
        timed((1_000, "а"), (DURATION_MS - 1, "б"), (DURATION_MS, "в"), (DURATION_MS + 5, "г")),
        DURATION_MS,
    )

    assert [line.text for line in proposal.lines] == ["а", "б"]
    assert proposal.dropped_line_count == 2
    assert plan_lyrics(drafts_of(proposal.lines), DURATION_MS)


def test_a_collision_pushing_a_line_to_the_end_drops_it() -> None:
    proposal = synced_proposal(timed((DURATION_MS - 10, "а"), (DURATION_MS - 10, "б")), DURATION_MS)

    assert [(line.text, line.start_ms) for line in proposal.lines] == [("а", DURATION_MS - 10)]
    assert proposal.dropped_line_count == 1


def test_a_long_synced_line_is_cut_and_counted() -> None:
    proposal = synced_proposal(timed((1_000, "а" * 600), (2_000, "б")), DURATION_MS)

    assert len(proposal.lines[0].text) == MAX_LYRIC_TEXT_LENGTH
    assert proposal.truncated_line_count == 1
    assert plan_lyrics(drafts_of(proposal.lines), DURATION_MS)


def test_lines_without_a_time_are_not_part_of_a_synced_proposal() -> None:
    answer = SourceAnswer(
        "lrclib",
        LyricsResult(
            outcome=LyricsOutcome.SYNCED,
            lines=(
                LyricsLine(start_ms=None, text="без часу"),
                LyricsLine(start_ms=1_000, text="а"),
            ),
        ),
    )

    assert [line.text for line in synced_proposal(answer, DURATION_MS).lines] == ["а"]


def test_a_synced_text_past_the_line_limit_is_over_limits() -> None:
    answer = timed(*((index, "р") for index in range(MAX_LYRIC_LINES + 1)))

    with pytest.raises(ProposalOverLimitsError):
        synced_proposal(answer, DURATION_MS)


def test_the_line_limit_is_counted_after_the_lines_past_the_end_are_dropped() -> None:
    answer = timed(*((index, "р") for index in range(MAX_LYRIC_LINES)), (DURATION_MS + 1, "зайвий"))

    assert len(synced_proposal(answer, DURATION_MS).lines) == MAX_LYRIC_LINES


def test_a_synced_text_past_the_character_limit_is_over_limits() -> None:
    answer = timed(*((index, "а" * 400) for index in range(51)))

    with pytest.raises(ProposalOverLimitsError):
        synced_proposal(answer, DURATION_MS)


def test_a_plain_proposal_has_no_times_and_counts_what_was_left_out() -> None:
    proposal = plain_proposal("genius", ("раз", "а" * 700, "три"), 5)

    assert (proposal.kind, proposal.source) == ("plain", "genius")
    assert [line.start_ms for line in proposal.lines] == [None, None, None]
    assert len(proposal.lines[1].text) == MAX_LYRIC_TEXT_LENGTH
    assert (proposal.dropped_line_count, proposal.truncated_line_count) == (2, 1)
    assert plan_lyrics(drafts_of(proposal.lines), DURATION_MS)


def test_a_plain_text_past_a_limit_is_over_limits() -> None:
    with pytest.raises(ProposalOverLimitsError):
        plain_proposal("lrclib", tuple("р" for _ in range(MAX_LYRIC_LINES + 1)), MAX_LYRIC_LINES + 1)
    with pytest.raises(ProposalOverLimitsError):
        plain_proposal("lrclib", tuple("а" * 400 for _ in range(51)), 51)


class SubjectSession:
    """a session whose one SELECT answers a post and its track; every statement is kept."""

    def __init__(self, post: Post | None, track: Track | None) -> None:
        self.statements: list[Any] = []
        row = MagicMock()
        row.tuple.return_value = (post, track)
        self.result = MagicMock()
        self.result.one_or_none.return_value = None if post is None else row

    @property
    def db(self) -> AsyncSession:
        return cast("AsyncSession", self)

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return self.result


def make_track() -> Track:
    return Track(
        id=TRACK_ID,
        title="Тема",
        artists=[{"name": "Артист", "hashtag": None}],
        duration_ms=DURATION_MS - 1_000,
    )


def make_post(status: PostStatus = PostStatus.RENDERED, **fields: Any) -> Post:
    return Post(id=POST_ID, track_id=TRACK_ID, status=status, **fields)


@pytest.fixture
def audio(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    asset = AsyncMock(return_value=AudioAsset(duration_ms=DURATION_MS, video_id="n4bPzdIs_nc", track_id=TRACK_ID))
    monkeypatch.setattr(lyrics_proposals, "current_audio_asset", asset)
    return asset


async def test_the_subject_is_read_without_a_lock_from_the_post_the_track_and_the_audio(
    audio: AsyncMock,
) -> None:
    session = SubjectSession(make_post(), make_track())

    subject = await fetch_lookup_subject(session.db, POST_ID, SYNCED_SCOPE, ADMIN_ID)

    assert subject == LookupSubject(
        query=LyricsLookupQuery(TRACK_ID, ("Артист",), "Тема", DURATION_MS - 1_000, "n4bPzdIs_nc"),
        audio_duration_ms=DURATION_MS,
    )
    assert len(session.statements) == 1
    assert "FOR " not in str(session.statements[0].compile())


async def test_a_missing_post_has_no_subject(audio: AsyncMock) -> None:
    session = SubjectSession(None, None)

    assert await fetch_lookup_subject(session.db, POST_ID, SYNCED_SCOPE, ADMIN_ID) is None


@pytest.mark.parametrize(
    "status", [PostStatus.RESOLVED, PostStatus.PUBLISHED, PostStatus.DOWNLOADING, PostStatus.DRAFT]
)
async def test_a_post_whose_text_cannot_be_edited_is_not_choosable_without_naming_its_state(
    audio: AsyncMock,
    logger: RecordingLogger,
    status: PostStatus,
) -> None:
    session = SubjectSession(make_post(status), make_track())

    with pytest.raises(ApiProblem) as refusal:
        await fetch_lookup_subject(session.db, POST_ID, LookupScope("plain", "genius"), ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "not_choosable")
    assert status.value not in refusal.value.message
    refused = logger.events("lyrics_lookup_refused")
    assert [(entry["reason"], entry["kind"]) for entry in refused] == [("not_choosable", "plain")]
    audio.assert_not_awaited()


@pytest.mark.parametrize("status", [PostStatus.RENDERED, PostStatus.EDITING])
async def test_a_rendered_post_and_one_awaiting_a_choice_can_be_looked_up(audio: AsyncMock, status: PostStatus) -> None:
    session = SubjectSession(make_post(status), make_track())

    assert await fetch_lookup_subject(session.db, POST_ID, SYNCED_SCOPE, ADMIN_ID) is not None


async def test_no_audio_of_the_current_recording_is_audio_missing(audio: AsyncMock, logger: RecordingLogger) -> None:
    audio.return_value = None
    session = SubjectSession(make_post(), make_track())

    with pytest.raises(ApiProblem) as refusal:
        await fetch_lookup_subject(session.db, POST_ID, SYNCED_SCOPE, ADMIN_ID)

    assert (refusal.value.status_code, refusal.value.code) == (409, "audio_missing")
    assert logger.events("lyrics_lookup_refused")[0]["reason"] == "audio_missing"


@dataclass
class Lookup:
    """the app under test with a session that notes when it closes, and the replaced sources."""

    client: TestClient
    subject_read: AsyncMock
    sessions_open: list[int] = field(default_factory=list)
    network: list[str] = field(default_factory=list)

    def get(self, path: str) -> Any:
        return self.client.get(path, headers=bearer())


class NotedSession:
    def __init__(self, lookup: Lookup) -> None:
        self.lookup = lookup

    async def __aenter__(self) -> NotedSession:
        self.lookup.sessions_open.append(1)
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        self.lookup.sessions_open.pop()


def build_lookup(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    subject: LookupSubject | None = SUBJECT,
    with_http_client: bool = True,
) -> Lookup:
    holder: dict[str, Lookup] = {}
    session_factory = MagicMock(side_effect=lambda: NotedSession(holder["lookup"]))
    app = create_webapp(
        settings,
        clock=lambda: float(NOW),
        session_factory=session_factory,
        http_client=MagicMock() if with_http_client else None,
    )
    subject_read = AsyncMock(return_value=subject)
    lookup = Lookup(TestClient(app), subject_read)
    holder["lookup"] = lookup
    monkeypatch.setattr(lyrics_proposals, "fetch_lookup_subject", subject_read)
    return lookup


def replace_timed_lookup(
    lookup: Lookup,
    monkeypatch: pytest.MonkeyPatch,
    answer: SourceAnswer | None,
    reason: str = "",
) -> AsyncMock:
    async def found(*args: object) -> tuple[SourceAnswer | None, str]:
        lookup.network.append(f"timed:open_sessions={len(lookup.sessions_open)}")
        return answer, reason

    spy = AsyncMock(side_effect=found)
    monkeypatch.setattr(lyrics_proposals, "look_up_timed_text", spy)
    return spy


def replace_plain_lookup(
    lookup: Lookup,
    monkeypatch: pytest.MonkeyPatch,
    result: LyricsResult | BaseException,
) -> AsyncMock:
    async def found(*args: object) -> LyricsResult:
        lookup.network.append(f"plain:open_sessions={len(lookup.sessions_open)}")
        if isinstance(result, BaseException):
            raise result
        return result

    spy = AsyncMock(side_effect=found)
    monkeypatch.setattr(lyrics_proposals, "fetch_plain_text", spy)
    return spy


SYNCED_PATH = f"/api/posts/{POST_ID}/lyrics/synced"


def plain_path(source: str) -> str:
    return f"/api/posts/{POST_ID}/lyrics/plain?source={source}"


def test_a_timed_text_is_offered_with_its_source_and_nothing_else(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    spy = replace_timed_lookup(lookup, monkeypatch, timed((1_000, SECRET), (2_000, "♪")))

    response = lookup.get(SYNCED_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "kind": "synced",
        "source": "ytmusic",
        "lines": [{"text": SECRET, "start_ms": 1_000}, {"text": "♪", "start_ms": 2_000}],
        "dropped_line_count": 0,
        "truncated_line_count": 0,
    }
    assert spy.await_args is not None and spy.await_args.args[2] == QUERY
    assert [entry["event"] for entry in logger.entries] == [
        "lyrics_lookup_requested",
        "lyrics_lookup_found",
    ]
    assert logger.entries[1] | {"source": "ytmusic"} == logger.entries[1]
    assert (logger.entries[1]["kind"], logger.entries[1]["line_count"]) == ("synced", 2)
    assert SECRET not in repr(logger.entries)


def test_the_fallback_source_is_named_in_the_proposal(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_timed_lookup(lookup, monkeypatch, timed((1_000, "а"), source="lrclib"))

    assert lookup.get(SYNCED_PATH).json()["source"] == "lrclib"


def test_the_session_is_closed_before_the_network_is_asked(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_timed_lookup(lookup, monkeypatch, timed((1_000, "а")))
    replace_plain_lookup(lookup, monkeypatch, plain_result("а"))

    lookup.get(SYNCED_PATH)
    lookup.get(plain_path("genius"))

    assert lookup.network == ["timed:open_sessions=0", "plain:open_sessions=0"]


@pytest.mark.parametrize(
    ("reason", "status_code", "code", "message"),
    [
        (
            "lyrics_absent",
            409,
            "synced_lyrics_not_found",
            lyrics_proposals.SYNCED_NOT_FOUND_MESSAGE,
        ),
        (
            "lyrics_source_unavailable",
            503,
            "lyrics_source_unavailable",
            lyrics_proposals.SOURCE_UNAVAILABLE_MESSAGE,
        ),
        ("lyrics_over_limits", 409, "lyrics_over_limits", lyrics_proposals.OVER_LIMITS_MESSAGE),
    ],
)
def test_no_timed_text_is_a_refusal_by_the_reason_with_its_own_sentence(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
    reason: str,
    status_code: int,
    code: str,
    message: str,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_timed_lookup(lookup, monkeypatch, None, reason)

    response = lookup.get(SYNCED_PATH)

    assert (response.status_code, response.json()) == (
        status_code,
        {"code": code, "message": message},
    )
    refused = logger.events("lyrics_lookup_refused")
    assert [(entry["reason"], entry["kind"]) for entry in refused] == [(code, "synced")]


def test_a_timed_text_that_ends_up_empty_after_the_end_of_the_audio_is_not_found(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_timed_lookup(lookup, monkeypatch, timed((DURATION_MS + 1, "а")))

    response = lookup.get(SYNCED_PATH)

    assert (response.status_code, response.json()["code"]) == (409, "synced_lyrics_not_found")


def test_an_oversized_timed_text_is_over_limits(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_timed_lookup(lookup, monkeypatch, timed(*((index, "а" * 400) for index in range(51))))

    response = lookup.get(SYNCED_PATH)

    assert (response.status_code, response.json()["code"]) == (409, "lyrics_over_limits")


def test_a_slow_lookup_is_cut_at_the_time_limit_as_an_unavailable_source(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
) -> None:
    lookup = build_lookup(settings, monkeypatch)

    async def never(*args: object) -> tuple[SourceAnswer | None, str]:
        await asyncio.sleep(3_600)
        return None, ""

    monkeypatch.setattr(lyrics_proposals, "look_up_timed_text", never)
    monkeypatch.setattr(lyrics_lookup, "LYRICS_LOOKUP_TIMEOUT_SECONDS", 0.01)

    response = lookup.get(SYNCED_PATH)

    assert (response.status_code, response.json()["code"]) == (503, "lyrics_source_unavailable")
    assert logger.events("lyrics_lookup_refused")[0]["reason"] == "lyrics_source_unavailable"


def test_a_database_error_under_the_cache_is_a_database_failure_not_a_silent_source(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    failing = AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("closed")))
    monkeypatch.setattr(lyrics_proposals, "look_up_timed_text", failing)

    response = lookup.get(SYNCED_PATH)

    assert (response.status_code, response.json()["code"]) == (503, "database_unavailable")


def test_a_missing_post_is_a_404(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = build_lookup(settings, monkeypatch, subject=None)

    for path in (SYNCED_PATH, plain_path("genius")):
        response = lookup.get(path)
        assert (response.status_code, response.json()["code"]) == (404, "post_not_found")


def test_without_a_http_client_the_lookup_answers_unavailable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = build_lookup(settings, monkeypatch, with_http_client=False)

    for path in (SYNCED_PATH, plain_path("genius")):
        response = lookup.get(path)
        assert (response.status_code, response.json()["code"]) == (503, "lyrics_source_unavailable")


@pytest.mark.parametrize("source", ["genius", "ytmusic", "lrclib"])
def test_plain_text_is_offered_for_every_source_without_times(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
    source: str,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    spy = replace_plain_lookup(lookup, monkeypatch, plain_result("раз", "", "♪", " два  три "))

    response = lookup.get(plain_path(source))

    assert response.status_code == 200
    assert response.json() == {
        "kind": "plain",
        "source": source,
        "lines": [{"text": "раз", "start_ms": None}, {"text": "два три", "start_ms": None}],
        "dropped_line_count": 2,
        "truncated_line_count": 0,
    }
    assert spy.await_args is not None
    assert spy.await_args.args[0] == source and spy.await_args.args[3] == QUERY
    found = logger.events("lyrics_lookup_found")[0]
    assert (found["kind"], found["source"], found["line_count"]) == ("plain", source, 2)
    assert found["dropped_line_count"] == 2


def test_a_timed_answer_becomes_plain_lines_when_the_admin_asks_for_plain_text(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_plain_lookup(lookup, monkeypatch, timed((1_000, "раз"), (2_000, "♪")).result)

    body = lookup.get(plain_path("ytmusic")).json()

    assert body["lines"] == [{"text": "раз", "start_ms": None}]


@pytest.mark.parametrize(
    ("source", "label"),
    [("genius", "Genius"), ("ytmusic", "YouTube Music"), ("lrclib", "LRCLIB")],
)
@pytest.mark.parametrize(
    "result",
    [
        LyricsResult(outcome=LyricsOutcome.ABSENT),
        LyricsResult(outcome=LyricsOutcome.INSTRUMENTAL),
        LyricsResult(outcome=LyricsOutcome.REJECTED_BY_MATCHING),
        plain_result("", "♪"),
    ],
)
def test_a_source_without_text_is_not_found_and_names_itself(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    label: str,
    result: LyricsResult,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_plain_lookup(lookup, monkeypatch, result)

    response = lookup.get(plain_path(source))

    assert response.status_code == 409
    assert response.json() == {
        "code": "plain_lyrics_not_found",
        "message": f"У {label} тексту цього треку не знайдено. Спробуйте інше джерело.",
    }


def test_a_silent_plain_source_is_unavailable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_plain_lookup(lookup, monkeypatch, LyricsResult(outcome=LyricsOutcome.SOURCE_UNAVAILABLE))

    response = lookup.get(plain_path("genius"))

    assert (response.status_code, response.json()["code"]) == (503, "lyrics_source_unavailable")


def test_a_slow_plain_source_is_cut_at_the_time_limit(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = build_lookup(settings, monkeypatch)

    async def never(*args: object) -> LyricsResult:
        await asyncio.sleep(3_600)
        return plain_result("а")

    monkeypatch.setattr(lyrics_proposals, "fetch_plain_text", never)
    monkeypatch.setattr(lyrics_lookup, "LYRICS_LOOKUP_TIMEOUT_SECONDS", 0.01)

    response = lookup.get(plain_path("lrclib"))

    assert (response.status_code, response.json()["code"]) == (503, "lyrics_source_unavailable")


def test_a_long_plain_line_is_cut_and_counted_and_an_oversized_text_is_over_limits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_plain_lookup(lookup, monkeypatch, plain_result("а" * 700, "б"))
    cut = lookup.get(plain_path("genius")).json()
    replace_plain_lookup(lookup, monkeypatch, plain_result(*(f"р{index}" for index in range(MAX_LYRIC_LINES + 1))))
    over = lookup.get(plain_path("genius"))

    assert (len(cut["lines"][0]["text"]), cut["truncated_line_count"]) == (MAX_LYRIC_TEXT_LENGTH, 1)
    assert (over.status_code, over.json()["code"]) == (409, "lyrics_over_limits")


def test_an_unknown_source_is_a_422_before_anything_is_read(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = build_lookup(settings, monkeypatch)

    for path in (plain_path("spotify"), f"/api/posts/{POST_ID}/lyrics/plain"):
        response = lookup.get(path)
        assert (response.status_code, response.json()["code"]) == (422, "invalid_request")


@pytest.mark.parametrize("source", ["genius", "ytmusic", "lrclib"])
@pytest.mark.parametrize(
    ("result", "code"),
    [
        (LyricsResult(outcome=LyricsOutcome.ABSENT), "plain_lyrics_not_found"),
        (LyricsResult(outcome=LyricsOutcome.SOURCE_UNAVAILABLE), "lyrics_source_unavailable"),
        (
            plain_result(*(f"р{index}" for index in range(MAX_LYRIC_LINES + 1))),
            "lyrics_over_limits",
        ),
    ],
)
def test_every_plain_log_line_names_the_source_and_every_synced_one_has_none(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
    source: str,
    result: LyricsResult,
    code: str,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_plain_lookup(lookup, monkeypatch, result)

    lookup.get(plain_path(source))

    requested = logger.events("lyrics_lookup_requested")
    refused = logger.events("lyrics_lookup_refused")
    assert [(entry["kind"], entry["source"]) for entry in requested] == [("plain", source)]
    assert [(entry["reason"], entry["kind"], entry["source"]) for entry in refused] == [(code, "plain", source)]


def test_the_plain_timeout_and_the_plain_success_name_the_source(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
) -> None:
    lookup = build_lookup(settings, monkeypatch)

    async def never(*args: object) -> LyricsResult:
        await asyncio.sleep(3_600)
        return plain_result("а")

    monkeypatch.setattr(lyrics_proposals, "fetch_plain_text", never)
    monkeypatch.setattr(lyrics_lookup, "LYRICS_LOOKUP_TIMEOUT_SECONDS", 0.01)
    lookup.get(plain_path("lrclib"))
    replace_plain_lookup(lookup, monkeypatch, plain_result("а"))
    lookup.get(plain_path("genius"))

    refused = logger.events("lyrics_lookup_refused")
    assert [(entry["reason"], entry["source"]) for entry in refused] == [("lyrics_source_unavailable", "lrclib")]
    assert [entry["source"] for entry in logger.events("lyrics_lookup_found")] == ["genius"]


def test_a_synced_lookup_logs_no_source_when_it_is_requested_or_refused(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    logger: RecordingLogger,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    replace_timed_lookup(lookup, monkeypatch, None, "lyrics_absent")

    lookup.get(SYNCED_PATH)

    entries = logger.events("lyrics_lookup_requested") + logger.events("lyrics_lookup_refused")
    assert len(entries) == 2
    assert [(entry["kind"], entry["source"]) for entry in entries] == [("synced", None)] * 2


def test_the_cache_of_the_lookups_uses_the_ttl_the_worker_uses(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ttl = 86_400
    configured = settings.model_copy(update={"source_fetch_ttl_seconds": ttl})
    lookup = build_lookup(configured, monkeypatch)
    spy = replace_timed_lookup(lookup, monkeypatch, timed((1_000, "а")))

    lookup.get(SYNCED_PATH)

    assert spy.await_args is not None
    assert spy.await_args.args[1].ttl_seconds == ttl


@pytest.mark.parametrize("path", [SYNCED_PATH, plain_path("genius")])
def test_an_anonymous_or_foreign_caller_is_refused_before_the_database(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    lookup = build_lookup(settings, monkeypatch)
    spy = replace_timed_lookup(lookup, monkeypatch, None)

    assert lookup.client.get(path).status_code == 401
    assert lookup.client.get(path, headers=bearer(999)).status_code == 403
    spy.assert_not_awaited()
    lookup.subject_read.assert_not_awaited()
