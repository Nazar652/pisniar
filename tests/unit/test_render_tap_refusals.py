"""The render button's claim over a `Post`, no database: which taps move the post
to `downloading` and which are refused before any status is written.
"""

from __future__ import annotations

import pytest

from pisniar.bot import handlers
from pisniar.db.models import Post
from pisniar.db.states import PostStatus


def post_in(status: PostStatus, *, preview_message_id: int | None) -> Post:
    post = Post(id=7, track_id=1, created_by_user_id=111)
    post.status = status
    post.failed_from_status = PostStatus.PUBLISHING if status is PostStatus.FAILED else None
    post.draft_chat_id, post.draft_message_id = 555, 41
    if preview_message_id is not None:
        post.preview_chat_id, post.preview_message_id = 555, preview_message_id
    return post


def test_a_tap_on_a_previewed_post_is_refused_before_the_status_moves() -> None:
    """a surviving draft button must not start a second render: the channel gets
    the newest `render` row, and the admin has only seen the first."""
    post = post_in(PostStatus.FAILED, preview_message_id=99)

    refusal, reference = handlers.claim_post_for_media(post, 7, 111)

    assert refusal == handlers.PREVIEW_ALREADY_SHOWN_ANSWER.format(post_id=7)
    assert reference is None
    assert post.status is PostStatus.FAILED


@pytest.mark.parametrize("preview_message_id", [None, 99])
def test_a_tap_on_a_publishing_post_says_it_is_publishing(preview_message_id: int | None) -> None:
    post = post_in(PostStatus.PUBLISHING, preview_message_id=preview_message_id)

    refusal, reference = handlers.claim_post_for_media(post, 7, 111)

    assert refusal == handlers.ALREADY_PUBLISHING_ANSWER.format(post_id=7)
    assert reference is None
    assert post.status is PostStatus.PUBLISHING


def test_the_refusal_names_no_state() -> None:
    answer = handlers.PREVIEW_ALREADY_SHOWN_ANSWER.format(post_id=7)

    assert not any(status.value in answer for status in PostStatus)


def test_a_failed_post_without_a_preview_can_still_be_rendered() -> None:
    post = post_in(PostStatus.FAILED, preview_message_id=None)

    refusal, reference = handlers.claim_post_for_media(post, 7, 111)

    assert refusal is None
    assert reference == (555, 41)
    assert post.status is PostStatus.DOWNLOADING
