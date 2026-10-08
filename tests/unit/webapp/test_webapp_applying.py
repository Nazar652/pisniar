"""Applying a caption and archiving a post with the rows and Telegram faked: which messages
are edited with which keyboard, the order of Telegram and the database, and every refusal.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import EditMessageCaption
from test_webapp_editing import make_rows

from pisniar.db.models import Post, Track
from pisniar.db.states import ALLOWED_TRANSITIONS, PostStatus
from pisniar.telegram.caption_edit import EditOutcome, MessageProbe
from pisniar.webapp import applying
from pisniar.webapp.applying import (
    AppliedCaption,
    EditTarget,
    apply_caption,
    archive_post,
    edit_remaining,
    message_targets,
    refuse_telegram_error,
)
from pisniar.webapp.errors import ApiProblem
from pisniar.webapp.schemas import ApplyResponse
from pisniar.webapp.service import caption_sha256, rebuild_caption

METHOD = EditMessageCaption(chat_id=1, message_id=2, caption="x")
URL = "https://pisniar.example.ts.net"
DRAFT = (5, 100)
PREVIEW = (5, 200)
CHANNEL = (-1001, 300)


def make_post(status: PostStatus, **references: Any) -> tuple[Post, Track]:
    post, track = make_rows(status)
    post.draft_chat_id, post.draft_message_id = references.get("draft", DRAFT)
    post.preview_chat_id, post.preview_message_id = references.get("preview", (None, None))
    post.channel_chat_id, post.channel_message_id = references.get("channel", (None, None))
    post.channel_message_kind = references.get("kind")
    return post, track


def caption_of(post: Post, track: Track) -> tuple[str, str]:
    view, html = rebuild_caption(post, track)
    assert view is not None
    assert html is not None
    return caption_sha256(html), html


@pytest.fixture
def session() -> MagicMock:
    fake = MagicMock()
    fake.flush = AsyncMock()
    fake.get = AsyncMock()
    return fake


def arrange(
    monkeypatch: pytest.MonkeyPatch,
    session: MagicMock,
    post: Post | None,
    track: Track | None,
    edit: Any = None,
) -> AsyncMock:
    monkeypatch.setattr(applying, "lock_post_without_waiting", AsyncMock(return_value=post))
    session.get.return_value = track
    mock = edit if edit is not None else AsyncMock(return_value=EditOutcome.EDITED)
    monkeypatch.setattr(applying, "edit_message_caption_by_kind", mock)
    return mock


def keyboard_buttons(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


def test_a_resolved_post_edits_its_draft_with_the_draft_keyboard() -> None:
    post, _ = make_post(PostStatus.RESOLVED)

    targets = message_targets(post, URL)

    assert [(t.role, t.chat_id, t.message_id, t.message_kind) for t in targets] == [
        ("draft", 5, 100, "text")
    ]
    assert keyboard_buttons(targets[0].reply_markup)


def test_a_rendered_post_edits_the_preview_first_with_its_keyboard_and_the_draft_bare() -> None:
    post, _ = make_post(PostStatus.RENDERED, preview=PREVIEW)

    targets = message_targets(post, URL)

    assert [(t.role, t.message_kind) for t in targets] == [("preview", "video"), ("draft", "text")]
    assert keyboard_buttons(targets[0].reply_markup)
    assert targets[1].reply_markup is None


@pytest.mark.parametrize("with_preview", [False, True])
def test_a_failed_post_edits_the_draft_and_any_preview_without_a_keyboard(
    with_preview: bool,
) -> None:
    post, _ = make_post(PostStatus.FAILED, preview=PREVIEW if with_preview else (None, None))

    targets = message_targets(post, URL)

    assert [t.role for t in targets] == (["draft", "preview"] if with_preview else ["draft"])
    assert all(t.reply_markup is None for t in targets)


@pytest.mark.parametrize("kind", ["photo", "video", "text"])
def test_a_published_post_edits_the_channel_message_by_its_kind_without_a_keyboard(
    kind: str,
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED, channel=CHANNEL, kind=kind)

    targets = message_targets(post, URL)

    assert targets == [EditTarget("channel", -1001, 300, kind, None)]


def test_a_published_post_without_a_channel_reference_has_no_target() -> None:
    post, _ = make_post(PostStatus.PUBLISHED)

    assert message_targets(post, URL) == []


def test_a_post_without_any_message_has_no_target() -> None:
    post, _ = make_post(PostStatus.FAILED, draft=(None, None))

    assert message_targets(post, URL) == []


async def test_telegram_is_edited_before_the_caption_is_stored(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, html = caption_of(post, track)
    seen: list[str | None] = []

    async def edit(*args: Any, **kwargs: Any) -> EditOutcome:
        seen.append(post.caption)
        return EditOutcome.EDITED

    arrange(monkeypatch, session, post, track, AsyncMock(side_effect=edit))

    applied = await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert seen == [None]
    assert post.caption == html
    assert applied is not None
    assert applied.response.result == "edited"
    assert applied.response.caption_sha256 == sha
    session.flush.assert_awaited_once()


async def test_the_first_message_is_edited_with_html_a_fifteen_second_timeout_and_its_keyboard(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, html = caption_of(post, track)
    edit = arrange(monkeypatch, session, post, track)
    bot = MagicMock()

    await apply_caption(session, bot, 7, sha, 111, URL)

    call = edit.await_args_list[0]
    assert call.args[:5] == (bot, 5, 100, "text", html)
    assert keyboard_buttons(call.args[5])
    assert call.kwargs == {"request_timeout": 15}


async def test_not_modified_is_a_success_that_still_stores_the_caption(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, html = caption_of(post, track)
    arrange(monkeypatch, session, post, track, AsyncMock(return_value=EditOutcome.NOT_MODIFIED))

    applied = await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert applied is not None
    assert applied.response.result == "not_modified"
    assert post.caption == html


async def test_a_deleted_message_is_a_409_and_the_caption_stays(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, _ = caption_of(post, track)
    arrange(monkeypatch, session, post, track, AsyncMock(return_value=EditOutcome.MESSAGE_MISSING))

    with pytest.raises(ApiProblem) as raised:
        await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert (raised.value.status_code, raised.value.code) == (409, "channel_message_missing")
    assert post.caption is None
    session.flush.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            TelegramRetryAfter(method=METHOD, message="Too Many", retry_after=11),
            429,
            "telegram_rate_limited",
        ),
        (
            TelegramForbiddenError(method=METHOD, message="Forbidden: kicked"),
            502,
            "telegram_forbidden",
        ),
        (
            TelegramBadRequest(method=METHOD, message="Bad Request: chat not found"),
            502,
            "telegram_failed",
        ),
        (TelegramNetworkError(method=METHOD, message="timeout"), 502, "telegram_failed"),
        (ValueError("unsupported kind"), 502, "telegram_failed"),
    ],
)
async def test_a_telegram_failure_rolls_back_with_its_own_sentence(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock, error: Exception, status: int, code: str
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, _ = caption_of(post, track)
    arrange(monkeypatch, session, post, track, AsyncMock(side_effect=error))

    with pytest.raises(ApiProblem) as raised:
        await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert (raised.value.status_code, raised.value.code) == (status, code)
    assert post.caption is None
    session.flush.assert_not_awaited()


def test_a_rate_limit_names_the_seconds_and_a_forbidden_answer_the_lost_right() -> None:
    with pytest.raises(ApiProblem) as limited:
        refuse_telegram_error(
            TelegramRetryAfter(method=METHOD, message="x", retry_after=11), "event", 1, 7
        )
    with pytest.raises(ApiProblem) as forbidden:
        refuse_telegram_error(TelegramForbiddenError(method=METHOD, message="x"), "event", 1, 7)
    with pytest.raises(ApiProblem) as other:
        refuse_telegram_error(RuntimeError("secret detail"), "event", 1, 7)

    assert "11 с" in limited.value.message
    assert "403" in forbidden.value.message
    assert other.value.message == applying.TELEGRAM_FAILED_MESSAGE
    assert "secret detail" not in other.value.message


async def test_a_stale_hash_is_a_409_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    edit = arrange(monkeypatch, session, post, track)

    with pytest.raises(ApiProblem) as raised:
        await apply_caption(session, MagicMock(), 7, "0" * 64, 111, URL)

    assert (raised.value.status_code, raised.value.code) == (409, "caption_changed")
    edit.assert_not_awaited()
    assert post.caption is None


async def test_a_caption_over_the_limit_is_a_422_with_the_number(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    track.title = "я" * 1200
    sha, _ = caption_of(post, track)
    edit = arrange(monkeypatch, session, post, track)

    with pytest.raises(ApiProblem) as raised:
        await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert (raised.value.status_code, raised.value.code) == (422, "caption_too_long")
    assert "1024" in raised.value.message
    edit.assert_not_awaited()


async def test_a_post_that_cannot_be_edited_is_refused_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.PUBLISHING)
    edit = arrange(monkeypatch, session, post, track)

    with pytest.raises(ApiProblem) as raised:
        await apply_caption(session, MagicMock(), 7, "0" * 64, 111, URL)

    assert raised.value.code == "not_editable"
    session.get.assert_not_awaited()
    edit.assert_not_awaited()


async def test_a_missing_post_is_none(monkeypatch: pytest.MonkeyPatch, session: MagicMock) -> None:
    arrange(monkeypatch, session, None, None)

    assert await apply_caption(session, MagicMock(), 7, "0" * 64, 111, URL) is None


async def test_a_missing_track_or_title_is_a_409(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    arrange(monkeypatch, session, post, None)
    with pytest.raises(ApiProblem) as without_track:
        await apply_caption(session, MagicMock(), 7, "0" * 64, 111, URL)

    track.title = ""
    arrange(monkeypatch, session, post, track)
    with pytest.raises(ApiProblem) as without_title:
        await apply_caption(session, MagicMock(), 7, "0" * 64, 111, URL)

    assert without_track.value.code == without_title.value.code == "caption_unavailable"


async def test_the_track_is_read_with_a_plain_get_and_never_locked(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RESOLVED)
    sha, _ = caption_of(post, track)
    arrange(monkeypatch, session, post, track)

    await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert session.get.await_args_list[0].args == (Track, post.track_id)
    assert session.get.await_args_list[0].kwargs == {}


async def test_without_a_message_the_caption_is_only_stored(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.FAILED, draft=(None, None))
    post.failed_from_status = PostStatus.RENDERING
    sha, html = caption_of(post, track)
    edit = arrange(monkeypatch, session, post, track)

    applied = await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert applied is not None
    assert applied.response.result == "stored_only"
    assert post.caption == html
    edit.assert_not_awaited()


async def test_a_rendered_post_returns_the_draft_to_edit_after_the_commit(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, track = make_post(PostStatus.RENDERED, preview=PREVIEW)
    sha, _ = caption_of(post, track)
    edit = arrange(monkeypatch, session, post, track)

    applied = await apply_caption(session, MagicMock(), 7, sha, 111, URL)

    assert applied is not None
    assert edit.await_count == 1
    assert edit.await_args_list[0].args[1:4] == (5, 200, "video")
    assert [target.role for target in applied.remaining] == ["draft"]


def make_applied(*roles: str) -> AppliedCaption:
    remaining = [EditTarget(role, 5, 100 + index, "text", None) for index, role in enumerate(roles)]
    response = ApplyResponse(caption_sha256="a" * 64, result="edited", other_messages_failed=[])
    return AppliedCaption(response=response, html="<b>x</b>", remaining=remaining)


async def test_the_remaining_messages_are_edited_and_a_failure_is_named_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edit = AsyncMock(
        side_effect=[
            TelegramNetworkError(method=METHOD, message="boom"),
            EditOutcome.MESSAGE_MISSING,
            EditOutcome.NOT_MODIFIED,
        ]
    )
    monkeypatch.setattr(applying, "edit_message_caption_by_kind", edit)

    response = await edit_remaining(MagicMock(), make_applied("draft", "preview", "extra"), 111, 7)

    assert response.other_messages_failed == ["draft", "preview"]
    assert edit.await_count == 3
    assert response.result == "edited"


async def test_no_remaining_messages_means_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    edit = AsyncMock()
    monkeypatch.setattr(applying, "edit_message_caption_by_kind", edit)

    response = await edit_remaining(MagicMock(), make_applied(), 111, 7)

    assert response.other_messages_failed == []
    edit.assert_not_awaited()


def arrange_archive(monkeypatch: pytest.MonkeyPatch, post: Post | None, probe: Any) -> AsyncMock:
    monkeypatch.setattr(applying, "lock_post_without_waiting", AsyncMock(return_value=post))
    mock = (
        AsyncMock(side_effect=probe)
        if isinstance(probe, Exception)
        else AsyncMock(return_value=probe)
    )
    monkeypatch.setattr(applying, "probe_message", mock)
    return mock


async def test_a_deleted_channel_message_archives_the_post(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED, channel=CHANNEL, kind="video")
    probe = arrange_archive(monkeypatch, post, MessageProbe.MISSING)
    bot = MagicMock()

    result = await archive_post(session, bot, 7, 111)

    assert result is not None
    assert (result.post_id, post.status) == (7, PostStatus.ARCHIVED)
    assert probe.await_args_list[0].args == (bot, -1001, 300)
    assert probe.await_args_list[0].kwargs == {"request_timeout": 15}
    session.flush.assert_awaited_once()


async def test_a_live_channel_message_is_a_409_and_the_post_stays(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED, channel=CHANNEL, kind="video")
    arrange_archive(monkeypatch, post, MessageProbe.EXISTS)

    with pytest.raises(ApiProblem) as raised:
        await archive_post(session, MagicMock(), 7, 111)

    assert (raised.value.status_code, raised.value.code) == (409, "channel_message_exists")
    assert post.status == PostStatus.PUBLISHED


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=METHOD, message="Bad Request: chat not found"),
        TelegramForbiddenError(method=METHOD, message="Forbidden"),
        TelegramNetworkError(method=METHOD, message="timeout"),
        TelegramRetryAfter(method=METHOD, message="x", retry_after=3),
    ],
)
async def test_any_other_answer_refuses_without_changing_the_post(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock, error: Exception
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED, channel=CHANNEL, kind="video")
    arrange_archive(monkeypatch, post, error)

    with pytest.raises(ApiProblem):
        await archive_post(session, MagicMock(), 7, 111)

    assert post.status == PostStatus.PUBLISHED
    session.flush.assert_not_awaited()


@pytest.mark.parametrize("status", [PostStatus.RESOLVED, PostStatus.RENDERED, PostStatus.FAILED])
async def test_only_a_published_post_can_be_archived_this_way(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock, status: PostStatus
) -> None:
    post, _ = make_post(status, channel=CHANNEL, kind="video")
    probe = arrange_archive(monkeypatch, post, MessageProbe.MISSING)

    with pytest.raises(ApiProblem) as raised:
        await archive_post(session, MagicMock(), 7, 111)

    assert (raised.value.status_code, raised.value.code) == (409, "not_published")
    assert "published" not in raised.value.message.lower()
    probe.assert_not_awaited()


async def test_a_published_post_without_a_channel_reference_is_not_probed(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    post, _ = make_post(PostStatus.PUBLISHED)
    probe = arrange_archive(monkeypatch, post, MessageProbe.MISSING)

    with pytest.raises(ApiProblem):
        await archive_post(session, MagicMock(), 7, 111)

    probe.assert_not_awaited()


async def test_archiving_a_missing_post_is_none(
    monkeypatch: pytest.MonkeyPatch, session: MagicMock
) -> None:
    arrange_archive(monkeypatch, None, MessageProbe.MISSING)

    assert await archive_post(session, MagicMock(), 7, 111) is None


def test_the_archive_transition_exists_for_a_published_post() -> None:
    assert PostStatus.ARCHIVED in ALLOWED_TRANSITIONS[PostStatus.PUBLISHED]
