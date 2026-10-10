"""LRC parsing: a pure function from a source payload to timed lines. The lyrics themselves are never logged, only their
shape. A malformed line is dropped with a WARN; more than `MAX_DROPPED_LINE_RATIO` of them means the payload is not LRC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pisniar.logging import get_logger

logger = get_logger(__name__)

# above this share of unparseable lines the payload is a different format —
# plain lyrics, an error page, HTML — not "a few crooked lines".
MAX_DROPPED_LINE_RATIO = 0.2

# `[mm:ss.xx]` and `[mm:ss.xxx]`; the fraction is optional and `mm` may
# exceed 59, both of which real LRC payloads contain.
TIMESTAMP_PATTERN = re.compile(r"^(\d{1,4}):([0-5]?\d)(?:[.:](\d{1,3}))?$")
# `[ar:...]`, `[ti:...]`, `[length:...]`, `[by:]`, `[offset:]` — ignored,
# `offset` included: applying it would shift every timecode silently.
METADATA_PATTERN = re.compile(r"^[A-Za-z_]+:")
LEADING_TAG_PATTERN = re.compile(r"\[([^\]]*)\]")


class LrcParseError(ValueError):
    """the payload is not LRC — too large a share of it did not parse."""


@dataclass(frozen=True)
class LrcLine:
    """`start_ms` is relative to the start of the *full* track, as `lyric.start_ms`."""

    start_ms: int
    text: str

    @property
    def is_pause(self) -> bool:
        """an empty text under a timecode is a gap marker: it draws nothing and it is what ends the line before it."""
        return not self.text


def parse_lrc(content: str) -> list[LrcLine]:
    """timed lines sorted by time, several per source line when it carries
    several tags. Metadata tags are ignored; ties keep source order."""
    parsed: list[LrcLine] = []
    considered_count = 0
    dropped_count = 0

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        source_line = raw_line.strip()
        if not source_line:
            continue

        tags, text = split_tags(source_line)
        timestamps = timestamps_of(tags)

        if not timestamps and not text and tags and all(METADATA_PATTERN.match(tag) for tag in tags):
            continue

        considered_count += 1

        if not timestamps:
            dropped_count += 1
            logger.warning("lrc_line_dropped", line_number=line_number, length=len(source_line))
            continue

        parsed.extend(LrcLine(start_ms=start_ms, text=text) for start_ms in timestamps)

    refuse_a_payload_that_is_not_lrc(considered_count, dropped_count)

    parsed.sort(key=lambda line: line.start_ms)
    logger.info(
        "lrc_parsed",
        line_count=len(parsed),
        pause_count=sum(1 for line in parsed if line.is_pause),
        dropped_count=dropped_count,
    )
    return parsed


def split_tags(source_line: str) -> tuple[list[str], str]:
    """tags only count at the start: a `[` inside the lyrics is text."""
    tags: list[str] = []
    position = 0

    while (match := LEADING_TAG_PATTERN.match(source_line, position)) is not None:
        tags.append(match.group(1))
        position = match.end()

    return tags, " ".join(source_line[position:].split())


def timestamps_of(tags: list[str]) -> list[int]:
    """several tags on one source line mean the same text repeats at several
    moments — a documented LRC shorthand, not a duplicate."""
    parsed_tags = (parse_timestamp(tag) for tag in tags)
    return [start_ms for start_ms in parsed_tags if start_ms is not None]


def parse_timestamp(tag: str) -> int | None:
    match = TIMESTAMP_PATTERN.match(tag)
    if match is None:
        return None

    minutes, seconds, fraction = match.groups()
    # `.x` is tenths and `.xx` hundredths, so the fraction is right-padded
    # to milliseconds rather than read as a plain integer.
    milliseconds = int(fraction.ljust(3, "0")) if fraction else 0
    return int(minutes) * 60_000 + int(seconds) * 1_000 + milliseconds


def refuse_a_payload_that_is_not_lrc(considered_count: int, dropped_count: int) -> None:
    if considered_count == 0:
        return

    dropped_ratio = dropped_count / considered_count
    if dropped_ratio <= MAX_DROPPED_LINE_RATIO:
        return

    raise LrcParseError(
        f"не розпізнано {dropped_count} рядків із {considered_count} "
        f"({dropped_ratio:.0%}, поріг {MAX_DROPPED_LINE_RATIO:.0%}) — "
        f"це не LRC, а інший формат"
    )
