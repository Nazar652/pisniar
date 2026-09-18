"""Declarative base with an explicit constraint naming convention.

Set on `MetaData` before the first table is defined — Alembic autogenerate
relies on these names to detect renamed/dropped constraints across
migrations, and retrofitting it after tables exist means a one-off data
migration to rename every constraint in Postgres. Fixed now, for good.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# `column_0_N_label`/`column_0_N_name`, not the single-column
# `column_0_label`/`column_0_name`: for a single-column constraint the two
# render identically (verified in `tests/unit/test_naming_convention.py`),
# but the `_N_` form also handles a future multi-column index/unique
# constraint gracefully (each extra column appends `_N` instead of being
# silently dropped from the name) — cheap to get right now, before any
# table has data a rename would need to migrate.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata
