"""The sources of plain lyrics (lines without times), in the order they are asked.
A source answers with the same `LyricsResult` as LRCLIB; `UNSYNCED` carries the lines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from pisniar.resolve.cache import SourceCache
from pisniar.resolve.sources import genius
from pisniar.resolve.sources.lrclib import LyricsResult


class PlainLyricsFetch(Protocol):
    def __call__(
        self,
        client: httpx.AsyncClient,
        cache: SourceCache,
        *,
        artist_names: Sequence[str],
        title: str,
        duration_ms: int | None,
    ) -> Awaitable[LyricsResult]: ...


@dataclass(frozen=True)
class PlainLyricsSource:
    """`name` is what a stored line records as its `source`."""

    name: str
    fetch: PlainLyricsFetch


PLAIN_LYRICS_SOURCES = (PlainLyricsSource(name=genius.SOURCE_NAME, fetch=genius.fetch_lyrics),)
