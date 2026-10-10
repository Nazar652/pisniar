"""ORM models: `track`, `post`, the `source_fetch` resolver cache, the media
artifacts (`audio_asset` per track, `render` per post) and `lyric` (per track).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Select,
    SmallInteger,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from pisniar.db.base import Base, IdAndTimestampsMixin
from pisniar.db.states import PostStatus

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect

VALID_POST_STATUSES = tuple(status.value for status in PostStatus)
VALID_POST_STATUSES_SQL_LIST = ", ".join(f"'{value}'" for value in VALID_POST_STATUSES)

# language belongs to the recording, not a publication, and no source reports
# it — so it is stored with a default and edited by hand.
DEFAULT_LANGUAGES = ["українська"]
DEFAULT_LANGUAGES_SQL = f"'{json.dumps(DEFAULT_LANGUAGES, ensure_ascii=False)}'::jsonb"

# track columns a human may set so that no later resolve overwrites them.
MANUAL_FIELD_NAMES = (
    "title",
    "artists",
    "year",
    "release_type",
    "release_title",
    "album_id_spotify",
)
MANUAL_FIELD_NAMES_JSON = json.dumps(list(MANUAL_FIELD_NAMES))

# what kind of Telegram message `channel_message_id` points at, which decides how it is edited.
CHANNEL_MESSAGE_KINDS = ("photo", "video", "text")
CHANNEL_MESSAGE_KINDS_SQL_LIST = ", ".join(f"'{value}'" for value in CHANNEL_MESSAGE_KINDS)

# a karaoke line, not a verse: anything longer is a parser that lost the line
# breaks, and the CHECK is where that stops being silent.
MAX_LYRIC_TEXT_LENGTH = 500
# a stored text is ~50 lines and ~1 000 characters; the ceilings leave room for a
# pasted whole song and stop a paste of a book.
MAX_LYRIC_LINES = 300
MAX_LYRICS_TOTAL_CHARACTERS = 20_000
# a saved set's JSON body: 20 000 four-byte characters plus ~40 bytes of keys per line.
LYRICS_MAX_BODY_BYTES = 96 * 1024


class PostStatusType(TypeDecorator[PostStatus]):
    """the column is TEXT, not a native enum. Without this `post.status` loads as a
    plain `str` and `post.status is PostStatus.X` is always False."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: PostStatus | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return PostStatus(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> PostStatus | None:
        if value is None:
            return None
        return PostStatus(value)


class Track(IdAndTimestampsMixin, Base):
    __tablename__ = "track"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(artists) = 'array'", name="artists_is_array"),
        CheckConstraint("jsonb_typeof(platform_links) = 'object'", name="platform_links_is_object"),
        CheckConstraint("jsonb_typeof(languages) = 'array'", name="languages_is_array"),
        CheckConstraint("jsonb_typeof(manual_fields) = 'array'", name="manual_fields_is_array"),
        CheckConstraint(
            f"manual_fields <@ '{MANUAL_FIELD_NAMES_JSON}'::jsonb",
            name="manual_fields_known_names",
        ),
        CheckConstraint(
            "jsonb_typeof(removed_platforms) = 'array'", name="removed_platforms_is_array"
        ),
    )

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url_normalized: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # indexed for the duplicate warning: the same recording submitted through
    # another platform's URL is otherwise invisible.
    isrc: Mapped[str | None] = mapped_column(Text, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    # form: [{"name": str, "hashtag": str | None, "spotify_id": str | None}];
    # `hashtag` stays null until a human overrides it.
    artists: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    year: Mapped[int | None] = mapped_column(SmallInteger)
    release_type: Mapped[str | None] = mapped_column(Text)
    release_title: Mapped[str | None] = mapped_column(Text)
    # needed to rebuild a caption: the rendered one holds it only inside an `href`.
    album_id_spotify: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    # the downloaded audio is checked against it, so a re-resolve must never null
    # it (hence its place in `STORED_SCALAR_FIELDS`).
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    # form: ["українська", ...]; the first is the primary language, the order is the admin's.
    languages: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text(DEFAULT_LANGUAGES_SQL)
    )
    platform_links: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # names from `MANUAL_FIELD_NAMES` a human changed; a re-resolve keeps those values, an
    # empty one included (the value is NULL in its own column, the name is listed here).
    manual_fields: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # platform keys a human removed; a re-resolve must not bring them back.
    removed_platforms: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # set when a human saved the text, an empty set included: no automatic source
    # writes or asks again. NULL means nobody edited it.
    lyrics_edited_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # passive_deletes="all": without it, SQLAlchemy nulls track_id before
    # delete, so ON DELETE RESTRICT never fires.
    posts: Mapped[list[Post]] = relationship(back_populates="track", passive_deletes="all")


