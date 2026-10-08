"""`IdAndTimestampsMixin`: every table gets the same key and timestamp columns, in front,
with `updated_at` stamped by the ORM `onupdate`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.schema import ColumnDefault, DefaultClause

from pisniar.db.base import IdAndTimestampsMixin
from pisniar.db.models import AudioAsset, Lyric, Post, Render, SourceFetch, Track

MODELS = [Track, Post, AudioAsset, Render, Lyric, SourceFetch]


def table_of(model: type) -> Table:
    table = getattr(model, "__table__", None)
    assert isinstance(table, Table)
    return table


@pytest.mark.parametrize("model", MODELS)
def test_every_model_takes_its_key_and_timestamps_from_the_mixin(model: type) -> None:
    assert issubclass(model, IdAndTimestampsMixin)
    assert {"id", "created_at", "updated_at"} <= set(vars(IdAndTimestampsMixin))


@pytest.mark.parametrize("model", MODELS)
def test_the_mixin_columns_come_first_in_the_table(model: type) -> None:
    column_names = [column.name for column in table_of(model).columns]

    assert column_names[:3] == ["id", "created_at", "updated_at"]


@pytest.mark.parametrize("model", MODELS)
def test_the_key_is_an_identity_bigint_primary_key(model: type) -> None:
    column = table_of(model).columns["id"]

    assert isinstance(column.type, BigInteger)
    assert column.primary_key
    assert column.identity is not None


@pytest.mark.parametrize("model", MODELS)
def test_only_updated_at_is_stamped_on_update(model: type) -> None:
    columns = table_of(model).columns

    for column_name in ("created_at", "updated_at"):
        column = columns[column_name]
        assert isinstance(column.type, TIMESTAMP)
        assert column.type.timezone
        assert not column.nullable
        assert isinstance(column.server_default, DefaultClause)
        assert str(column.server_default.arg) == "now()"

    assert columns["created_at"].onupdate is None
    onupdate = columns["updated_at"].onupdate
    assert isinstance(onupdate, ColumnDefault)
    assert str(onupdate.arg) == "now()"
