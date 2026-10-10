"""Pure `.ass` generator: stored lyric lines in, subtitle file text out. Song text is untrusted and ASS has no escapes
inside a line, so `{`/`}` become `(`/`)` and `\\` is dropped: override blocks like `{\\an7\\fs90}` cannot be spelled."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pisniar.logging import get_logger
from pisniar.post.sanitize import sanitize_metadata_text
from pisniar.worker.text_metrics import fits_within, longest_prefix_within

logger = get_logger(__name__)

# without these libass assumes a 384x288 script and scales every coordinate by
# the wrong factor. The subtitles are drawn onto the finished frame.
PLAY_RES_X = 720
PLAY_RES_Y = 1280

# the only family verified to carry Ukrainian in the worker image. A missing name
# falls back silently, which is why `tests/integration/test_ass_font.py` exists.
FONT_FAMILY = "Noto Sans"

# the lyric band starts under the raised cover (`render.COVER_TOP_OFFSET_PX`);
# `tests/unit/worker/test_ass.py` pins the two against each other.
TEXT_BAND_TOP_PX = 820

# `Alignment=5` plus an explicit `\pos` makes the three slots symmetric by
# construction rather than by `MarginV`, which `\an5` ignores anyway.
SLOT_CENTER_X = PLAY_RES_X // 2
CURRENT_SLOT_Y = 1050
# the tightest case is a current line wrapped to two; `test_ass.py` measures that
# block against its neighbours rather than trusting the number.
SLOT_SPACING_PX = 95
PREVIOUS_SLOT_Y = CURRENT_SLOT_Y - SLOT_SPACING_PX
NEXT_SLOT_Y = CURRENT_SLOT_Y + SLOT_SPACING_PX

FONT_SIZE_CURRENT_PX = 44
FONT_SIZE_ADJACENT_PX = 32
HORIZONTAL_MARGIN_PX = 40

# `WrapStyle: 2` turns libass' own wrapping off, so the break points are ours,
# measured against the font's real advance widths (`text_metrics`).
USABLE_TEXT_WIDTH_PX = PLAY_RES_X - 2 * HORIZONTAL_MARGIN_PX
MAX_CURRENT_WRAPPED_LINES = 2
# one, not two: a second wrapped line in a side slot is exactly how a long line climbs into the neighbouring one.
MAX_ADJACENT_WRAPPED_LINES = 1
ELLIPSIS = "…"

# without a ceiling the last line of a fragment hangs for the rest of it, and a
# 60-second gap in the source hangs for a minute.
MAX_LINE_MS = 8_000
# a 45-second fragment with more than this many lines is a broken LRC or an attempt to spend ffmpeg's CPU, not a song.
MAX_RENDERED_LINES = 120
# mirrors `models.MAX_LYRIC_TEXT_LENGTH`: past it the wrapper would be word-splitting a payload, not a lyric.
MAX_SOURCE_TEXT_CHARACTERS = 500

STYLE_CURRENT = "current"
STYLE_ADJACENT = "adjacent"

# the `Format:` line each `Style:` row is positional against; rows look every
# field up in this order, so they cannot drift out of step with the header.
STYLE_FORMAT_FIELDS = (
    "Name",
    "Fontname",
    "Fontsize",
    "PrimaryColour",
    "SecondaryColour",
    "OutlineColour",
    "BackColour",
    "Bold",
    "Italic",
    "Underline",
    "StrikeOut",
    "ScaleX",
    "ScaleY",
    "Spacing",
    "Angle",
    "BorderStyle",
    "Outline",
    "Shadow",
    "Alignment",
    "MarginL",
    "MarginR",
    "MarginV",
    "Encoding",
)

EVENT_FORMAT_FIELDS = (
    "Layer",
    "Start",
    "End",
    "Style",
    "Name",
    "MarginL",
    "MarginR",
    "MarginV",
    "Effect",
    "Text",
)

# `\an5` centres on the `\pos` point and ignores `MarginV` entirely, so the vertical margin is honestly zero here.
ALIGNMENT_MIDDLE_CENTER = 5

ASS_SUBSTITUTIONS = str.maketrans({"{": "(", "}": ")", "\\": None})


class AssGenerationError(ValueError):
    """our own failure, never the source's — it fails a post loudly."""


@dataclass(frozen=True)
class LyricLine:
    """`start_ms` is absolute in the *full* track and optional on purpose, so
    allowing untimed lines in the column does not change this signature."""

    line_index: int
    start_ms: int | None
    text: str


@dataclass(frozen=True)
class PlacedLine:
    """a line already shifted into fragment time and already escaped."""

    start_ms: int
    text: str


