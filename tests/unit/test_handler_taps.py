"""The inline-button taps and the dispatch helpers with the database faked: database
first, keyboard second, dispatch last; a refused tap keeps its button; a failed dispatch
fails the post. Also the three syntactic refusals of a submitted link.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendPhoto
from sqlalchemy.exc import DBAPIError

from pisniar.bot import handlers
from pisniar.db.models import Post
from pisniar.db.states import PostStatus
from pisniar.resolve.urls import MAX_NORMALIZED_URL_BYTES, classify_url
from pisniar.worker.celery_app import (
    DOWNLOAD_POST_AUDIO_TASK_NAME,
    PUBLISH_POST_TASK_NAME,
    RESOLVE_POST_TASK_NAME,
)

POST_ID = 7
USER_ID = 111
DRAFT_REFERENCE = (555, 41)


class FakeSession:
    def __init__(
        self,
        post: Post | None,
        lock_error: bool,
        order: list[str],
    ) -> None:
        self.post = post
        self.lock_error = lock_error
        self.order = order
        self.rollback = AsyncMock()

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exception_info: object) -> None:
        return None

    async def get(
        self,
        model: type,
        identifier: int,
        **options: Any,
    ) -> Post | None:
        assert options == {"with_for_update": {"nowait": True}}
        if self.lock_error:
            raise DBAPIError("SELECT ... FOR UPDATE NOWAIT", {}, Exception("lock not available"))
        return self.post

    async def commit(self) -> None:
        self.order.append("commit")


def make_post(status: PostStatus = PostStatus.RESOLVED, **overrides: Any) -> Post:
    post = Post(id=POST_ID, track_id=1, created_by_user_id=USER_ID)
    post.status = status
    post.draft_chat_id, post.draft_message_id = DRAFT_REFERENCE
    for name, value in overrides.items():
        setattr(post, name, value)
    return post


@pytest.fixture
def tap(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    order: list[str] = []
    state = SimpleNamespace(post=make_post(), lock_error=False, order=order)

    def session_factory() -> FakeSession:
        return FakeSession(state.post, state.lock_error, order)

    callback = MagicMock()
    callback.answer = AsyncMock(side_effect=lambda *args, **kwargs: order.append("answer"))
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock(side_effect=lambda **kwargs: order.append("keyboard"))
    celery_client = MagicMock()
    celery_client.send_task = MagicMock(side_effect=lambda *args, **kwargs: order.append("dispatch"))
    mark_failure = AsyncMock()
    monkeypatch.setattr(handlers, "mark_dispatch_failure", mark_failure)

    state.session_factory = session_factory
    state.callback = callback
    state.bot = bot
    state.celery_client = celery_client
    state.mark_failure = mark_failure
    return state


async def tap_publish(tap: SimpleNamespace) -> None:
    await handlers.handle_publish_tap(tap.callback, POST_ID, USER_ID, tap.bot, tap.session_factory, tap.celery_client)


async def tap_render(tap: SimpleNamespace) -> None:
    await handlers.handle_render_tap(tap.callback, POST_ID, USER_ID, tap.bot, tap.session_factory, tap.celery_client)


PREVIEW_REFERENCE = (777, 88)


def tap_under_the_preview(tap: SimpleNamespace) -> None:
    """a publish tap is valid only under the video preview of a rendered post."""
    tap.post = make_post(
        PostStatus.RENDERED,
        preview_chat_id=PREVIEW_REFERENCE[0],
        preview_message_id=PREVIEW_REFERENCE[1],
    )
    tap.callback.message.chat.id, tap.callback.message.message_id = PREVIEW_REFERENCE


async def test_a_publish_tap_runs_database_then_keyboard_then_dispatch(
    tap: SimpleNamespace,
) -> None:
    tap_under_the_preview(tap)

    await tap_publish(tap)

    assert tap.post.status is PostStatus.PUBLISHING
    assert tap.order == ["commit", "keyboard", "dispatch", "answer"]
    keyboard_call = tap.bot.edit_message_reply_markup.await_args
    assert keyboard_call.kwargs == {
        "chat_id": PREVIEW_REFERENCE[0],
        "message_id": PREVIEW_REFERENCE[1],
        "reply_markup": None,
    }
    tap.celery_client.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME, kwargs={"post_id": POST_ID, "published_by_user_id": USER_ID}
    )
    assert tap.callback.answer.await_args.args == ("Публікую…",)


async def test_a_refused_publish_tap_keeps_its_button_and_dispatches_nothing(
    tap: SimpleNamespace,
) -> None:
    tap.post = make_post(PostStatus.PUBLISHED, channel_message_id=500)

    await tap_publish(tap)

    tap.bot.edit_message_reply_markup.assert_not_awaited()
    tap.celery_client.send_task.assert_not_called()
    assert tap.callback.answer.await_args.kwargs == {"show_alert": True}
    assert "уже опубліковано" in tap.callback.answer.await_args.args[0]


async def test_a_publish_tap_on_a_locked_post_is_refused_with_a_sentence(
    tap: SimpleNamespace,
) -> None:
    tap.lock_error = True

    await tap_publish(tap)

    tap.bot.edit_message_reply_markup.assert_not_awaited()
    tap.celery_client.send_task.assert_not_called()
    assert "зараз обробляється" in tap.callback.answer.await_args.args[0]


async def test_a_missing_post_is_refused_on_both_buttons(tap: SimpleNamespace) -> None:
    tap.post = None

    await tap_publish(tap)
    await tap_render(tap)

    answers = [call.args[0] for call in tap.callback.answer.await_args_list]
    assert answers == [f"Пост {POST_ID} не знайдено."] * 2
    tap.celery_client.send_task.assert_not_called()


async def test_a_failed_publish_dispatch_fails_the_post_and_tells_the_admin(
    tap: SimpleNamespace,
) -> None:
    tap_under_the_preview(tap)
    tap.celery_client.send_task = MagicMock(side_effect=ConnectionError("redis is down"))

    await tap_publish(tap)

    tap.mark_failure.assert_awaited_once_with(
        tap.session_factory,
        POST_ID,
        expected_status=PostStatus.PUBLISHING,
        error=handlers.PUBLISH_DISPATCH_FAILED_ERROR,
    )
    assert f"/retry {POST_ID}" in tap.callback.answer.await_args.args[0]
    assert tap.callback.answer.await_args.kwargs == {"show_alert": True}


async def test_a_keyboard_that_cannot_be_removed_does_not_stop_the_dispatch(
    tap: SimpleNamespace,
) -> None:
    tap_under_the_preview(tap)
    tap.bot.edit_message_reply_markup = AsyncMock(
        side_effect=TelegramAPIError(method=SendPhoto(chat_id=1, photo="x"), message="gone")
    )

    await tap_publish(tap)

    tap.celery_client.send_task.assert_called_once()


async def test_a_publish_tap_on_a_draft_is_refused_with_the_editor_pointer(
    tap: SimpleNamespace,
) -> None:
    await tap_publish(tap)

    assert tap.post.status is PostStatus.RESOLVED
    assert tap.order == ["commit", "answer"]
    tap.bot.edit_message_reply_markup.assert_not_awaited()
    tap.celery_client.send_task.assert_not_called()
    assert tap.callback.answer.await_args.args[0] == handlers.COVER_IN_EDITOR_ANSWER.format(post_id=POST_ID)


@pytest.mark.parametrize("failed_from", [PostStatus.DOWNLOADED, PostStatus.RENDERING])
async def test_a_publish_tap_on_a_draft_whose_video_failed_points_at_retry(
    failed_from: PostStatus, tap: SimpleNamespace
) -> None:
    tap.post = make_post(PostStatus.FAILED, failed_from_status=failed_from)

    await tap_publish(tap)

    tap.celery_client.send_task.assert_not_called()
    assert tap.callback.answer.await_args.args[0] == handlers.MEDIA_FAILED_PUBLISH_ANSWER.format(post_id=POST_ID)


async def test_a_render_tap_runs_database_then_keyboard_then_dispatch(
    tap: SimpleNamespace,
) -> None:
    await tap_render(tap)

    assert tap.post.status is PostStatus.DOWNLOADING
    assert tap.order == ["commit", "keyboard", "dispatch", "answer"]
    tap.celery_client.send_task.assert_called_once_with(DOWNLOAD_POST_AUDIO_TASK_NAME, kwargs={"post_id": POST_ID})
    assert "Роблю відео" in tap.callback.answer.await_args.args[0]


async def test_a_refused_render_tap_keeps_its_button(tap: SimpleNamespace) -> None:
    tap.post = make_post(PostStatus.RENDERED)

    await tap_render(tap)

    tap.bot.edit_message_reply_markup.assert_not_awaited()
    tap.celery_client.send_task.assert_not_called()
    assert tap.callback.answer.await_args.args[0] == handlers.VIDEO_READY_ANSWER.format(post_id=POST_ID)


@pytest.mark.parametrize("tap_button", [tap_publish, tap_render])
async def test_a_tap_on_a_post_awaiting_its_fragment_choice_is_refused_and_changes_nothing(
    tap_button: Any, tap: SimpleNamespace
) -> None:
    tap.post = make_post(PostStatus.EDITING)

    await tap_button(tap)

    assert tap.post.status is PostStatus.EDITING
    assert tap.order == ["commit", "answer"]
    tap.bot.edit_message_reply_markup.assert_not_awaited()
    tap.celery_client.send_task.assert_not_called()
    assert tap.callback.answer.await_args.args[0] == handlers.AUDIO_READY_ANSWER.format(post_id=POST_ID)
    assert "editing" not in tap.callback.answer.await_args.args[0]


async def test_a_render_tap_on_a_locked_post_is_refused_with_a_sentence(
    tap: SimpleNamespace,
) -> None:
    tap.lock_error = True

    await tap_render(tap)

    tap.celery_client.send_task.assert_not_called()
    assert "зараз обробляється" in tap.callback.answer.await_args.args[0]


async def test_a_failed_render_dispatch_fails_the_post_from_downloading(
    tap: SimpleNamespace,
) -> None:
    tap.celery_client.send_task = MagicMock(side_effect=ConnectionError("redis is down"))

    await tap_render(tap)

    tap.mark_failure.assert_awaited_once_with(
        tap.session_factory,
        POST_ID,
        expected_status=PostStatus.DOWNLOADING,
        error=handlers.DOWNLOAD_DISPATCH_FAILED_ERROR,
    )
    assert f"/retry {POST_ID}" in tap.callback.answer.await_args.args[0]


async def test_the_resolve_dispatch_sends_the_task_by_name_and_reports_success() -> None:
    celery_client = MagicMock()

    assert await handlers.dispatch_resolve_task(celery_client, POST_ID) is True

    celery_client.send_task.assert_called_once_with(RESOLVE_POST_TASK_NAME, kwargs={"post_id": POST_ID})


async def test_the_resolve_dispatch_reports_a_broker_failure() -> None:
    celery_client = MagicMock()
    celery_client.send_task.side_effect = ConnectionError("redis is down")

    assert await handlers.dispatch_resolve_task(celery_client, POST_ID) is False


async def test_the_publish_dispatch_carries_who_published() -> None:
    celery_client = MagicMock()

    assert await handlers.dispatch_publish_task(celery_client, POST_ID, 222) is True

    celery_client.send_task.assert_called_once_with(
        PUBLISH_POST_TASK_NAME, kwargs={"post_id": POST_ID, "published_by_user_id": 222}
    )


async def test_the_publish_dispatch_reports_a_broker_failure() -> None:
    celery_client = MagicMock()
    celery_client.send_task.side_effect = ConnectionError("redis is down")

    assert await handlers.dispatch_publish_task(celery_client, POST_ID, 222) is False


async def test_the_media_dispatch_reports_a_broker_failure() -> None:
    celery_client = MagicMock()
    celery_client.send_task.side_effect = ConnectionError("redis is down")

    assert await handlers.dispatch_media_task(celery_client, DOWNLOAD_POST_AUDIO_TASK_NAME, POST_ID) is False


def test_a_too_long_link_is_refused_with_the_byte_counts() -> None:
    raw_url = "https://open.spotify.com/track/" + "a" * (MAX_NORMALIZED_URL_BYTES + 10)

    answer, fields = handlers.reject_url(raw_url, classify_url(raw_url)) or ("", {})

    assert str(MAX_NORMALIZED_URL_BYTES) in answer
    assert fields["reason"] == "too_long"
    assert fields["limit_bytes"] == MAX_NORMALIZED_URL_BYTES
    assert fields["raw_url_bytes"] == len(raw_url.encode())


@pytest.mark.parametrize(
    ("raw_url", "noun"),
    [
        ("https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j", "альбом"),
        ("https://open.spotify.com/artist/4yYXqedvSKuJlgI2IWsB0j", "артиста"),
        ("https://www.deezer.com/playlist/123", "плейлист"),
    ],
)
def test_a_link_to_something_that_is_not_a_track_names_what_it_is(raw_url: str, noun: str) -> None:
    answer, fields = handlers.reject_url(raw_url, classify_url(raw_url)) or ("", {})

    assert noun in answer
    assert fields["reason"] == "not_track"
    assert fields["normalized_url"] == classify_url(raw_url).normalized_url


def test_an_unrecognised_message_logs_only_its_length() -> None:
    raw_text = "привіт, це не посилання"

    answer, fields = handlers.reject_url(raw_text, classify_url(raw_text)) or ("", {})

    assert answer == "Не розпізнав це як посилання на трек."
    assert fields == {"reason": "unknown", "raw_text_length": len(raw_text)}


def test_a_track_link_is_not_rejected() -> None:
    raw_url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"

    assert handlers.reject_url(raw_url, classify_url(raw_url)) is None
