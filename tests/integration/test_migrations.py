"""Migrations must be reversible and match the models. Both tests use the
disposable `migrations_test_database_url`, not the shared `pisniar_test`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

pytestmark = pytest.mark.integration


def test_upgrade_head_then_downgrade_base_then_upgrade_head(
    migrations_test_database_url: str,
    run_alembic: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    upgrade_result = run_alembic("upgrade", "head", database_url=migrations_test_database_url)
    assert upgrade_result.returncode == 0, upgrade_result.stdout + upgrade_result.stderr

    downgrade_result = run_alembic("downgrade", "base", database_url=migrations_test_database_url)
    assert downgrade_result.returncode == 0, downgrade_result.stdout + downgrade_result.stderr

    reupgrade_result = run_alembic("upgrade", "head", database_url=migrations_test_database_url)
    assert reupgrade_result.returncode == 0, reupgrade_result.stdout + reupgrade_result.stderr


def test_alembic_check_reports_no_drift(
    migrations_test_database_url: str,
    run_alembic: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    upgrade_result = run_alembic("upgrade", "head", database_url=migrations_test_database_url)
    assert upgrade_result.returncode == 0, upgrade_result.stdout + upgrade_result.stderr

    check_result = run_alembic("check", database_url=migrations_test_database_url)
    assert check_result.returncode == 0, check_result.stdout + check_result.stderr