def escape_ass_text(value: str) -> str:
    """substitution, not escaping: `sanitize_metadata_text` folds line breaks and
    drops Cc/Cf (U+202E, U+200B), then the table removes the override characters."""
    substituted = sanitize_metadata_text(value).translate(ASS_SUBSTITUTIONS)
    return " ".join(substituted.split())


def wrap_for_slot(
    text: str,
    *,
    font_size_px: int,
    max_lines: int,
) -> str:
    """the literal `\\N` is safe only because `escape_ass_text` already removed
    every backslash the source could contribute."""
    wrapped = wrap_words(text[:MAX_SOURCE_TEXT_CHARACTERS], font_size_px)

    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = with_ellipsis(wrapped[-1], font_size_px)

    return "\\N".join(wrapped)


def build_ass_subtitles(
    lines: Sequence[LyricLine],
    *,
    fragment_start_ms: int,
    fragment_end_ms: int,
) -> str | None:
    """`None` when the fragment has nothing to draw: «no text» is a state
    (`text_version=None`), not an empty file."""
    if fragment_end_ms <= fragment_start_ms:
        raise AssGenerationError(f"межі фрагмента невалідні: [{fragment_start_ms}, {fragment_end_ms})")

    fragment_length_ms = fragment_end_ms - fragment_start_ms
    placed = place_lines(lines, fragment_start_ms)
    events = build_events(placed, fragment_length_ms)

    if not events:
        logger.info("ass_nothing_to_render", line_count=len(lines))
        return None

    logger.info("ass_generated", line_count=len(lines), event_count=len(events))
    return "\n".join([*script_info_section(), "", *styles_section(), "", *events_section(events)]) + "\n"


def place_lines(lines: Sequence[LyricLine], fragment_start_ms: int) -> list[PlacedLine]:
    """ordered by `line_index`, never by `start_ms`: sorting by time would leave
    untimed lines nowhere to stand."""
    timed = [line for line in lines if line.start_ms is not None]
    ordered = sorted(timed, key=lambda line: line.line_index)

    return [
        PlacedLine(
            # the one place absolute milliseconds become fragment-local ones.
            start_ms=absolute_start_ms(line) - fragment_start_ms,
            text=escape_ass_text(line.text),
        )
        for line in ordered
    ]


def absolute_start_ms(line: LyricLine) -> int:
    if line.start_ms is None:  # pragma: no cover - filtered out one frame up
        raise AssGenerationError("рядок без таймкоду не доходить сюди")
    return line.start_ms


def build_events(placed: Sequence[PlacedLine], fragment_length_ms: int) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    rendered_count = 0

    for index, line in enumerate(placed):
        if not line.text:
            continue

        window = display_window(placed, index, fragment_length_ms)
        if window is None:
            continue

        if rendered_count >= MAX_RENDERED_LINES:
            logger.warning(
                "ass_line_budget_exhausted",
                max_rendered_lines=MAX_RENDERED_LINES,
                line_count=len(placed),
            )
            break

        start_ms, end_ms = window
        events.extend(slot_events(placed, index, start_ms, end_ms))
        rendered_count += 1

    return events


def display_window(
    placed: Sequence[PlacedLine],
    index: int,
    fragment_length_ms: int,
) -> tuple[int, int] | None:
    """a pause entry counts as the next line, which is exactly what ends a verse."""
    start_ms = placed[index].start_ms
    end_ms = min(start_ms + MAX_LINE_MS, fragment_length_ms)

    if index + 1 < len(placed):
        end_ms = min(end_ms, placed[index + 1].start_ms)

    start_ms = max(start_ms, 0)
    if end_ms <= start_ms:
        return None
    return start_ms, end_ms


def slot_events(
    placed: Sequence[PlacedLine],
    index: int,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, str]]:
    """the neighbours are the nearest lines that draw something, so a pause between verses never leaves a blank slot."""
    previous = neighbour_text(placed, index, step=-1)
    following = neighbour_text(placed, index, step=1)

    slots = [
        (previous, PREVIOUS_SLOT_Y, STYLE_ADJACENT, FONT_SIZE_ADJACENT_PX),
        (placed[index].text, CURRENT_SLOT_Y, STYLE_CURRENT, FONT_SIZE_CURRENT_PX),
        (following, NEXT_SLOT_Y, STYLE_ADJACENT, FONT_SIZE_ADJACENT_PX),
    ]

    events: list[dict[str, str]] = []
    for text, slot_y, style, font_size_px in slots:
        if not text:
            continue

        max_lines = MAX_CURRENT_WRAPPED_LINES if style == STYLE_CURRENT else MAX_ADJACENT_WRAPPED_LINES
        wrapped = wrap_for_slot(text, font_size_px=font_size_px, max_lines=max_lines)
        events.append(
            {
                "Layer": "0",
                "Start": ass_timestamp(start_ms),
                "End": ass_timestamp(end_ms),
                "Style": style,
                "Name": "",
                "MarginL": "0",
                "MarginR": "0",
                "MarginV": "0",
                "Effect": "",
                "Text": f"{{\\pos({SLOT_CENTER_X},{slot_y})}}{wrapped}",
            }
        )
    return events


