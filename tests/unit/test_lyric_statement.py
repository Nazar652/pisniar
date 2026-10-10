"""The render's read of `lyric`: it orders by `line_index` and keeps a line not yet
timed (`start_ms` NULL) out of the render, so the render path needs no other guard.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from pisniar.db.models import renderable_lyrics_statement

TRACK_ID = 22


def compiled_sql() -> str:
    statement = renderable_lyrics_statement(TRACK_ID)
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_render_read_orders_by_line_index_and_never_by_the_timecode() -> None:
    sql = compiled_sql()

    assert "ORDER BY lyric.line_index" in sql
    assert "ORDER BY lyric.start_ms" not in sql


def test_the_render_read_leaves_out_a_line_without_a_timecode() -> None:
    sql = compiled_sql()

    assert "lyric.start_ms IS NOT NULL" in sql


def test_the_read_is_scoped_to_one_track() -> None:
    sql = compiled_sql()

    assert f"lyric.track_id = {TRACK_ID}" in sql
