"""The `_N_`-suffixed naming convention tokens (`column_0_N_label`,
`column_0_N_name`) render identically to their non-`_N_` counterparts for
every *single*-column constraint in the current schema — this pins that
down, so a future switch back (or a genuinely multi-column constraint that
changes a name) is a conscious, visible diff here."""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table

from pisniar.db.models import Post, Track

# `Track.__table__`/`Post.__table__` are typed as the more general
# `FromClause` by SQLAlchemy's declarative stubs, but a mapped class with its
# own table always has a genuine `Table` at runtime — this cast reflects
# that guarantee rather than asserting something unverified.
TRACK_TABLE = cast(Table, Track.__table__)
POST_TABLE = cast(Table, Post.__table__)


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
    }

    check_names = {
        constraint.name
        for constraint in POST_TABLE.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_post_status_valid" in check_names
    assert "ck_post_fragment_bounds_valid" in check_names
    assert "ck_post_channel_ref_complete" in check_names

    fk_names = {fk.name for fk in POST_TABLE.foreign_key_constraints}
    assert fk_names == {"fk_post_track_id_track"}
