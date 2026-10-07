"""Migrations must be reversible and match the models. Both tests use the
disposable `migrations_test_database_url`, not the shared `pisniar_test`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime

import asyncpg
import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "be8d6bff2a6c"


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


async def test_channel_message_kind_backfill_follows_the_publication_rule(
    migrations_test_database_url: str,
    run_alembic: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """A preview means the video went out, else a cover means a photo, else text; a post that
    was never published stays NULL, and every touched row gets a fresh `updated_at`."""
    old_stamp = datetime(2020, 1, 1, tzinfo=UTC)
    upgraded = run_alembic("upgrade", PREVIOUS_REVISION, database_url=migrations_test_database_url)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

    plain_dsn = (
        make_url(migrations_test_database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    connection = await asyncpg.connect(plain_dsn)
    try:
        for track_id, cover_url in ((1, "https://cdn.example/a.jpg"), (2, None), (3, "")):
            await connection.execute(
                "INSERT INTO track (id, source_url, source_url_normalized, cover_url) "
                "OVERRIDING SYSTEM VALUE VALUES ($1, $2, $2, $3)",
                track_id,
                f"test://{track_id}",
                cover_url,
            )
        rows = [
            (1, 1, 10, 5, "video"),
            (2, 1, 11, None, "photo"),
            (3, 2, 12, None, "text"),
            (4, 3, 13, None, "text"),
            (5, 1, None, None, None),
        ]
        for post_id, track_id, message_id, preview_id, _ in rows:
            await connection.execute(
                "INSERT INTO post (id, track_id, created_by_user_id, channel_chat_id, "
                "channel_message_id, preview_chat_id, preview_message_id, updated_at) "
                "OVERRIDING SYSTEM VALUE VALUES ($1, $2, 1, $3, $4, $5, $6, $7::timestamptz)",
                post_id,
                track_id,
                -100 if message_id else None,
                message_id,
                -200 if preview_id else None,
                preview_id,
                old_stamp,
            )

        head = run_alembic("upgrade", "head", database_url=migrations_test_database_url)
        assert head.returncode == 0, head.stdout + head.stderr

        stored = {
            record["id"]: record
            for record in await connection.fetch(
                "SELECT id, channel_message_kind, updated_at > $1::timestamptz AS touched "
                "FROM post",
                old_stamp,
            )
        }
    finally:
        await connection.close()

    for post_id, _, _, _, expected_kind in rows:
        assert stored[post_id]["channel_message_kind"] == expected_kind, post_id
    assert [stored[post_id]["touched"] for post_id in (1, 2, 3, 4)] == [True] * 4
    assert stored[5]["touched"] is False

    downgraded = run_alembic("downgrade", "-1", database_url=migrations_test_database_url)
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
