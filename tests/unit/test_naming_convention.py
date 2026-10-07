"""`_N_`-suffixed naming tokens render identically to non-`_N_` ones for
every single-column constraint today — pins that down so a future rename
is a conscious diff here.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table

from pisniar.db.models import AudioAsset, Lyric, Post, Render, SourceFetch, Track

# typed as FromClause by the stubs, but always a real Table at runtime.
TRACK_TABLE = cast(Table, Track.__table__)
POST_TABLE = cast(Table, Post.__table__)
SOURCE_FETCH_TABLE = cast(Table, SourceFetch.__table__)
AUDIO_ASSET_TABLE = cast(Table, AudioAsset.__table__)
RENDER_TABLE = cast(Table, Render.__table__)
LYRIC_TABLE = cast(Table, Lyric.__table__)


def test_track_constraint_names_are_unchanged() -> None:
    assert TRACK_TABLE.primary_key.name == "pk_track"
    unique_constraint_names = {
        constraint.name
        for constraint in TRACK_TABLE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_constraint_names == {"uq_track_source_url_normalized"}

    check_names = {
        constraint.name
        for constraint in TRACK_TABLE.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert check_names == {
        "ck_track_artists_is_array",
        "ck_track_platform_links_is_object",
        "ck_track_languages_is_array",
        "ck_track_manual_fields_is_array",
        "ck_track_manual_fields_known_names",
        "ck_track_removed_platforms_is_array",
    }


def test_post_constraint_and_index_names_are_unchanged() -> None:
    assert POST_TABLE.primary_key.name == "pk_post"

    index_names = {(index.name, index.unique) for index in POST_TABLE.indexes}
    assert index_names == {
        ("ix_post_status", False),
        ("ix_post_track_id", False),
        ("uq_post_channel_message", True),
        ("uq_post_draft_message", True),
        ("uq_post_preview_message", True),
    }

    check_names = {
        constraint.name
        for constraint in POST_TABLE.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_post_status_valid" in check_names
    assert "ck_post_failed_from_status_valid" in check_names
    assert "ck_post_fragment_bounds_valid" in check_names
    assert "ck_post_channel_ref_complete" in check_names
    assert "ck_post_draft_ref_complete" in check_names
    assert "ck_post_preview_ref_complete" in check_names
    assert "ck_post_channel_message_kind_valid" in check_names
    assert "ck_post_channel_message_kind_matches_ref" in check_names

    foreign_key_names = {foreign_key.name for foreign_key in POST_TABLE.foreign_key_constraints}
    assert foreign_key_names == {"fk_post_track_id_track"}


def test_media_artifact_constraint_names_are_unchanged() -> None:
    assert AUDIO_ASSET_TABLE.primary_key.name == "pk_audio_asset"
    assert RENDER_TABLE.primary_key.name == "pk_render"

    unique_names = {
        constraint.name
        for table in (AUDIO_ASSET_TABLE, RENDER_TABLE)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_names == {"uq_audio_asset_track_id_video_id", "uq_render_post_id_params_hash"}

    check_names = {
        constraint.name
        for table in (AUDIO_ASSET_TABLE, RENDER_TABLE)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert check_names == {
        "ck_audio_asset_size_positive",
        "ck_audio_asset_duration_positive",
        "ck_render_size_positive",
        "ck_render_duration_positive",
    }

    foreign_key_names = {
        foreign_key.name
        for table in (AUDIO_ASSET_TABLE, RENDER_TABLE)
        for foreign_key in table.foreign_key_constraints
    }
    assert foreign_key_names == {"fk_audio_asset_track_id_track", "fk_render_post_id_post"}


def test_source_fetch_constraint_names_are_unchanged() -> None:
    assert SOURCE_FETCH_TABLE.primary_key.name == "pk_source_fetch"
    unique_constraint_names = {
        constraint.name
        for constraint in SOURCE_FETCH_TABLE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_constraint_names == {"uq_source_fetch_source_request_key"}


def test_lyric_constraint_names_are_unchanged() -> None:
    assert LYRIC_TABLE.primary_key.name == "pk_lyric"

    unique_constraint_names = {
        constraint.name
        for constraint in LYRIC_TABLE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_constraint_names == {"uq_lyric_track_id_line_index"}

    check_names = {
        constraint.name
        for constraint in LYRIC_TABLE.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert check_names == {"ck_lyric_start_ms_non_negative", "ck_lyric_text_length_bounded"}

    foreign_key_names = {foreign_key.name for foreign_key in LYRIC_TABLE.foreign_key_constraints}
    assert foreign_key_names == {"fk_lyric_track_id_track"}


def test_a_lyric_follows_its_track_out_of_the_database() -> None:
    """CASCADE like `audio_asset`, unlike `post.track_id`'s RESTRICT: stored
    text is not a reason to refuse deleting a track."""
    foreign_key = next(iter(LYRIC_TABLE.foreign_key_constraints)).elements[0]

    assert foreign_key.ondelete == "CASCADE"
