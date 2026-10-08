"""Declarative base with an explicit constraint naming convention, fixed
before the first table — retrofitting it later means a data migration to
rename every constraint in Postgres.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, MetaData, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import Identity

# _N_ variants: same result for single-column constraints (see
# test_naming_convention.py), but handle a future multi-column one too.
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


class IdAndTimestampsMixin:
    """the key and the two timestamps every table carries, first in column order.
    `updated_at` comes from the ORM `onupdate`, so a raw UPDATE has to set it itself."""

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True, sort_order=-3)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"), sort_order=-2
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
        sort_order=-1,
    )
