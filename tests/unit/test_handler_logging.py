"""Publication is the most consequential action in the system, so every guard
names the branch it took: a post that failed twice and then published can be
read back out of the logs instead of out of the admin's memory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.bot import handlers
from pisniar.db.models import Post
from pisniar.db.states import PostStatus, set_post_status

PUBLISHER_ID = 222
POST_ID = 7


def capture_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)
    return fake_logger


def warnings_named(fake_logger: MagicMock, event: str) -> list[dict[str, Any]]:
    return [
        warning_call.kwargs
        for warning_call in fake_logger.warning.call_args_list
        if warning_call.args[:1] == (event,)
    ]


def make_post(status: PostStatus | None = None) -> Post:
    post = Post(track_id=1, created_by_user_id=111)
    if status is not None:
        set_post_status(post, status)
    return post


def test_a_missing_post_is_refused_with_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_logger = capture_logger(monkeypatch)

    refusal, draft_reference = handlers.claim_post_for_publishing(None, POST_ID, PUBLISHER_ID)

    assert refusal is not None
    assert draft_reference is None
    assert warnings_named(fake_logger, "publish_refused") == [
        {"reason": "post_not_found", "user_id": PUBLISHER_ID, "post_id": POST_ID}
    ]


def test_an_already_published_post_is_refused_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = capture_logger(monkeypatch)
    post = make_post(PostStatus.RESOLVING)
    post.channel_message_id = 55

    refusal, _ = handlers.claim_post_for_publishing(post, POST_ID, PUBLISHER_ID)

    assert refusal is not None
    assert warnings_named(fake_logger, "publish_refused")[0]["reason"] == "already_published"


def test_a_second_tap_while_publishing_is_refused_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = capture_logger(monkeypatch)
    post = make_post()
    for step in (PostStatus.RESOLVING, PostStatus.RESOLVED, PostStatus.PUBLISHING):
        set_post_status(post, step)

    refusal, _ = handlers.claim_post_for_publishing(post, POST_ID, PUBLISHER_ID)

    assert refusal is not None
    assert warnings_named(fake_logger, "publish_refused")[0]["reason"] == "already_publishing"


def test_an_illegal_status_is_refused_with_the_status_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = capture_logger(monkeypatch)
    post = make_post(PostStatus.RESOLVING)
    post.preview_chat_id, post.preview_message_id = 111, 300

    refusal, _ = handlers.claim_post_for_publishing(post, POST_ID, PUBLISHER_ID, (111, 300))

    assert refusal is not None
    refusal_fields = warnings_named(fake_logger, "publish_refused")[0]
    assert refusal_fields["reason"] == "illegal_status"
    assert refusal_fields["status"] == "resolving"


def test_a_tap_on_a_draft_without_a_preview_points_to_the_editor_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = capture_logger(monkeypatch)
    post = make_post()
    for step in (PostStatus.RESOLVING, PostStatus.RESOLVED):
        set_post_status(post, step)

    refusal, reference = handlers.claim_post_for_publishing(post, POST_ID, PUBLISHER_ID)

    assert refusal == handlers.COVER_IN_EDITOR_ANSWER.format(post_id=POST_ID)
    assert len(refusal) <= 200
    assert reference is None
    assert post.status is PostStatus.RESOLVED
    assert warnings_named(fake_logger, "publish_refused")[0]["reason"] == (
        "cover_publication_in_editor"
    )


def test_a_tap_under_another_message_than_the_preview_is_refused_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_logger = capture_logger(monkeypatch)
    post = make_post()
    for step in (PostStatus.RESOLVING, PostStatus.RESOLVED, PostStatus.DOWNLOADING):
        set_post_status(post, step)
    for step in (PostStatus.DOWNLOADED, PostStatus.RENDERING, PostStatus.RENDERED):
        set_post_status(post, step)
    post.preview_chat_id = 111
    post.preview_message_id = 300

    refusal, _ = handlers.claim_post_for_publishing(post, POST_ID, PUBLISHER_ID, (111, 299))
    claimed, reference = handlers.claim_post_for_publishing(post, POST_ID, PUBLISHER_ID, (111, 300))

    assert refusal is not None
    assert warnings_named(fake_logger, "publish_refused")[0]["reason"] == "outdated_preview"
    assert (claimed, reference) == (None, (111, 300))


def test_a_claimed_post_is_not_logged_as_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_logger = capture_logger(monkeypatch)
    post = make_post()
    for step in (PostStatus.RESOLVING, PostStatus.RESOLVED):
        set_post_status(post, step)
    post.draft_chat_id = 111
    post.draft_message_id = 222
    post.preview_chat_id, post.preview_message_id = 111, 300

    refusal, draft_reference = handlers.claim_post_for_publishing(
        post, POST_ID, PUBLISHER_ID, (111, 300)
    )

    assert refusal is None
    assert draft_reference == (111, 300)
    assert warnings_named(fake_logger, "publish_refused") == []


async def test_a_malformed_callback_logs_who_sent_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_logger = capture_logger(monkeypatch)
    callback = AsyncMock()
    callback.data = "publish:not-an-id"
    callback.from_user = SimpleNamespace(id=PUBLISHER_ID)

    await handlers.handle_callback(callback, AsyncMock(), MagicMock(), MagicMock())

    assert warnings_named(fake_logger, "callback_rejected") == [
        {"reason": "unparsable_callback_data", "user_id": PUBLISHER_ID}
    ]


async def test_a_malformed_retry_command_leaves_a_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_logger = capture_logger(monkeypatch)
    message = AsyncMock()
    message.text = "/retry обовязково"
    message.from_user = SimpleNamespace(id=PUBLISHER_ID)

    await handlers.handle_retry(message, MagicMock(), MagicMock())

    refusal_fields = warnings_named(fake_logger, "retry_refused")[0]
    assert refusal_fields == {"reason": "bad_arguments", "user_id": PUBLISHER_ID}
    # the admin's own text is not a post id and has no business in the logs.
    assert not any("обовязково" in str(value) for value in refusal_fields.values())
