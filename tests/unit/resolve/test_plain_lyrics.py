"""`plain_lyrics`: the ordered tuple of plain-text sources and its entries."""

from __future__ import annotations

from pisniar.resolve.plain_lyrics import PLAIN_LYRICS_SOURCES, PlainLyricsSource
from pisniar.resolve.sources import genius


def test_genius_is_the_one_plain_source_and_its_name_is_what_a_stored_line_records() -> None:
    assert (PlainLyricsSource(name="genius", fetch=genius.fetch_lyrics),) == PLAIN_LYRICS_SOURCES


def test_source_names_are_unique() -> None:
    names = [source.name for source in PLAIN_LYRICS_SOURCES]

    assert len(set(names)) == len(names)