class Post(IdAndTimestampsMixin, Base):
    """several posts per track are allowed (no `UNIQUE (track_id)`). `fragment_*_ms`
    are absolute from the start of the *full* track, never fragment-local."""

    __tablename__ = "post"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({VALID_POST_STATUSES_SQL_LIST})",
            name="status_valid",
        ),
        CheckConstraint(
            f"failed_from_status IS NULL OR failed_from_status IN ({VALID_POST_STATUSES_SQL_LIST})",
            name="failed_from_status_valid",
        ),
        # (NULL, NULL) means no fragment yet.
        CheckConstraint(
            "num_nonnulls(fragment_start_ms, fragment_end_ms) <> 1 "
            "AND (fragment_start_ms IS NULL OR fragment_start_ms >= 0) "
            "AND (fragment_end_ms IS NULL OR fragment_end_ms > fragment_start_ms)",
            name="fragment_bounds_valid",
        ),
        CheckConstraint("jsonb_typeof(genres) = 'array'", name="genres_is_array"),
        CheckConstraint(
            "num_nonnulls(channel_chat_id, channel_message_id) <> 1",
            name="channel_ref_complete",
        ),
        CheckConstraint(
            f"channel_message_kind IS NULL OR channel_message_kind IN "
            f"({CHANNEL_MESSAGE_KINDS_SQL_LIST})",
            name="channel_message_kind_valid",
        ),
        CheckConstraint(
            "(channel_message_id IS NULL) = (channel_message_kind IS NULL)",
            name="channel_message_kind_matches_ref",
        ),
        Index(
            "uq_post_channel_message",
            "channel_chat_id",
            "channel_message_id",
            unique=True,
            postgresql_where=text("channel_chat_id IS NOT NULL AND channel_message_id IS NOT NULL"),
        ),
        CheckConstraint(
            "num_nonnulls(draft_chat_id, draft_message_id) <> 1",
            name="draft_ref_complete",
        ),
        Index(
            "uq_post_draft_message",
            "draft_chat_id",
            "draft_message_id",
            unique=True,
            postgresql_where=text("draft_chat_id IS NOT NULL AND draft_message_id IS NOT NULL"),
        ),
        # the video preview is a *separate* message from the draft:
        # `editMessageMedia` cannot turn a text message into a media one.
        CheckConstraint(
            "num_nonnulls(preview_chat_id, preview_message_id) <> 1",
            name="preview_ref_complete",
        ),
        CheckConstraint(
            "num_nonnulls(preview_message_id, preview_params_hash) <> 1",
            name="preview_params_hash_matches_ref",
        ),
        Index(
            "uq_post_preview_message",
            "preview_chat_id",
            "preview_message_id",
            unique=True,
            postgresql_where=text("preview_chat_id IS NOT NULL AND preview_message_id IS NOT NULL"),
        ),
    )

    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("track.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[PostStatus] = mapped_column(
        PostStatusType,
        nullable=False,
        default=PostStatus.DRAFT,
        server_default=PostStatus.DRAFT.value,
        index=True,
    )
    caption: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    fragment_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    fragment_end_ms: Mapped[int | None] = mapped_column(BigInteger)

    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # set in the same statement as `channel_message_id`; the edit call depends on it.
    channel_message_kind: Mapped[str | None] = mapped_column(Text)
    # the admin chat the draft was delivered to; `draft_message_id IS NULL`
    # is delivery's own idempotency guard against Celery redelivery.
    draft_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    draft_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # the rendered video shown to the admin before publication.
    preview_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    preview_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # the `render.params_hash` of that preview: publication sends exactly this file.
    preview_params_hash: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # the stage that failed, written by `set_post_status()`; `/retry` routes by it,
    # since every stage past resolve already has a `draft_message_id`.
    failed_from_status: Mapped[PostStatus | None] = mapped_column(PostStatusType)
    # `created_by_user_id` is who *submitted* the link; this one is who
    # approved and published it — normally two different admins.
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_by_user_id: Mapped[int | None] = mapped_column(BigInteger)

    track: Mapped[Track] = relationship(back_populates="posts")


