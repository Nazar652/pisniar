"""D-69: one message may carry a «platform link + clip» pair. Only the input
parse changes — the clip rides the same `user_input`/`manual` seat a lone clip
has always used, and the resolve learns nothing new.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pisniar.bot import handlers
from pisniar.db.models import Track
from pisniar.resolve.urls import classify_url

SPOTIFY_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
DEEZER_URL = "https://www.deezer.com/track/3135556"
YOUTUBE_MUSIC_URL = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
CLIP_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
OTHER_CLIP_URL = "https://youtu.be/9bZkp7q19f0"


async def capture_rejection(text: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Same shape as `test_handler_link_rejections`: the module logger is
    replaced, because `capture_logs()` loses an already-bound logger (V12)."""
    fake_logger = MagicMock()
    monkeypatch.setattr(handlers, "logger", fake_logger)

    message = AsyncMock()
    message.text = text
    message.from_user = SimpleNamespace(id=111)
    session_factory = MagicMock()
    celery_client = MagicMock()

    await handlers.handle_link(message, session_factory, celery_client)

    session_factory.assert_not_called()
    celery_client.send_task.assert_not_called()
    rejections = [
        info_call
        for info_call in fake_logger.info.call_args_list
        if info_call.args[:1] == ("link_rejected",)
    ]
    assert len(rejections) == 1
    return {"answer": message.answer.await_args.args[0], **rejections[0].kwargs}


def test_two_whitespace_separated_links_split_into_a_pair() -> None:
    assert handlers._split_links(f"{SPOTIFY_URL}  {CLIP_URL}") == [SPOTIFY_URL, CLIP_URL]


def test_a_newline_separates_links_as_well_as_a_space() -> None:
    assert handlers._split_links(f"{CLIP_URL}\n{SPOTIFY_URL}") == [CLIP_URL, SPOTIFY_URL]


def test_prose_around_a_link_stays_one_unrecognized_submission() -> None:
    text = f"дивись {SPOTIFY_URL}"

    assert handlers._split_links(text) == [text]


def test_a_single_link_is_returned_stripped() -> None:
    assert handlers._split_links(f"  {SPOTIFY_URL} ") == [SPOTIFY_URL]


def test_a_pair_resolves_from_the_platform_half() -> None:
    submitted = [SPOTIFY_URL, CLIP_URL]
    classified = [classify_url(url) for url in submitted]

    raw_url, source, clip = handlers._select_track_source(submitted, classified)

    assert raw_url == SPOTIFY_URL
    assert source.platform == "spotify"
    assert clip is not None
    assert clip.platform == "youtube"


def test_the_order_of_the_pair_does_not_matter() -> None:
    submitted = [CLIP_URL, SPOTIFY_URL]
    classified = [classify_url(url) for url in submitted]

    raw_url, source, clip = handlers._select_track_source(submitted, classified)

    assert raw_url == SPOTIFY_URL
    assert source.platform == "spotify"
    assert clip is not None
    assert clip.normalized_url == classify_url(CLIP_URL).normalized_url


def test_youtube_music_counts_as_the_platform_half_not_as_a_clip() -> None:
    """D-64: `youtubeMusic` is the canonical "— Topic" upload, a platform link
    like any other; only a `youtube` link is a clip."""
    submitted = [YOUTUBE_MUSIC_URL, CLIP_URL]
    classified = [classify_url(url) for url in submitted]

    raw_url, source, clip = handlers._select_track_source(submitted, classified)

    assert raw_url == YOUTUBE_MUSIC_URL
    assert source.platform == "youtubeMusic"
    assert clip is not None


def test_a_lone_clip_is_both_the_source_and_the_seeded_clip() -> None:
    classified = [classify_url(CLIP_URL)]

    raw_url, source, clip = handlers._select_track_source([CLIP_URL], classified)

    assert raw_url == CLIP_URL
    assert clip is source


def test_a_lone_platform_link_seeds_no_clip() -> None:
    classified = [classify_url(SPOTIFY_URL)]

    raw_url, source, clip = handlers._select_track_source([SPOTIFY_URL], classified)

    assert raw_url == SPOTIFY_URL
    assert source.platform == "spotify"
    assert clip is None


def test_the_clip_is_seeded_as_user_input_at_the_manual_tier() -> None:
    track = Track(source_url=SPOTIFY_URL, source_url_normalized=SPOTIFY_URL)

    handlers._seed_user_input_clip(track, classify_url(CLIP_URL))

    assert track.platform_links == {
        "youtube": {
            "url": classify_url(CLIP_URL).normalized_url,
            "source": "user_input",
            "match": "manual",
        }
    }


def test_seeding_replaces_an_automatically_found_youtube_link() -> None:
    """The admin's word outranks anything resolved: `manual` is the top tier
    of the four, and this is the only place the admin can reach it today."""
    track = Track(source_url=SPOTIFY_URL, source_url_normalized=SPOTIFY_URL)
    track.platform_links = {
        "youtube": {"url": OTHER_CLIP_URL, "source": "ytmusic", "match": "confirmed"},
        "spotify": {"url": SPOTIFY_URL, "source": "spotify", "match": "exact_isrc"},
    }

    handlers._seed_user_input_clip(track, classify_url(CLIP_URL))

    youtube_link = track.platform_links["youtube"]
    assert isinstance(youtube_link, dict)
    assert youtube_link["source"] == "user_input"
    assert "spotify" in track.platform_links


def test_reseeding_the_same_clip_leaves_the_row_untouched() -> None:
    """A resubmitted clip must not dirty `track.platform_links` — the UPDATE
    would contend with the worker's own lock on that row for nothing."""
    track = Track(source_url=CLIP_URL, source_url_normalized=CLIP_URL)
    handlers._seed_user_input_clip(track, classify_url(CLIP_URL))
    before = track.platform_links

    handlers._seed_user_input_clip(track, classify_url(CLIP_URL))

    assert track.platform_links is before


def test_no_clip_means_no_write_at_all() -> None:
    track = Track(source_url=SPOTIFY_URL, source_url_normalized=SPOTIFY_URL)

    handlers._seed_user_input_clip(track, None)

    assert track.platform_links is None


async def test_two_platform_links_are_refused_with_the_pair_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection = await capture_rejection(f"{SPOTIFY_URL} {DEEZER_URL}", monkeypatch)

    assert rejection["reason"] == "two_platform_links"
    assert rejection["platforms"] == ["spotify", "deezer"]
    assert "кліп" in rejection["answer"]


async def test_two_clips_are_refused_with_the_pair_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection = await capture_rejection(f"{CLIP_URL} {OTHER_CLIP_URL}", monkeypatch)

    assert rejection["reason"] == "two_clips"
    assert "платформу" in rejection["answer"]


async def test_three_links_are_refused_before_anything_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection = await capture_rejection(f"{SPOTIFY_URL} {CLIP_URL} {DEEZER_URL}", monkeypatch)

    assert rejection["reason"] == "too_many_links"
    assert rejection["link_count"] == 3
    assert "3" in rejection["answer"]


async def test_a_bad_half_of_a_pair_is_named_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each link is validated separately, so an album sent alongside a clip is
    answered as an album — not as a malformed pair."""
    album_url = "https://open.spotify.com/album/4yYXqedvSKuJlgI2IWsB0j"

    rejection = await capture_rejection(f"{album_url} {CLIP_URL}", monkeypatch)

    assert rejection["reason"] == "not_track"
    assert rejection["not_track_reason"] == "album"
    assert "альбом" in rejection["answer"]
