"""The engines never log the values of a failed statement: lyric text and captions are bound parameters."""

from __future__ import annotations

import pytest

from pisniar.db.session import build_engine

URL = "postgresql+asyncpg://user:pass@localhost:5432/db"


@pytest.mark.parametrize("use_null_pool", [False, True])
@pytest.mark.parametrize("lock_timeout_ms", [None, 400])
def test_an_engine_hides_the_parameters_of_a_failed_statement(use_null_pool: bool, lock_timeout_ms: int | None) -> None:
    engine = build_engine(URL, use_null_pool=use_null_pool, lock_timeout_ms=lock_timeout_ms)

    assert engine.sync_engine.hide_parameters is True
