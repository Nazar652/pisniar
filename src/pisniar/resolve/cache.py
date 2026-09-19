"""The `source_fetch` cache (contract p.1): one row per `(source,
request_key)`, refreshed once it goes stale (D-40). Every read and write
runs in its own short session — never the caller's business transaction,
whose rollback would otherwise undo the whole task (F-2/F-13).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pisniar.db.models import SourceFetch

DEFAULT_TTL_SECONDS = 3600

# Well under the ~2704-byte btree limit that UNIQUE (source, request_key)
# would hit as a ProgramLimitExceededError, which is not an IntegrityError.
MAX_REQUEST_KEY_BYTES = 1000
KEY_DIGEST_LENGTH = 32

KEY_SEPARATOR = "|"


def normalize_key_part(value: str, *, casefold: bool = True) -> str:
    """Whitespace-collapsed, separator-escaped, and casefolded for free-text
    parts. Identifiers pass `casefold=False` — a Spotify id is base62, so
    folding its case would merge two different tracks into one row."""
    collapsed = " ".join(value.split())
    if casefold:
        collapsed = collapsed.casefold()
    return collapsed.replace("\\", "\\\\").replace(KEY_SEPARATOR, "\\" + KEY_SEPARATOR)


def build_request_key(prefix: str, *parts: str, casefold: bool = True) -> str:
    """`prefix` plus normalized `parts`. An over-long key keeps a readable
    head and ends in a digest of the full value — deterministic, so the
    cache still hits, and never long enough to blow the btree limit."""
    key = prefix + KEY_SEPARATOR.join(normalize_key_part(part, casefold=casefold) for part in parts)
    if len(key.encode("utf-8")) <= MAX_REQUEST_KEY_BYTES:
        return key

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:KEY_DIGEST_LENGTH]
    head_budget = MAX_REQUEST_KEY_BYTES - len(digest) - 1
    head = key.encode("utf-8")[:head_budget].decode("utf-8", errors="ignore")
    return f"{head}#{digest}"


@dataclass(frozen=True)
class SourceCache:
    """Owns its own sessions on purpose: an adapter's cache write must be
    visible to other tasks immediately, and must survive a business-side
    rollback it knows nothing about."""

    session_factory: async_sessionmaker[AsyncSession]
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    async def get_or_fetch(
        self,
        source: str,
        request_key: str,
        fetch: Callable[[], Awaitable[dict[str, object]]],
    ) -> dict[str, object]:
        """Return a still-fresh cached payload, or call `fetch()` and upsert
        it. A stale row is a miss: without that, `{"found": false}` for a
        release song.link had not indexed yet would stick forever (D-40)."""
        cached = await self._select_fresh(source, request_key)
        if cached is not None:
            return cached

        payload = await fetch()
        await self._upsert(source, request_key, payload)
        return payload

    async def _select_fresh(self, source: str, request_key: str) -> dict[str, object] | None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.ttl_seconds)
        statement = select(SourceFetch).where(
            SourceFetch.source == source,
            SourceFetch.request_key == request_key,
            SourceFetch.fetched_at > cutoff,
        )
        async with self.session_factory() as session:
            row = await session.scalar(statement)
            return row.payload if row is not None else None

    async def _upsert(self, source: str, request_key: str, payload: dict[str, object]) -> None:
        # `fetched_at` from the clock, not `now()`: the latter is the
        # transaction's start time, tens of seconds stale after a resolve.
        fetched_at = datetime.now(UTC)
        statement = (
            insert(SourceFetch)
            .values(
                source=source,
                request_key=request_key,
                payload=payload,
                fetched_at=fetched_at,
                updated_at=fetched_at,
            )
            .on_conflict_do_update(
                index_elements=[SourceFetch.source, SourceFetch.request_key],
                set_={"payload": payload, "fetched_at": fetched_at, "updated_at": fetched_at},
            )
        )
        async with self.session_factory() as session:
            await session.execute(statement)
            await session.commit()
