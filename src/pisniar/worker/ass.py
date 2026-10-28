"""Pure `.ass` generators: stored lyric lines for the glass screen, and the title and
artists for the display. Song text is untrusted and ASS has no escapes inside a line, so
`{`/`}` become `(`/`)` and `\\` is dropped: override blocks like `{\\an7\\fs90}` cannot be spelled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pisniar.logging import get_logger
from pisniar.post.sanitize import sanitize_metadata_text
from pisniar.worker import layout
from pisniar.worker.text_metrics import fits_within, longest_prefix_within, text_width_px

logger = get_logger(__name__)

# without these libass assumes a 384x288 script and scales every coordinate by
# the wrong factor. The subtitles are drawn onto the finished frame.
PLAY_RES_X = layout.FRAME_WIDTH_PX
PLAY_RES_Y = layout.FRAME_HEIGHT_PX

# the only family verified to carry Ukrainian in the worker image. A missing name
# falls back silently, which is why `tests/integration/test_ass_font.py` exists.
FONT_FAMILY = "Noto Sans"

# `\an5` plus an explicit `\pos` places the slots (`MarginV` is ignored); they sit
# on the glass screen, and `test_ass.py` pins them inside it.
SLOT_CENTER_X = layout.FRAME_CENTER_X_PX
CURRENT_SLOT_Y = layout.LYRICS_CURRENT_SLOT_Y_PX
# the tightest case is a current line wrapped to two; `test_ass.py` measures that
# block against its neighbours rather than trusting the number.
SLOT_SPACING_PX = layout.LYRICS_SLOT_SPACING_PX
PREVIOUS_SLOT_Y = CURRENT_SLOT_Y - SLOT_SPACING_PX
NEXT_SLOT_Y = CURRENT_SLOT_Y + SLOT_SPACING_PX

FONT_SIZE_CURRENT_PX = layout.LYRICS_CURRENT_FONT_SIZE_PX
FONT_SIZE_ADJACENT_PX = layout.LYRICS_ADJACENT_FONT_SIZE_PX
HORIZONTAL_MARGIN_PX = (PLAY_RES_X - layout.LYRICS_TEXT_WIDTH_PX) // 2

CURRENT_RGB = (255, 255, 255)
ADJACENT_RGB = (150, 156, 170)
LYRICS_OUTLINE_PX = 2

# the display: one line each, centred on the display pane
DISPLAY_TITLE_Y = layout.DISPLAY_TITLE_Y_PX
DISPLAY_ARTISTS_Y = layout.DISPLAY_ARTISTS_Y_PX
FONT_SIZE_DISPLAY_TITLE_PX = layout.DISPLAY_TITLE_FONT_SIZE_PX
FONT_SIZE_DISPLAY_ARTISTS_PX = layout.DISPLAY_ARTISTS_FONT_SIZE_PX
DISPLAY_TITLE_RGB = (255, 196, 110)
DISPLAY_ARTISTS_RGB = (190, 140, 80)
DISPLAY_GLOW_BLUR = 6
DISPLAY_GLOW_OUTLINE_PX = 3
DISPLAY_GLOW_ALPHA = "&H90&"
ARTIST_SEPARATOR = ", "

# `WrapStyle: 2` turns libass' own wrapping off, so the break points are ours,
# measured against the font's real advance widths (`text_metrics`).
USABLE_TEXT_WIDTH_PX = layout.LYRICS_TEXT_WIDTH_PX
MAX_CURRENT_WRAPPED_LINES = 2
# one, not two: a second wrapped line in a side slot is exactly how a long line climbs into the neighbouring one.
MAX_ADJACENT_WRAPPED_LINES = 1
ELLIPSIS = "…"

# a line stays until the next entry; only the last entry of the text, with nothing after it, gets this ceiling.
# The Mini App's `lyrics/timing.ts` mirrors it; `test_frontend_wiring.py` ties the two.
LAST_LINE_MAX_MS = 8_000
# windows that draw, pauses included: more than this in a fragment is a broken LRC or an attempt to spend ffmpeg's CPU.
MAX_RENDERED_LINES = 120
# mirrors `models.MAX_LYRIC_TEXT_LENGTH`: past it the wrapper would be word-splitting a payload, not a lyric.
MAX_SOURCE_TEXT_CHARACTERS = 500

STYLE_CURRENT = "current"
STYLE_ADJACENT = "adjacent"
STYLE_DISPLAY_TITLE = "display_title"
STYLE_DISPLAY_ARTISTS = "display_artists"

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
        wrapped[-1] = with_ellipsis(wrapped[-1], font_size_px, USABLE_TEXT_WIDTH_PX)

    return "\\N".join(wrapped)


def build_ass_subtitles(
    lines: Sequence[LyricLine],
    *,
    fragment_start_ms: int,
    fragment_end_ms: int,
) -> str | None:
    """`None` when the fragment has nothing to draw: «no text» is a state (`text_version=None`), not an empty file."""
    if fragment_end_ms <= fragment_start_ms:
        raise AssGenerationError(f"межі фрагмента невалідні: [{fragment_start_ms}, {fragment_end_ms})")

    fragment_length_ms = fragment_end_ms - fragment_start_ms
    placed = place_lines(lines, fragment_start_ms)
    events = build_events(placed, fragment_length_ms)

    if not events:
        logger.info("ass_nothing_to_render", line_count=len(lines))
        return None

    logger.info("ass_generated", line_count=len(lines), event_count=len(events))
    return document_text(lyric_style_rows(), events)


def build_display_subtitles(
    title: str,
    artist_names: Sequence[str],
    length_ms: int,
) -> str:
    """the title and the artists on the display for the whole fragment: each line is a glow
    event under a crisp one. An empty field draws nothing, so the result may hold no events."""
    if length_ms <= 0:
        raise AssGenerationError(f"тривалість фрагмента невалідна: {length_ms}")

    title_text = display_text(title, FONT_SIZE_DISPLAY_TITLE_PX)
    artist_texts = (escape_ass_text(name[:MAX_SOURCE_TEXT_CHARACTERS]) for name in artist_names)
    artists_text = display_text(
        ARTIST_SEPARATOR.join(text for text in artist_texts if text), FONT_SIZE_DISPLAY_ARTISTS_PX
    )
    end_ms = length_ms
    events = [
        *display_events(title_text, DISPLAY_TITLE_Y, STYLE_DISPLAY_TITLE, DISPLAY_TITLE_RGB, end_ms),
        *display_events(artists_text, DISPLAY_ARTISTS_Y, STYLE_DISPLAY_ARTISTS, DISPLAY_ARTISTS_RGB, end_ms),
    ]
    logger.info("ass_display_generated", event_count=len(events))
    return document_text(display_style_rows(), events)


def document_text(style_rows: Sequence[str], events: Sequence[dict[str, str]]) -> str:
    sections = [
        *script_info_section(),
        "",
        *styles_section(style_rows),
        "",
        *events_section(events),
    ]
    return "\n".join(sections) + "\n"


def display_text(value: str, font_size_px: int) -> str:
    """cut to the input limit before escaping, then to the display width with an ellipsis."""
    escaped = escape_ass_text(value[:MAX_SOURCE_TEXT_CHARACTERS])
    if fits_text_width(escaped, font_size_px, layout.DISPLAY_TEXT_WIDTH_PX):
        return escaped

    room_px = layout.DISPLAY_TEXT_WIDTH_PX - text_width_px(ELLIPSIS, font_size_px)
    head = longest_prefix_within(escaped, font_size_px=font_size_px, available_px=room_px)
    return head.rstrip() + ELLIPSIS


def display_events(
    text: str,
    slot_y: int,
    style: str,
    rgb: tuple[int, int, int],
    end_ms: int,
) -> list[dict[str, str]]:
    """a blurred, translucent outline in the text colour under the crisp text."""
    if not text:
        return []

    position = f"\\pos({SLOT_CENTER_X},{slot_y})"
    glow = (
        f"{position}\\blur{DISPLAY_GLOW_BLUR}\\alpha{DISPLAY_GLOW_ALPHA}"
        f"\\bord{DISPLAY_GLOW_OUTLINE_PX}\\shad0\\3c{tag_colour(rgb)}"
    )
    crisp = f"{position}\\bord0\\shad0"
    return [dialogue_event(layer, 0, end_ms, style, f"{{{tags}}}{text}") for layer, tags in ((0, glow), (1, crisp))]


def place_lines(lines: Sequence[LyricLine], fragment_start_ms: int) -> list[PlacedLine]:
    """ordered by `line_index`, never by `start_ms`: sorting by time would leave untimed lines nowhere to stand."""
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

    for index in range(len(placed)):
        window = display_window(placed, index, fragment_length_ms)
        if window is None:
            continue

        start_ms, end_ms = window
        window_events = slot_events(placed, index, start_ms, end_ms)
        if not window_events:
            continue

        if rendered_count >= MAX_RENDERED_LINES:
            logger.warning(
                "ass_line_budget_exhausted",
                max_rendered_lines=MAX_RENDERED_LINES,
                line_count=len(placed),
            )
            break

        events.extend(window_events)
        rendered_count += 1

    return events


def display_window(
    placed: Sequence[PlacedLine],
    index: int,
    fragment_length_ms: int,
) -> tuple[int, int] | None:
    """an entry, a pause included, lasts until the next one starts; the last one gets `LAST_LINE_MAX_MS`."""
    start_ms = placed[index].start_ms

    if index + 1 < len(placed):
        end_ms = min(placed[index + 1].start_ms, fragment_length_ms)
    else:
        end_ms = min(start_ms + LAST_LINE_MAX_MS, fragment_length_ms)

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
    """the neighbours are the nearest lines that draw something; a pause empties the middle slot only."""
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
        events.append(dialogue_event(0, start_ms, end_ms, style, f"{{\\pos({SLOT_CENTER_X},{slot_y})}}{wrapped}"))
    return events


def dialogue_event(
    layer: int,
    start_ms: int,
    end_ms: int,
    style: str,
    text: str,
) -> dict[str, str]:
    return {
        "Layer": str(layer),
        "Start": ass_timestamp(start_ms),
        "End": ass_timestamp(end_ms),
        "Style": style,
        "Name": "",
        "MarginL": "0",
        "MarginR": "0",
        "MarginV": "0",
        "Effect": "",
        "Text": text,
    }


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


def styles_section(rows: Sequence[str]) -> list[str]:
    return ["[V4+ Styles]", "Format: " + ", ".join(STYLE_FORMAT_FIELDS), *rows]


def ass_colour(rgb: tuple[int, int, int]) -> str:
    """the `Style:` form, `&HAABBGGRR` with a zero alpha."""
    red, green, blue = rgb
    return f"&H00{blue:02X}{green:02X}{red:02X}"


def tag_colour(rgb: tuple[int, int, int]) -> str:
    """the override-tag form, `&HBBGGRR&`."""
    red, green, blue = rgb
    return f"&H{blue:02X}{green:02X}{red:02X}&"


def lyric_style_rows() -> list[str]:
    outline = ass_colour(layout.GLASS_RGB)
    return [
        style_row(
            STYLE_CURRENT,
            FONT_SIZE_CURRENT_PX,
            ass_colour(CURRENT_RGB),
            outline,
            LYRICS_OUTLINE_PX,
        ),
        style_row(
            STYLE_ADJACENT,
            FONT_SIZE_ADJACENT_PX,
            ass_colour(ADJACENT_RGB),
            outline,
            LYRICS_OUTLINE_PX,
        ),
    ]


def display_style_rows() -> list[str]:
    """the outline is set per event (the glow), so the style itself has none."""
    outline = ass_colour(layout.GLASS_RGB)
    return [
        style_row(
            STYLE_DISPLAY_TITLE,
            FONT_SIZE_DISPLAY_TITLE_PX,
            ass_colour(DISPLAY_TITLE_RGB),
            outline,
            0,
        ),
        style_row(
            STYLE_DISPLAY_ARTISTS,
            FONT_SIZE_DISPLAY_ARTISTS_PX,
            ass_colour(DISPLAY_ARTISTS_RGB),
            outline,
            0,
        ),
    ]


def style_row(
    name: str,
    font_size_px: int,
    primary_colour: str,
    outline_colour: str,
    outline_px: int,
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
        "Outline": outline_px,
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
        while not fits_text_width(remainder, font_size_px, USABLE_TEXT_WIDTH_PX):
            head = longest_prefix_within(remainder, font_size_px=font_size_px, available_px=USABLE_TEXT_WIDTH_PX)
            if current:
                wrapped.append(current)
                current = ""

            wrapped.append(head)
            remainder = remainder[len(head) :]

        candidate = f"{current} {remainder}".strip()
        if fits_text_width(candidate, font_size_px, USABLE_TEXT_WIDTH_PX):
            current = candidate
            continue

        if current:
            wrapped.append(current)
        current = remainder

    if current:
        wrapped.append(current)
    return wrapped or [""]


def fits_text_width(
    text: str,
    font_size_px: int,
    available_px: float,
) -> bool:
    return fits_within(text, font_size_px=font_size_px, available_px=available_px)


def with_ellipsis(
    line: str,
    font_size_px: int,
    available_px: float,
) -> str:
    while line and not fits_text_width(line + ELLIPSIS, font_size_px, available_px):
        line = line[:-1].rstrip()
    return line + ELLIPSIS
