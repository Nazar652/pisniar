"""Reading, previewing and saving posts for the Mini App.

Reads are plain SELECTs. Stored rows become the response through the same conversions the worker
uses, so the editor shows what the caption is built from; a save never calls Telegram.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pisniar.db.models import Post, Track
from pisniar.db.states import PostStatus
from pisniar.post.caption import MEDIA_CAPTION_LIMIT
from pisniar.post.caption_input import (
    build_caption_from_rows,
    genres_from_stored,
    languages_from_stored,
)
from pisniar.post.hashtags import default_hashtag
from pisniar.post.render import (
    LineBreakSegment,
    LinkSegment,
    TextSegment,
    render_html,
    visible_length,
)
from pisniar.resolve.json_utils import as_nonempty_str
from pisniar.resolve.matching import is_valid_spotify_id
from pisniar.resolve.merge import artists_from_stored, platform_link_from_stored
from pisniar.webapp.schemas import (
    ArtistDetail,
    CaptionSegmentView,
    CaptionView,
    GenreDetail,
    PlatformLinkDetail,
    PostDetail,
    PostListResponse,
    PostSummary,
)
from pisniar.webapp.status import STATUS_LABELS, editability_of

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_OTHER_POSTS = 50
HIDDEN_STATUSES = (PostStatus.ARCHIVED, PostStatus.DRAFT)


async def fetch_post_page(
    session: AsyncSession, before_id: int | None, limit: int
) -> PostListResponse:
    """newest first, keyed on the id: one query that asks for one row more than the page
    to know whether another page exists."""
    statement = (
        select(
            Post.id,
            Post.status,
            Post.failed_from_status,
            Post.created_at,
            Post.published_at,
            Post.preview_message_id,
            Post.channel_message_kind,
            Track.title,
            Track.artists,
        )
        .join(Track, Track.id == Post.track_id)
        .where(Post.status.not_in(HIDDEN_STATUSES))
        .order_by(Post.id.desc())
        .limit(limit + 1)
    )
    if before_id is not None:
        statement = statement.where(Post.id < before_id)

    rows = (await session.execute(statement)).all()
    page = rows[:limit]

    items = []
    for row in page:
        editability = editability_of(row.status, row.failed_from_status)
        items.append(
            PostSummary(
                id=row.id,
                status_label=STATUS_LABELS[row.status],
                title=row.title,
                artists=[artist.name for artist in artists_from_stored(row.artists)],
                created_at=row.created_at,
                published_at=row.published_at,
                editable=editability.editable,
                not_editable_reason=editability.reason,
                has_video=row.preview_message_id is not None or row.channel_message_kind == "video",
            )
        )

    next_before_id = page[-1].id if len(rows) > limit else None
    return PostListResponse(items=items, next_before_id=next_before_id)


async def fetch_post_detail(session: AsyncSession, post_id: int) -> PostDetail | None:
    row = (
        await session.execute(
            select(Post, Track).join(Track, Track.id == Post.track_id).where(Post.id == post_id)
        )
    ).one_or_none()
    if row is None:
        return None
    post, track = row.tuple()
    return await detail_of(session, post, track)


async def detail_of(session: AsyncSession, post: Post, track: Track) -> PostDetail:
    other_post_ids = list(
        await session.scalars(
            select(Post.id)
            .where(Post.track_id == track.id, Post.id != post.id)
            .order_by(Post.id.desc())
            .limit(MAX_OTHER_POSTS)
        )
    )
    return build_post_detail(post, track, other_post_ids)


def build_post_detail(post: Post, track: Track, other_post_ids: list[int]) -> PostDetail:
    editability = editability_of(post.status, post.failed_from_status)
    caption_view, rebuilt_html = rebuild_caption(post, track)

    return PostDetail(
        id=post.id,
        has_video=post.preview_message_id is not None or post.channel_message_kind == "video",
        status_label=STATUS_LABELS[post.status],
        editable=editability.editable,
        not_editable_reason=editability.reason,
        created_at=post.created_at,
        published_at=post.published_at,
        title=track.title,
        artists=[
            ArtistDetail(
                name=artist.name,
                hashtag=artist.hashtag,
                default_hashtag=default_hashtag(artist.name),
                spotify_id=artist.spotify_id,
            )
            for artist in artists_from_stored(track.artists)
        ],
        languages=languages_from_stored(track.languages),
        genres=[
            GenreDetail(
                name=genre.name,
                hashtag=genre.hashtag,
                default_hashtag=default_hashtag(genre.name),
                source=genre.source,
            )
            for genre in genres_from_stored(post.genres)
        ],
        year=track.year,
        release_type=track.release_type,
        release_title=track.release_title,
        album_id_spotify=track.album_id_spotify,
        album_url=album_url_of(track.album_id_spotify),
        platform_links=stored_platform_links(track.platform_links),
        manual_fields=names_of(track.manual_fields),
        removed_platforms=names_of(track.removed_platforms),
        caption=caption_view,
        stored_caption_html=post.caption,
        stored_caption_matches=rebuilt_html is not None and rebuilt_html == post.caption,
        other_post_ids=other_post_ids,
        expected_post_updated_at=post.updated_at,
        expected_track_updated_at=track.updated_at,
    )


def rebuild_caption(post: Post, track: Track) -> tuple[CaptionView | None, str | None]:
    """the caption the rows would give now, which the editor previews; `None` while the
    track has no title to build one from."""
    if not track.title:
        return None, None

    return caption_view_of(
        title=track.title,
        artists=track.artists,
        year=track.year,
        release_type=track.release_type,
        release_title=track.release_title,
        album_id_spotify=track.album_id_spotify,
        languages=track.languages,
        genres=post.genres,
        platform_links=track.platform_links,
    )


def caption_view_of(
    *,
    title: str,
    artists: Sequence[object],
    year: int | None,
    release_type: str | None,
    release_title: str | None,
    album_id_spotify: str | None,
    languages: Sequence[object],
    genres: Sequence[object],
    platform_links: dict[str, object],
) -> tuple[CaptionView, str]:
    """the view and the exact HTML it renders to; the sha256 in the view is that HTML's."""
    rebuilt = build_caption_from_rows(
        title=title,
        artists=artists,
        year=year,
        release_type=release_type,
        release_title=release_title,
        album_id_spotify=album_id_spotify,
        languages=languages,
        genres=genres,
        platform_links=platform_links,
    )

    segments = []
    for segment in rebuilt.document.segments:
        if isinstance(segment, TextSegment):
            segments.append(CaptionSegmentView(kind="text", text=segment.value, href=None))
        elif isinstance(segment, LinkSegment):
            segments.append(CaptionSegmentView(kind="link", text=segment.text, href=segment.href))
        elif isinstance(segment, LineBreakSegment):
            segments.append(CaptionSegmentView(kind="line_break", text=None, href=None))

    html = render_html(rebuilt.document)
    degradation = rebuilt.degradation
    view = CaptionView(
        segments=segments,
        visible_length=visible_length(rebuilt.document),
        limit=MEDIA_CAPTION_LIMIT,
        dropped_genres=list(degradation.dropped_genres),
        dropped_platforms=list(degradation.dropped_platforms),
        release_title_dropped=degradation.release_title_dropped,
        over_limit=degradation.over_limit,
        warnings=rebuilt.warnings,
        sha256=caption_sha256(html),
    )
    return view, html


def caption_sha256(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def album_url_of(album_id: str | None) -> str | None:
    if album_id is None or not is_valid_spotify_id(album_id):
        return None
    return f"https://open.spotify.com/album/{album_id}"


def stored_platform_links(stored: dict[str, object]) -> dict[str, PlatformLinkDetail]:
    links = {}
    for platform, raw_link in stored.items():
        link = platform_link_from_stored(raw_link)
        if link is not None:
            links[platform] = PlatformLinkDetail(
                url=link.url, source=link.source, match=link.match.value
            )
    return links


def names_of(stored: list[object]) -> list[str]:
    return [name for entry in stored if (name := as_nonempty_str(entry)) is not None]
