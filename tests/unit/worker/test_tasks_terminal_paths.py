"""The task bodies' terminal paths with the database and Telegram faked: the publish transaction, both failure
reporters, the cleanup of a non-track, the render preconditions and the missing-text notice."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.methods import SendPhoto
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import IntegrityError

from pisniar.db.models import AudioAsset, Post, Track
from pisniar.db.states import PostStatus
from pisniar.worker import tasks as worker_tasks
from pisniar.worker.lyrics import NO_TEXT_NOTICES, LyricsQuery
from pisniar.worker.tasks import PreviewReference, RenderOutcome

POST_ID = 7
ALERT_CHAT_ID = worker_tasks.settings.alert_chat_id
OTHER_CHAT_ID = ALERT_CHAT_ID + 1
CHANNEL_ID = -1001234567890
# a user id: only a positive chat id is a private chat, whatever the configured alert chat is
PRIVATE_DRAFT_CHAT_ID = 987654321


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.session.close = AsyncMock()
    send_alert = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_tasks, "build_bot", MagicMock(return_value=bot))
    monkeypatch.setattr(worker_tasks, "send_alert", send_alert)
    return SimpleNamespace(bot=bot, send_alert=send_alert)


def alert_targets(telegram: SimpleNamespace) -> list[int]:
    return [call.args[1] for call in telegram.send_alert.await_args_list]


def make_session() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.delete = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock()
    return session


async def test_confirmation_clears_both_keyboards_and_answers_in_the_draft_chat(
    telegram: SimpleNamespace,
) -> None:
    await worker_tasks.confirm_publication(
        telegram.bot, POST_ID, OTHER_CHAT_ID, 11, (OTHER_CHAT_ID, 12), "https://t.me/x/1"
    )

    cleared = [call.kwargs["message_id"] for call in telegram.bot.edit_message_reply_markup.await_args_list]
    assert cleared == [11, 12]
    assert all(call.kwargs["reply_markup"] is None for call in telegram.bot.edit_message_reply_markup.await_args_list)
    assert alert_targets(telegram) == [OTHER_CHAT_ID]
    assert "https://t.me/x/1" in telegram.send_alert.await_args.args[2]


async def test_confirmation_skips_a_missing_reference_and_falls_back_to_the_alert_chat(
    telegram: SimpleNamespace,
) -> None:
    await worker_tasks.confirm_publication(telegram.bot, POST_ID, None, None, (None, None), "https://t.me/x/1")

    telegram.bot.edit_message_reply_markup.assert_not_awaited()
    assert alert_targets(telegram) == [ALERT_CHAT_ID]


async def test_a_keyboard_that_cannot_be_removed_does_not_stop_the_confirmation(
    telegram: SimpleNamespace,
) -> None:
    telegram.bot.edit_message_reply_markup.side_effect = TelegramAPIError(
        method=SendPhoto(chat_id=1, photo="x"), message="message is not modified"
    )

    await worker_tasks.confirm_publication(
        telegram.bot, POST_ID, OTHER_CHAT_ID, 11, (OTHER_CHAT_ID, 12), "https://t.me/x/1"
    )

    assert telegram.bot.edit_message_reply_markup.await_count == 2
    assert alert_targets(telegram) == [OTHER_CHAT_ID]


async def test_a_failed_publish_writes_failed_and_alerts_the_admin_and_the_draft_chat(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_status = AsyncMock()
    monkeypatch.setattr(worker_tasks, "write_failed_status", write_status)
    session = make_session()

    await worker_tasks.fail_publish(session, POST_ID, OTHER_CHAT_ID, "Telegram відхилив пост")

    write_status.assert_awaited_once_with(session, POST_ID, "Telegram відхилив пост")
    assert alert_targets(telegram) == [ALERT_CHAT_ID, OTHER_CHAT_ID]
    text = telegram.send_alert.await_args_list[0].args[2]
    assert f"/retry {POST_ID}" in text
    assert "зафіксувати не вдалося" not in text
    telegram.bot.session.close.assert_awaited_once()


async def test_a_failed_publish_alerts_once_when_the_draft_chat_is_the_alert_chat(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks, "write_failed_status", AsyncMock())

    await worker_tasks.fail_publish(make_session(), POST_ID, ALERT_CHAT_ID, "boom")

    assert alert_targets(telegram) == [ALERT_CHAT_ID]


async def test_a_failed_publish_retries_the_status_write_after_a_rollback(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_status = AsyncMock(side_effect=[RuntimeError("connection reset"), None])
    monkeypatch.setattr(worker_tasks, "write_failed_status", write_status)
    session = make_session()

    await worker_tasks.fail_publish(session, POST_ID, None, "boom")

    assert write_status.await_count == 2
    session.rollback.assert_awaited_once()
    assert "зафіксувати не вдалося" not in telegram.send_alert.await_args.args[2]


async def test_a_failed_publish_still_alerts_when_the_database_stays_down(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_status = AsyncMock(side_effect=RuntimeError("database is down"))
    monkeypatch.setattr(worker_tasks, "write_failed_status", write_status)

    await worker_tasks.fail_publish(make_session(), POST_ID, None, "boom")

    assert write_status.await_count == 2
    text = telegram.send_alert.await_args.args[2]
    assert "зафіксувати не вдалося" in text
    assert "«publishing»" in text


async def test_a_failed_publish_scrubs_a_secret_from_the_alert(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks, "write_failed_status", AsyncMock())
    bot_token = worker_tasks.settings.bot_token.get_secret_value()

    await worker_tasks.fail_publish(make_session(), POST_ID, None, f"url with {bot_token}")

    assert bot_token not in telegram.send_alert.await_args.args[2]


async def test_a_failed_resolve_writes_failed_and_alerts_with_the_retry_hint(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_status = AsyncMock()
    monkeypatch.setattr(worker_tasks, "write_failed_status", write_status)
    session = make_session()

    await worker_tasks.fail_post(session, POST_ID, "резолв не вдався")

    write_status.assert_awaited_once_with(session, POST_ID, "резолв не вдався")
    assert alert_targets(telegram) == [ALERT_CHAT_ID]
    text = telegram.send_alert.await_args.args[2]
    assert f"/retry {POST_ID}" in text
    assert "резолв не вдався" in text
    telegram.bot.session.close.assert_awaited_once()


async def test_a_failed_resolve_alerts_even_when_postgres_is_gone(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks, "write_failed_status", AsyncMock(side_effect=RuntimeError("down")))
    session = make_session()

    await worker_tasks.fail_post(session, POST_ID, "boom")

    session.rollback.assert_awaited_once()
    text = telegram.send_alert.await_args.args[2]
    assert "«resolving»" in text


async def test_a_failed_resolve_scrubs_a_secret_before_it_is_stored_or_sent(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_status = AsyncMock()
    monkeypatch.setattr(worker_tasks, "write_failed_status", write_status)
    bot_token = worker_tasks.settings.bot_token.get_secret_value()

    await worker_tasks.fail_post(make_session(), POST_ID, f"error {bot_token}")

    assert bot_token not in write_status.await_args_list[0].args[2]
    assert bot_token not in telegram.send_alert.await_args.args[2]


def make_publishing_post(**overrides: Any) -> Post:
    post = Post(id=POST_ID, track_id=3, created_by_user_id=1, caption="<b>x</b>")
    post.status = PostStatus.PUBLISHING
    post.draft_chat_id = OTHER_CHAT_ID
    post.draft_message_id = 11
    for name, value in overrides.items():
        setattr(post, name, value)
    return post


@pytest.fixture
def publication(telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    chat = MagicMock()
    chat.id = CHANNEL_ID
    chat.username = "test_channel"

    async def write_reference(
        bot: object,
        post: Post,
        chat_id: int,
        cover_url: str | None,
        video: object | None,
    ) -> None:
        post.channel_chat_id = chat_id
        post.channel_message_id = 500
        post.channel_message_kind = "photo"

    pieces = SimpleNamespace(
        preflight=AsyncMock(return_value=chat),
        video=AsyncMock(return_value=None),
        publish=AsyncMock(side_effect=write_reference),
        confirm=AsyncMock(),
        fail=AsyncMock(),
        remove_artifacts=MagicMock(),
        telegram=telegram,
    )
    monkeypatch.setattr(worker_tasks, "preflight_channel", pieces.preflight)
    monkeypatch.setattr(worker_tasks, "video_for_publication", pieces.video)
    monkeypatch.setattr(worker_tasks, "publish_to_channel", pieces.publish)
    monkeypatch.setattr(worker_tasks, "confirm_publication", pieces.confirm)
    monkeypatch.setattr(worker_tasks, "fail_publish", pieces.fail)
    monkeypatch.setattr(worker_tasks.media_storage, "remove_post_artifacts", pieces.remove_artifacts)
    return pieces


async def test_a_successful_publication_commits_published_and_confirms(
    publication: SimpleNamespace,
) -> None:
    session = make_session()
    post = make_publishing_post()
    track = Track(source_url="x", source_url_normalized="x", cover_url="https://cdn/c.jpg")

    await worker_tasks.publish_and_confirm(session, post, track, 222)

    assert post.status is PostStatus.PUBLISHED
    assert post.published_by_user_id == 222
    assert post.published_at is not None
    session.commit.assert_awaited_once()
    publication.publish.assert_awaited_once()
    assert publication.publish.await_args.args[3] == "https://cdn/c.jpg"
    confirm_arguments = publication.confirm.await_args.args
    assert confirm_arguments[1:5] == (POST_ID, OTHER_CHAT_ID, 11, (None, None))
    assert confirm_arguments[5] == "https://t.me/test_channel/500"
    publication.fail.assert_not_awaited()
    publication.remove_artifacts.assert_not_called()
    publication.telegram.bot.session.close.assert_awaited_once()


async def test_the_confirmation_comes_only_after_the_commit(
    publication: SimpleNamespace,
) -> None:
    order: list[str] = []
    session = make_session()
    session.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    publication.confirm.side_effect = lambda *args: order.append("confirm")

    await worker_tasks.publish_and_confirm(
        session, make_publishing_post(), Track(source_url="x", source_url_normalized="x"), 222
    )

    assert order == ["commit", "confirm"]


async def test_a_published_video_removes_the_post_artifacts_after_the_commit(
    publication: SimpleNamespace,
) -> None:
    publication.video.return_value = object()

    await worker_tasks.publish_and_confirm(
        make_session(),
        make_publishing_post(),
        Track(source_url="x", source_url_normalized="x"),
        222,
    )

    publication.remove_artifacts.assert_called_once_with(worker_tasks.settings.media_root, POST_ID)


async def test_a_send_failure_rolls_back_and_fails_the_post_with_the_telegram_reason(
    publication: SimpleNamespace,
) -> None:
    publication.publish.side_effect = TelegramForbiddenError(
        method=SendPhoto(chat_id=1, photo="x"), message="Forbidden: bot was kicked"
    )
    session = make_session()
    post = make_publishing_post()

    await worker_tasks.publish_and_confirm(session, post, Track(source_url="x", source_url_normalized="x"), 222)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    publication.confirm.assert_not_awaited()
    fail_arguments = publication.fail.await_args.args
    assert fail_arguments[1:3] == (POST_ID, OTHER_CHAT_ID)
    assert "403" in fail_arguments[3]
    publication.telegram.bot.session.close.assert_awaited_once()


async def test_a_failing_rollback_does_not_stop_the_failure_report(
    publication: SimpleNamespace,
) -> None:
    publication.preflight.side_effect = RuntimeError("channel unreachable")
    session = make_session()
    session.rollback = AsyncMock(side_effect=RuntimeError("connection lost"))

    await worker_tasks.publish_and_confirm(
        session, make_publishing_post(), Track(source_url="x", source_url_normalized="x"), 222
    )

    publication.fail.assert_awaited_once()
    publication.publish.assert_not_awaited()


async def test_an_unavailable_video_fails_the_post_instead_of_sending_a_photo(
    publication: SimpleNamespace,
) -> None:
    publication.video.side_effect = worker_tasks.RenderedVideoUnavailableError("файл зник")

    await worker_tasks.publish_and_confirm(
        make_session(),
        make_publishing_post(),
        Track(source_url="x", source_url_normalized="x"),
        222,
    )

    publication.publish.assert_not_awaited()
    assert publication.fail.await_args.args[3] == "файл зник"


async def test_the_preview_reference_travels_to_the_confirmation(
    publication: SimpleNamespace,
) -> None:
    post = make_publishing_post(preview_chat_id=OTHER_CHAT_ID, preview_message_id=12)

    await worker_tasks.publish_and_confirm(make_session(), post, Track(source_url="x", source_url_normalized="x"), 222)

    assert publication.confirm.await_args.args[4] == (OTHER_CHAT_ID, 12)


@pytest.fixture
def cleanup_storage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    pieces = SimpleNamespace(post=MagicMock(), track=MagicMock())
    monkeypatch.setattr(worker_tasks.media_storage, "remove_post_artifacts", pieces.post)
    monkeypatch.setattr(worker_tasks.media_storage, "remove_track_artifacts", pieces.track)
    return pieces


def make_orphan_track() -> Track:
    track = Track(source_url="https://x", source_url_normalized="https://x")
    track.id = 3
    return track


async def test_cleanup_reports_a_post_that_could_not_be_deleted(
    cleanup_storage: SimpleNamespace,
) -> None:
    session = make_session()
    session.delete = AsyncMock(side_effect=RuntimeError("database is down"))
    post = make_publishing_post()

    deleted, note = await worker_tasks.delete_post_and_orphan_track(session, post, POST_ID, make_orphan_track())

    assert deleted is False
    assert "«resolving»" in note
    session.rollback.assert_awaited_once()
    cleanup_storage.post.assert_not_called()


async def test_cleanup_deletes_the_orphaned_track_and_its_artifacts(
    cleanup_storage: SimpleNamespace,
) -> None:
    session = make_session()
    session.scalar = AsyncMock(return_value=0)
    track = make_orphan_track()

    deleted, note = await worker_tasks.delete_post_and_orphan_track(session, make_publishing_post(), POST_ID, track)

    assert (deleted, note) == (True, "")
    assert [call.args[0] for call in session.delete.await_args_list][-1] is track
    assert session.commit.await_count == 2
    cleanup_storage.post.assert_called_once_with(worker_tasks.settings.media_root, POST_ID)
    cleanup_storage.track.assert_called_once_with(worker_tasks.settings.media_root, 3)


async def test_cleanup_keeps_a_track_that_another_post_still_uses(
    cleanup_storage: SimpleNamespace,
) -> None:
    session = make_session()
    session.scalar = AsyncMock(return_value=2)

    deleted, note = await worker_tasks.delete_post_and_orphan_track(
        session, make_publishing_post(), POST_ID, make_orphan_track()
    )

    assert (deleted, note) == (True, "")
    assert session.delete.await_count == 1
    assert session.commit.await_count == 2
    cleanup_storage.track.assert_not_called()


async def test_cleanup_treats_a_restricted_track_delete_as_an_expected_outcome(
    cleanup_storage: SimpleNamespace,
) -> None:
    session = make_session()
    session.scalar = AsyncMock(return_value=0)
    session.commit = AsyncMock(side_effect=[None, IntegrityError("DELETE", {}, Exception("fk"))])

    deleted, note = await worker_tasks.delete_post_and_orphan_track(
        session, make_publishing_post(), POST_ID, make_orphan_track()
    )

    assert (deleted, note) == (True, "")
    session.rollback.assert_awaited_once()
    cleanup_storage.track.assert_not_called()


async def test_cleanup_reports_a_track_step_that_failed_after_the_post_was_deleted(
    cleanup_storage: SimpleNamespace,
) -> None:
    session = make_session()
    session.execute = AsyncMock(side_effect=RuntimeError("lock timeout"))

    deleted, note = await worker_tasks.delete_post_and_orphan_track(
        session, make_publishing_post(), POST_ID, make_orphan_track()
    )

    assert deleted is True
    assert "Трек #3 прибрати не вдалося" in note
    cleanup_storage.post.assert_called_once()


YOUTUBE_MUSIC_LINKS: dict[str, object] = {
    "youtubeMusic": {
        "url": "https://music.youtube.com/watch?v=n4bPzdIs_nc",
        "source": "ytmusic",
        "match": "confirmed",
    }
}


def make_render_track(**overrides: Any) -> Track:
    track = Track(source_url="x", source_url_normalized="x", title="Покаяння", cover_url="c")
    track.id = 3
    track.platform_links = YOUTUBE_MUSIC_LINKS
    track.artists = [{"name": "Burned Time Machine"}, "junk", {"name": "  "}]
    track.duration_ms = 212_000
    for name, value in overrides.items():
        setattr(track, name, value)
    return track


def make_render_post(start_ms: int | None = 0, end_ms: int | None = 45_000) -> Post:
    post = Post(id=POST_ID, track_id=3, created_by_user_id=1)
    post.fragment_start_ms = start_ms
    post.fragment_end_ms = end_ms
    return post


def session_returning_asset(asset: AudioAsset | None) -> MagicMock:
    session = make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = asset
    session.execute = AsyncMock(return_value=result)
    return session


async def test_render_preconditions_without_a_youtube_music_link_refuse_before_any_query() -> None:
    session = session_returning_asset(None)

    preconditions = await worker_tasks.read_render_preconditions(
        session, make_render_track(platform_links={}), make_render_post()
    )

    assert preconditions.claim is None
    assert preconditions.error is not None
    session.execute.assert_not_awaited()


async def test_render_preconditions_refuse_a_track_without_downloaded_audio() -> None:
    preconditions = await worker_tasks.read_render_preconditions(
        session_returning_asset(None), make_render_track(), make_render_post()
    )

    assert preconditions.claim is None
    assert preconditions.error is not None
    assert "немає завантаженого аудіо" in preconditions.error


@pytest.mark.parametrize(("start_ms", "end_ms"), [(0, None), (45_000, 10_000)])
async def test_render_preconditions_refuse_unusable_bounds(start_ms: int | None, end_ms: int | None) -> None:
    asset = AudioAsset(path="/data/audio/3/a.opus", duration_ms=212_000)

    preconditions = await worker_tasks.read_render_preconditions(
        session_returning_asset(asset), make_render_track(), make_render_post(start_ms, end_ms)
    )

    assert preconditions.claim is None
    assert preconditions.error is not None


async def test_render_preconditions_refuse_a_post_without_bounds() -> None:
    asset = AudioAsset(path="/data/audio/3/a.opus", duration_ms=212_000)

    preconditions = await worker_tasks.read_render_preconditions(
        session_returning_asset(asset), make_render_track(), make_render_post(None, None)
    )

    assert preconditions.claim is None
    assert preconditions.error is not None
    assert "межі фрагмента" in preconditions.error


async def test_render_preconditions_build_a_claim_of_plain_values() -> None:
    asset = AudioAsset(path="/data/audio/3/n4bPzdIs_nc.opus", duration_ms=212_000)

    preconditions = await worker_tasks.read_render_preconditions(
        session_returning_asset(asset), make_render_track(), make_render_post(5_000, 50_000)
    )

    assert preconditions.error is None
    claim = preconditions.claim
    assert claim is not None
    assert (claim.post_id, claim.track_id, claim.video_id) == (POST_ID, 3, "n4bPzdIs_nc")
    assert claim.audio_path == "/data/audio/3/n4bPzdIs_nc.opus"
    assert (claim.fragment_start_ms, claim.fragment_end_ms) == (5_000, 50_000)
    assert claim.cover_url == "c"
    assert claim.lyrics_query == LyricsQuery(
        track_id=3,
        artist_names=("Burned Time Machine",),
        title="Покаяння",
        duration_ms=212_000,
        video_id="n4bPzdIs_nc",
    )


def make_outcome(text_version: str | None, lyrics_reason: str) -> RenderOutcome:
    return RenderOutcome(
        params_hash="a" * 64,
        path=Path("/data/render/7/x.mp4"),
        size_bytes=1,
        duration_ms=45_000,
        text_version=text_version,
        lyrics_reason=lyrics_reason,
    )


PREVIEW = PreviewReference(chat_id=OTHER_CHAT_ID, message_id=12)


async def test_a_video_without_text_gets_a_notice_answering_its_preview(
    telegram: SimpleNamespace,
) -> None:
    reason = next(iter(NO_TEXT_NOTICES))

    await worker_tasks.notify_about_missing_text(POST_ID, make_outcome(None, reason), PREVIEW)

    call = telegram.send_alert.await_args
    assert call.args[1] == OTHER_CHAT_ID
    assert call.kwargs["reply_to_message_id"] == 12
    assert NO_TEXT_NOTICES[reason] in call.args[2]
    telegram.bot.session.close.assert_awaited_once()


async def test_a_video_with_text_gets_no_notice(telegram: SimpleNamespace) -> None:
    await worker_tasks.notify_about_missing_text(POST_ID, make_outcome("b" * 64, "x"), PREVIEW)

    telegram.send_alert.assert_not_awaited()


async def test_a_lost_notice_is_a_log_line_and_never_raises(
    telegram: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    telegram.send_alert.side_effect = RuntimeError("telegram down")
    caplog.set_level(logging.WARNING)

    await worker_tasks.notify_about_missing_text(POST_ID, make_outcome(None, "x"), PREVIEW)

    telegram.bot.session.close.assert_awaited_once()


async def test_a_timeout_while_sending_the_notice_is_swallowed(
    telegram: SimpleNamespace,
) -> None:
    telegram.send_alert.side_effect = SoftTimeLimitExceeded()

    await worker_tasks.notify_about_missing_text(POST_ID, make_outcome(None, "x"), PREVIEW)

    telegram.bot.session.close.assert_awaited_once()


async def test_the_confirmation_in_a_private_chat_carries_the_edit_button(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "webapp_public_url", "https://pisniar.example.ts.net")

    await worker_tasks.confirm_publication(
        telegram.bot, POST_ID, PRIVATE_DRAFT_CHAT_ID, 11, (None, None), "https://t.me/x/1"
    )

    keyboard = telegram.send_alert.await_args.kwargs["reply_markup"]
    assert keyboard is not None
    web_app = keyboard.inline_keyboard[0][0].web_app
    assert web_app.url == f"https://pisniar.example.ts.net/?post_id={POST_ID}"


async def test_the_confirmation_in_a_group_has_no_button_and_is_still_sent(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "webapp_public_url", "https://pisniar.example.ts.net")

    await worker_tasks.confirm_publication(telegram.bot, POST_ID, -1001234567890, 11, (None, None), "https://t.me/x/1")

    telegram.send_alert.assert_awaited_once()
    assert telegram.send_alert.await_args.kwargs["reply_markup"] is None


async def test_the_confirmation_without_the_address_has_no_button(
    telegram: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_tasks.settings, "webapp_public_url", None)

    await worker_tasks.confirm_publication(telegram.bot, POST_ID, OTHER_CHAT_ID, 11, (None, None), "https://t.me/x/1")

    assert telegram.send_alert.await_args.kwargs["reply_markup"] is None