def neighbour_text(
    placed: Sequence[PlacedLine],
    index: int,
    *,
    step: int,
) -> str:
    position = index + step
    while 0 <= position < len(placed):
        if placed[position].text:
            return placed[position].text
        position += step
    return ""


def ass_timestamp(milliseconds: int) -> str:
    """`H:MM:SS.CC` — ASS keeps centiseconds, so the tail is truncated, never rounded up past the next line's start."""
    total_centiseconds, _ = divmod(max(milliseconds, 0), 10)
    total_seconds, centiseconds = divmod(total_centiseconds, 100)
    total_minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def script_info_section() -> list[str]:
    return [
        "[Script Info]",
        "; Generated by pisniar — regenerated on every render, never edited.",
        "ScriptType: v4.00+",
        f"PlayResX: {PLAY_RES_X}",
        f"PlayResY: {PLAY_RES_Y}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
    ]


def styles_section() -> list[str]:
    return [
        "[V4+ Styles]",
        "Format: " + ", ".join(STYLE_FORMAT_FIELDS),
        style_row(STYLE_CURRENT, FONT_SIZE_CURRENT_PX, "&H00FFFFFF", "&H00101010"),
        style_row(STYLE_ADJACENT, FONT_SIZE_ADJACENT_PX, "&H80FFFFFF", "&H80101010"),
    ]


def style_row(
    name: str,
    font_size_px: int,
    primary_colour: str,
    outline_colour: str,
) -> str:
    """a forgotten field is a `KeyError` here, not a style libass silently ignores."""
    values: dict[str, object] = {
        "Name": name,
        "Fontname": FONT_FAMILY,
        "Fontsize": font_size_px,
        "PrimaryColour": primary_colour,
        "SecondaryColour": primary_colour,
        "OutlineColour": outline_colour,
        "BackColour": "&H80000000",
        "Bold": 0,
        "Italic": 0,
        "Underline": 0,
        "StrikeOut": 0,
        "ScaleX": 100,
        "ScaleY": 100,
        "Spacing": 0,
        "Angle": 0,
        "BorderStyle": 1,
        "Outline": 2,
        "Shadow": 0,
        "Alignment": ALIGNMENT_MIDDLE_CENTER,
        "MarginL": HORIZONTAL_MARGIN_PX,
        "MarginR": HORIZONTAL_MARGIN_PX,
        "MarginV": 0,
        "Encoding": 1,
    }
    return "Style: " + ",".join(str(values[field]) for field in STYLE_FORMAT_FIELDS)


def events_section(events: Sequence[dict[str, str]]) -> list[str]:
    return [
        "[Events]",
        "Format: " + ", ".join(EVENT_FORMAT_FIELDS),
        *(event_row(event) for event in events),
    ]


def event_row(event: dict[str, str]) -> str:
    """`Text` is the last field, the only reason a comma inside the lyrics is harmless; a test pins it."""
    return "Dialogue: " + ",".join(event[field] for field in EVENT_FORMAT_FIELDS)


def wrap_words(text: str, font_size_px: int) -> list[str]:
    wrapped: list[str] = []
    current = ""

    for word in text.split():
        remainder = word
        while not fits_text_width(remainder, font_size_px):
            head = longest_prefix_within(remainder, font_size_px=font_size_px, available_px=USABLE_TEXT_WIDTH_PX)
            if current:
                wrapped.append(current)
                current = ""

            wrapped.append(head)
            remainder = remainder[len(head) :]

        candidate = f"{current} {remainder}".strip()
        if fits_text_width(candidate, font_size_px):
            current = candidate
            continue

        if current:
            wrapped.append(current)
        current = remainder

    if current:
        wrapped.append(current)
    return wrapped or [""]


def fits_text_width(text: str, font_size_px: int) -> bool:
    return fits_within(text, font_size_px=font_size_px, available_px=USABLE_TEXT_WIDTH_PX)


def with_ellipsis(line: str, font_size_px: int) -> str:
    while line and not fits_text_width(line + ELLIPSIS, font_size_px):
        line = line[:-1].rstrip()
    return line + ELLIPSIS
