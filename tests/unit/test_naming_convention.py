"""`_N_`-suffixed naming tokens render identically to non-`_N_` ones for
every single-column constraint today — pins that down so a future rename
is a conscious diff here.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table

from pisniar.db.models import Post, SourceFetch, Track

# Typed as FromClause by the stubs, but always a real Table at runtime.
TRACK_TABLE = cast(Table, Track.__table__)
POST_TABLE = cast(Table, Post.__table__)
SOURCE_FETCH_TABLE = cast(Table, SourceFetch.__table__)


def test_track_constraint_names_are_unchanged() -> None:
    assert TRACK_TABLE.primary_key.name == "pk_track"
    unique_constraint_names = {
        constraint.name
        for constraint in TRACK_TABLE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_constraint_names == {"uq_track_source_url_normalized"}


def test_post_constraint_and_index_names_are_unchanged() -> None:
    assert POST_TABLE.primary_key.name == "pk_post"

    index_names = {(index.name, index.unique) for index in POST_TABLE.indexes}
    assert index_names == {
        ("ix_post_status", False),
        ("ix_post_track_id", False),
        ("uq_post_channel_message", True),
        ("uq_post_draft_message", True),
    }

    check_names = {
        constraint.name
        for constraint in POST_TABLE.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_post_status_valid" in check_names
    assert "ck_post_fragment_bounds_valid" in check_names
    assert "ck_post_channel_ref_complete" in check_names
    assert "ck_post_draft_ref_complete" in check_names

    fk_names = {fk.name for fk in POST_TABLE.foreign_key_constraints}
    assert fk_names == {"fk_post_track_id_track"}


def test_source_fetch_constraint_names_are_unchanged() -> None:
    assert SOURCE_FETCH_TABLE.primary_key.name == "pk_source_fetch"
    unique_constraint_names = {
        constraint.name
        for constraint in SOURCE_FETCH_TABLE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_constraint_names == {"uq_source_fetch_source_request_key"}
