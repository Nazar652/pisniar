"""`/edit` and the archived sentence of `/retry`: the editor button goes only to a private
chat with the Mini App configured, and an archived post gets a sentence of its own.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from pisniar.bot import handlers
from pisniar.db.models import Post
from pisniar.db.states import PostStatus

URL = "https://pisniar.example.ts.net"
USER_ID = 111
POST_ID = 7


class FakeSession:
    def __init__(self, post: Post | None, lock_error: bool = False) -> None:
        self.post = post
        self.lock_error = lock_error
        self.options: list[dict[str, Any]] = []
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
        self.options.append(options)
        if self.lock_error:
            raise DBAPIError("SELECT", {}, Exception("lock"))
        return self.post

    async def commit(self) -> None:
        return None


def make_message(text: str, chat_id: int = USER_ID) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.from_user.id = USER_ID
    message.chat.id = chat_id
    message.answer = AsyncMock()
    return message


def make_post(status: PostStatus) -> Post:
    post = Post(id=POST_ID, track_id=1, created_by_user_id=USER_ID)
    post.status = status
    return post


def factory_for(post: Post | None) -> MagicMock:
    session = FakeSession(post)
    factory = MagicMock(return_value=session)
    factory.session = session
    return factory


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers.settings, "webapp_public_url", URL)


def button_url(message: MagicMock) -> str:
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    return keyboard.inline_keyboard[0][0].web_app.url


@pytest.mark.usefixtures("configured")
async def test_edit_without_an_id_offers_the_list_of_posts() -> None:
    message = make_message("/edit")

    await handlers.handle_edit(message, factory_for(None))

    assert button_url(message) == f"{URL}/"
    assert message.answer.await_args.args[0] == handlers.EDIT_LIST_ANSWER


@pytest.mark.usefixtures("configured")
async def test_edit_with_an_id_offers_that_posts_editor() -> None:
    message = make_message(f"/edit {POST_ID}")
    factory = factory_for(make_post(PostStatus.RESOLVED))

    await handlers.handle_edit(message, factory)

    assert button_url(message) == f"{URL}/?post_id={POST_ID}"
    assert factory.session.options == [{}]


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("status", [PostStatus.FAILED, PostStatus.PUBLISHED, PostStatus.RENDERED])
async def test_edit_opens_the_editor_in_every_state_but_archived(status: PostStatus) -> None:
    message = make_message(f"/edit {POST_ID}")

    await handlers.handle_edit(message, factory_for(make_post(status)))

    assert button_url(message) == f"{URL}/?post_id={POST_ID}"


@pytest.mark.usefixtures("configured")
async def test_edit_on_an_archived_post_answers_with_its_own_sentence() -> None:
    message = make_message(f"/edit {POST_ID}")

    await handlers.handle_edit(message, factory_for(make_post(PostStatus.ARCHIVED)))

    assert message.answer.await_args.args[0] == f"Пост {POST_ID} архівовано — дій із ним немає."
    assert "reply_markup" not in message.answer.await_args.kwargs
    assert "archived" not in message.answer.await_args.args[0]


@pytest.mark.usefixtures("configured")
async def test_edit_on_a_missing_post_says_so() -> None:
    message = make_message(f"/edit {POST_ID}")

    await handlers.handle_edit(message, factory_for(None))

    assert message.answer.await_args.args[0] == f"Пост {POST_ID} не знайдено."


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("text", ["/edit abc", "/edit -3", "/edit 7 8", "/edit 1.5"])
async def test_a_malformed_edit_argument_gets_the_format_hint(text: str) -> None:
    message = make_message(text)
    factory = factory_for(make_post(PostStatus.RESOLVED))

    await handlers.handle_edit(message, factory)

    assert message.answer.await_args.args[0] == handlers.EDIT_FORMAT_ANSWER
    factory.assert_not_called()


async def test_edit_without_the_address_says_the_editor_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers.settings, "webapp_public_url", None)
    message = make_message("/edit")

    await handlers.handle_edit(message, factory_for(None))

    assert message.answer.await_args.args[0] == handlers.EDITOR_NOT_CONFIGURED_ANSWER
    assert "reply_markup" not in message.answer.await_args.kwargs


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("text", ["/edit", f"/edit {POST_ID}"])
async def test_edit_in_a_group_sends_no_web_app_button(text: str) -> None:
    message = make_message(text, chat_id=-1001234567890)
    factory = factory_for(make_post(PostStatus.RESOLVED))

    await handlers.handle_edit(message, factory)

    assert message.answer.await_args.args[0] == handlers.EDITOR_PRIVATE_ONLY_ANSWER
    assert "reply_markup" not in message.answer.await_args.kwargs
    factory.assert_not_called()


async def test_edit_ignores_a_message_without_a_sender() -> None:
    message = make_message("/edit")
    message.from_user = None

    await handlers.handle_edit(message, factory_for(None))

    message.answer.assert_not_awaited()


async def test_retry_on_an_archived_post_answers_with_the_archived_sentence() -> None:
    message = make_message(f"/retry {POST_ID}")
    factory = factory_for(make_post(PostStatus.ARCHIVED))
    celery_client = MagicMock()

    await handlers.handle_retry(message, factory, celery_client)

    assert message.answer.await_args.args[0] == f"Пост {POST_ID} архівовано — дій із ним немає."
    assert "«archived»" not in message.answer.await_args.args[0]
    celery_client.send_task.assert_not_called()


async def test_retry_on_a_post_in_another_dead_end_still_names_the_state() -> None:
    message = make_message(f"/retry {POST_ID}")
    factory = factory_for(make_post(PostStatus.PUBLISHED))

    await handlers.handle_retry(message, factory, MagicMock())

    assert "«published»" in message.answer.await_args.args[0]