class AudioAsset(IdAndTimestampsMixin, Base):
    """keyed to a track, so posts share one download. The unique pair is the
    database half of idempotency; the atomic `os.replace` on disk is the other."""

    __tablename__ = "audio_asset"
    __table_args__ = (
        UniqueConstraint("track_id", "video_id"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("duration_ms > 0", name="duration_positive"),
    )

    # CASCADE, unlike `post.track_id`: an artifact of a deleted track is not a
    # reason to refuse the delete, and a file without a row is the safe side.
    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("track.id", ondelete="CASCADE"), nullable=False
    )
    # from `platform_links["youtubeMusic"]` and never `["youtube"]` — stored so
    # a wrong download can be told apart from a wrong choice of source.
    video_id: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # measured by `ffprobe` on the file itself, never copied from the resolve:
    # that is what makes it evidence yt-dlp returned the track we asked for.
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # yt-dlp's extraction path is deprecated upstream: the version tells a broken
    # extractor apart from one missing track.
    ytdlp_version: Mapped[str] = mapped_column(Text, nullable=False)


class Render(IdAndTimestampsMixin, Base):
    """keyed to a post: the caption and fragment bounds belong to the publication.
    `params_hash` covers every input the frames depend on, `text_version` included."""

    __tablename__ = "render"
    __table_args__ = (
        UniqueConstraint("post_id", "params_hash"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("duration_ms > 0", name="duration_positive"),
    )

    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("post.id", ondelete="CASCADE"), nullable=False
    )
    params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # a digest of the `.ass` file, null for a video without text. It is an input
    # to `params_hash`, so the column keeps that hash auditable.
    text_version: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # `ffprobe` of the output, against which the fragment length is asserted:
    # ffmpeg can exit 0 on an unusable file.
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Lyric(IdAndTimestampsMixin, Base):
    """keyed to a track, so posts of one recording share the text. `start_ms` is
    absolute in the full track, NULL for a line not yet timed; there is no `end_ms`."""

    __tablename__ = "lyric"
    __table_args__ = (
        UniqueConstraint("track_id", "line_index"),
        CheckConstraint("start_ms >= 0", name="start_ms_non_negative"),
        CheckConstraint(f"length(text) <= {MAX_LYRIC_TEXT_LENGTH}", name="text_length_bounded"),
    )

    # CASCADE like `audio_asset.track_id`: a stored lyric is not a reason to
    # refuse deleting its track.
    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("track.id", ondelete="CASCADE"), nullable=False
    )
    # the only source of render order — `start_ms` orders nothing.
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(BigInteger)
    # raw as the source gave it; the ASS substitution happens at render time
    # (`worker.ass`), so nothing lossy is stored.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # `'lrclib'`, or `'manual'` for hand-edited text.
    source: Mapped[str] = mapped_column(Text, nullable=False)


def renderable_lyrics_statement(track_id: int) -> Select[tuple[Lyric]]:
    """the render path's only read of `lyric`: a line not yet timed never reaches it."""
    return (
        select(Lyric)
        .where(Lyric.track_id == track_id, Lyric.start_ms.is_not(None))
        .order_by(Lyric.line_index)
    )


class SourceFetch(IdAndTimestampsMixin, Base):
    """`payload` is `{"found": bool, "data": ... | None}`: a miss is cached too,
    so a retry never re-hits it."""

    __tablename__ = "source_fetch"
    __table_args__ = (UniqueConstraint("source", "request_key"),)

    source: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # set from the clock at fetch time: `now()` is the transaction's start.
    # Indexed for the TTL read.
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"), index=True
    )
