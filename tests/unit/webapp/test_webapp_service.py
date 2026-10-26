"""Turning stored rows into the Mini App's responses: the editable fields with their default
hashtags, the previewed caption, the optimistic-check stamps, and the page of posts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.post.caption_input import build_caption_from_rows
from pisniar.post.render import render_html
from pisniar.webapp import service
from pisniar.webapp.schemas import FragmentAvailability
from pisniar.webapp.service import (
    album_url_of,
    build_post_detail,
    fetch_post_detail,
    fetch_post_page,
    names_of,
    rebuild_caption,
    stored_platform_links,
)

ALBUM_ID = "4yYXqedvSKuJlgI2IWsB0j"
CREATED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
POST_UPDATED = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TRACK_UPDATED = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

STORED_ARTISTS: list[object] = [
    {"name": "Океан Ельзи", "hashtag": None, "spotify_id": "a1"},
    {"name": "Друга Ріка", "hashtag": "DrugaRika", "spotify_id": None},
    "junk",
]
STORED_LINKS: dict[str, object] = {
    "spotify": {
        "url": "https://open.spotify.com/track/x",
        "source": "spotify",
        "match": "exact_isrc",
    },
    "youtube": {
        "url": "https://www.youtube.com/watch?v=n4bPzdIs_nc",
        "source": "user_input",
        "match": "manual",
    },
    "broken": "not a dict",
}


def make_track(**overrides: object) -> Track:
    track = Track(source_url="x", source_url_normalized="x")
    track.id = 3
    track.title = "Тема"
    track.artists = STORED_ARTISTS
    track.year = 2019
    track.release_type = "album"
    track.release_title = "Альбом"
    track.album_id_spotify = ALBUM_ID
    track.languages = ["українська", "англійська"]
    track.platform_links = STORED_LINKS
    track.manual_fields = ["year", 7, "title"]
    track.removed_platforms = ["tidal"]
    track.updated_at = TRACK_UPDATED
    for name, value in overrides.items():
        setattr(track, name, value)
    return track


def make_post(status: PostStatus = PostStatus.RESOLVED, **overrides: object) -> Post:
    post = Post(id=7, track_id=3, created_by_user_id=1)
    post.status = status
    post.failed_from_status = None
    post.genres = [{"name": "Рок", "hashtag": None, "source": "deezer"}]
    post.caption = None
    post.created_at = CREATED
    post.published_at = None
    post.updated_at = POST_UPDATED
    for name, value in overrides.items():
        setattr(post, name, value)
    return post


def test_the_detail_carries_every_editable_field() -> None:
    detail = build_post_detail(make_post(), make_track(), [9, 8])

    assert detail.id == 7
    assert detail.status_label == "Чернетка готова"
    assert (detail.editable, detail.not_editable_reason) == (True, None)
    assert detail.title == "Тема"
    assert (detail.year, detail.release_type, detail.release_title) == (2019, "album", "Альбом")
    assert detail.languages == ["українська", "англійська"]
    assert detail.manual_fields == ["year", "title"]
    assert detail.removed_platforms == ["tidal"]
    assert detail.other_post_ids == [9, 8]


def test_the_detail_marks_a_stored_link_the_caption_never_shows() -> None:
    tidal = {"url": "https://listen.tidal.com/track/1", "source": "songlink", "match": "aggregator"}

    detail = build_post_detail(make_post(), make_track(platform_links={**STORED_LINKS, "tidal": tidal}), [])

    assert detail.platform_links["tidal"].shown_in_caption is False
    assert detail.platform_links["spotify"].shown_in_caption is True


def test_the_detail_says_whether_the_post_has_a_video() -> None:
    plain = build_post_detail(make_post(), make_track(), [])
    previewed = build_post_detail(make_post(PostStatus.RENDERED, preview_message_id=5), make_track(), [])
    published = build_post_detail(make_post(PostStatus.PUBLISHED, channel_message_kind="video"), make_track(), [])
    photo = build_post_detail(make_post(PostStatus.PUBLISHED, channel_message_kind="photo"), make_track(), [])

    assert [plain.has_video, previewed.has_video, published.has_video, photo.has_video] == [
        False,
        True,
        True,
        False,
    ]


def test_artists_show_their_stored_hashtag_and_the_default_one_for_a_placeholder() -> None:
    detail = build_post_detail(make_post(), make_track(), [])

    assert [(a.name, a.hashtag, a.default_hashtag, a.spotify_id) for a in detail.artists] == [
        ("Океан Ельзи", None, "ОкеанЕльзи", "a1"),
        ("Друга Ріка", "DrugaRika", "ДругаРіка", None),
    ]


def test_genres_come_from_the_post_with_their_default_hashtags() -> None:
    detail = build_post_detail(make_post(), make_track(), [])

    assert [(g.name, g.hashtag, g.default_hashtag, g.source) for g in detail.genres] == [("Рок", None, "Рок", "deezer")]


def test_platform_links_keep_their_source_and_match_and_skip_an_unreadable_entry() -> None:
    detail = build_post_detail(make_post(), make_track(), [])

    assert set(detail.platform_links) == {"spotify", "youtube"}
    assert detail.platform_links["youtube"].source == "user_input"
    assert detail.platform_links["youtube"].match == "manual"


def test_the_optimistic_stamps_are_the_rows_updated_at() -> None:
    detail = build_post_detail(make_post(), make_track(), [])

    assert detail.expected_post_updated_at == POST_UPDATED
    assert detail.expected_track_updated_at == TRACK_UPDATED


def test_a_post_in_a_read_only_state_says_why_without_naming_the_state() -> None:
    detail = build_post_detail(make_post(PostStatus.ARCHIVED), make_track(), [])

    assert detail.editable is False
    assert detail.not_editable_reason == "Пост архівовано — дій із ним немає."
    assert detail.status_label == "Архівовано"


def test_the_detail_judges_the_fragment_from_the_status_alone() -> None:
    rendered = build_post_detail(
        make_post(PostStatus.RENDERED, fragment_start_ms=10_000, fragment_end_ms=55_000),
        make_track(),
        [],
    )
    published = build_post_detail(make_post(PostStatus.PUBLISHED), make_track(), [])
    resolved = build_post_detail(make_post(PostStatus.RESOLVED), make_track(), [])
    downloading = build_post_detail(make_post(PostStatus.DOWNLOADING), make_track(), [])
    ready = build_post_detail(make_post(PostStatus.EDITING), make_track(), [])

    assert rendered.fragment == FragmentAvailability(
        start_ms=10_000,
        end_ms=55_000,
        choosable=True,
        preparable=False,
        preparing=False,
        cancellable=False,
        reason=None,
    )
    assert ready.fragment.cancellable is True
    assert (resolved.fragment.cancellable, downloading.fragment.cancellable) == (False, False)
    assert published.fragment.choosable is False
    assert published.fragment.reason
    assert published.fragment.start_ms is None
    assert (resolved.fragment.preparable, resolved.fragment.choosable) == (True, False)
    assert (downloading.fragment.preparing, downloading.fragment.choosable) == (True, False)
    assert ready.fragment.choosable is True
    assert ready.status_label == "Аудіо готове — оберіть фрагмент"


def test_the_detail_carries_the_cover_publication_verdict_with_the_preview_in_mind() -> None:
    draft = build_post_detail(make_post(PostStatus.RESOLVED), make_track(), [])
    previewed = build_post_detail(make_post(PostStatus.RESOLVED, preview_message_id=99), make_track(), [])
    ready = build_post_detail(make_post(PostStatus.EDITING), make_track(), [])

    assert (draft.cover_publication.allowed, draft.cover_publication.reason) == (True, None)
    assert previewed.cover_publication.allowed is False
    assert ready.cover_publication.allowed is False
    assert ready.cover_publication.reason


def test_a_valid_album_id_gives_the_album_link_and_a_bad_one_none() -> None:
    assert album_url_of(ALBUM_ID) == f"https://open.spotify.com/album/{ALBUM_ID}"
    assert album_url_of("bad id") is None
    assert album_url_of(None) is None


def test_names_of_keeps_only_non_empty_strings() -> None:
    assert names_of(["a", "", 3, None, "b"]) == ["a", "b"]


def test_stored_platform_links_skip_what_cannot_be_read() -> None:
    links = stored_platform_links({"a": {"url": "https://x/1"}, "b": 5, "c": {"source": "x"}})

    assert set(links) == {"a"}
    assert links["a"].match == "confirmed"


def test_the_previewed_caption_is_what_the_rows_build() -> None:
    post = make_post()
    track = make_track()

    view, html = rebuild_caption(post, track)

    expected = build_caption_from_rows(
        title="Тема",
        artists=track.artists,
        year=2019,
        release_type="album",
        release_title="Альбом",
        album_id_spotify=ALBUM_ID,
        languages=track.languages,
        genres=post.genres,
        platform_links=track.platform_links,
    )
    assert view is not None
    assert html == render_html(expected.document)
    assert view.limit == 1024
    assert view.visible_length > 0
    assert [segment.kind for segment in view.segments][:1] == ["text"]
    assert any(segment.kind == "link" and segment.href for segment in view.segments)
    assert any(segment.kind == "line_break" for segment in view.segments)


def test_there_is_no_caption_to_preview_before_the_track_has_a_title() -> None:
    assert rebuild_caption(make_post(), make_track(title=None)) == (None, None)


def test_the_stored_caption_is_reported_as_matching_only_when_it_is_the_rebuilt_one() -> None:
    track = make_track()
    _, html = rebuild_caption(make_post(), track)

    matching = build_post_detail(make_post(caption=html), track, [])
    stale = build_post_detail(make_post(caption="<b>old</b>"), track, [])

    assert matching.stored_caption_matches is True
    assert stale.stored_caption_matches is False
    assert stale.stored_caption_html == "<b>old</b>"


def test_degradation_is_reported_for_an_overflowing_caption() -> None:
    post = make_post(genres=[{"name": f"жанр{index}", "hashtag": None, "source": "d"} for index in range(200)])

    detail = build_post_detail(post, make_track(), [])

    assert detail.caption is not None
    assert detail.caption.dropped_genres
    assert detail.caption.over_limit is False


def make_row(
    post_id: int,
    status: PostStatus,
    **overrides: object,
) -> SimpleNamespace:
    fields: dict[str, object] = {
        "id": post_id,
        "status": status,
        "failed_from_status": None,
        "created_at": CREATED,
        "published_at": None,
        "preview_message_id": None,
        "channel_message_kind": None,
        "title": f"Тема {post_id}",
        "artists": [{"name": "Артист"}],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def session_returning(rows: list[SimpleNamespace]) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return session


async def test_a_page_with_one_row_more_than_asked_names_the_cursor_for_the_next() -> None:
    rows = [make_row(9, PostStatus.RESOLVED), make_row(8, PostStatus.RESOLVED)]
    session = session_returning(rows)

    page = await fetch_post_page(session, None, 1)

    assert [item.id for item in page.items] == [9]
    assert page.next_before_id == 9
    session.execute.assert_awaited_once()


async def test_the_last_page_has_no_cursor() -> None:
    page = await fetch_post_page(session_returning([make_row(9, PostStatus.RESOLVED)]), None, 5)

    assert page.next_before_id is None
    assert len(page.items) == 1


async def test_an_empty_page_is_empty() -> None:
    page = await fetch_post_page(session_returning([]), 100, 20)

    assert page.items == []
    assert page.next_before_id is None


async def test_the_statement_asks_for_one_extra_row_newest_first_without_hidden_states() -> None:
    session = session_returning([])

    await fetch_post_page(session, 50, 20)

    statement = str(session.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY post.id DESC" in statement
    assert "LIMIT 21" in statement
    assert "post.id < 50" in statement
    assert "JOIN track" in statement
    assert "'archived'" in statement
    assert "'draft'" in statement


async def test_a_list_item_carries_the_verdict_and_whether_a_video_exists() -> None:
    rows = [
        make_row(5, PostStatus.RENDERED, preview_message_id=77),
        make_row(4, PostStatus.PUBLISHED, channel_message_kind="video", published_at=CREATED),
        make_row(3, PostStatus.PUBLISHED, channel_message_kind="photo"),
        make_row(2, PostStatus.RESOLVING),
        make_row(1, PostStatus.FAILED, failed_from_status=PostStatus.RESOLVING),
    ]

    page = await fetch_post_page(session_returning(rows), None, 10)

    assert [item.has_video for item in page.items] == [True, True, False, False, False]
    assert [item.editable for item in page.items] == [True, True, True, False, False]
    assert page.items[3].not_editable_reason
    assert page.items[0].artists == ["Артист"]
    assert page.items[1].published_at == CREATED


async def test_a_missing_post_has_no_detail() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=None)))

    assert await fetch_post_detail(session, 7) is None
    assert session.execute.await_count == 1


async def test_the_detail_reads_the_post_and_then_the_other_posts_of_its_track() -> None:
    row = MagicMock()
    row.tuple.return_value = (make_post(), make_track())
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=row)))
    session.scalars = AsyncMock(return_value=[9, 8])

    detail = await fetch_post_detail(session, 7)

    assert detail is not None
    assert detail.other_post_ids == [9, 8]
    other_statement = str(session.scalars.await_args_list[0].args[0].compile())
    assert "post.track_id" in other_statement
    assert "LIMIT" in other_statement


def test_the_constants_bound_the_page() -> None:
    assert (service.DEFAULT_PAGE_SIZE, service.MAX_PAGE_SIZE) == (20, 50)
