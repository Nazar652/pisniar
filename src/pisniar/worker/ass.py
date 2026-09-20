"""The `.ass` generator: a pure function from stored lyric lines to the text
of a subtitle file. No filesystem, no database, no ffmpeg.

Song text is untrusted input (§4 p.9). ASS has **no escape sequences inside a
line**, so the only reliable defence is substitution: `{`/`}` become `(`/`)`,
which disables override blocks as a class, and `\\` is dropped, which is what
makes `\\N`/`\\h`/`\\an7` impossible to spell. The gate rendered
`{\\an7\\fs90\\c&H0000FF&}` to actual red 90 px pixels outside its slot, so
this is a demonstrated attack, not a theoretical one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pisniar.logging import get_logger
from pisniar.post.sanitize import sanitize_metadata_text
from pisniar.worker.text_metrics import fits_within, longest_prefix_within

logger = get_logger(__name__)

# §4 p.4: without these libass assumes a 384x288 script and scales every
# coordinate below by the wrong factor. The subtitles are drawn onto the
# finished 720x1280 frame, so the script resolution *is* the frame.
PLAY_RES_X = 720
PLAY_RES_Y = 1280

# D-90/gate 4B.0.2: the only family proven to carry Ukrainian in the worker
# image. A name that is not installed falls back byte-for-byte silently
# (`fc-match 'Montserrat'` and `fc-match 'NoSuchFontFamily12345'` are
# identical), which is why `tests/integration/test_ass_font.py` exists.
FONT_FAMILY = "Noto Sans"

# D-92: the cover is pushed up and the bottom band belongs to the text, so a
# long line overflows into black rather than onto the artist's face. The
# other half of this number is `render.COVER_TOP_OFFSET_PX`; the two are
# pinned against each other in `tests/unit/worker/test_ass.py`.
TEXT_BAND_TOP_PX = 820

# Three slots (D-85), with the reduced leading D-92 asked for and D-93
# tightened again by eye on a rendered frame. `Alignment=5` plus an explicit
# `\pos` makes the symmetry a constant instead of an accident of `MarginV`,
# which `\an5` ignores anyway (§4 p.2, p.3).
SLOT_CENTER_X = PLAY_RES_X // 2
CURRENT_SLOT_Y = 1050
# 110 -> 95 (D-93). The tightest case is a current line that wraps to two,
# and `tests/unit/worker/test_ass.py` measures that block against its
# smaller neighbours rather than trusting the number.
SLOT_SPACING_PX = 95
PREVIOUS_SLOT_Y = CURRENT_SLOT_Y - SLOT_SPACING_PX
NEXT_SLOT_Y = CURRENT_SLOT_Y + SLOT_SPACING_PX

FONT_SIZE_CURRENT_PX = 44
FONT_SIZE_ADJACENT_PX = 32
HORIZONTAL_MARGIN_PX = 40

# `WrapStyle: 2` turns libass' own wrapping off, so the break points are
# ours — measured against the real advance widths of the image's own font
# (`text_metrics`, D-94) instead of against an average that stopped a line a
# third of the band short of its edge.
USABLE_TEXT_WIDTH_PX = PLAY_RES_X - 2 * HORIZONTAL_MARGIN_PX
MAX_CURRENT_WRAPPED_LINES = 2
# One, not two: a second wrapped line in a side slot is exactly how a long
# line climbs into the neighbouring one.
MAX_ADJACENT_WRAPPED_LINES = 1
ELLIPSIS = "…"

# §4 p.6: without a ceiling the last line of a fragment hangs for the rest of
# it, and a 60-second gap in the source hangs for a minute.
MAX_LINE_MS = 8_000
# A 45-second fragment with more than this many lines is a broken LRC or an
# attempt to spend ffmpeg's CPU, not a song (§5).
MAX_RENDERED_LINES = 120
# Mirrors `models.MAX_LYRIC_TEXT_LENGTH`: past it the wrapper would be
# word-splitting a payload, not a lyric.
MAX_SOURCE_TEXT_CHARACTERS = 500

STYLE_CURRENT = "current"
STYLE_ADJACENT = "adjacent"

# The `Format:` line each `Style:` row is positional against (§4 p.1). Rows
# are emitted by looking every field up in this order, so a row can never
# drift out of step with the header or fall back to `key=value`.
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

# `\an5` centres on the `\pos` point and ignores `MarginV` entirely (§4 p.2),
# so the vertical margin is honestly zero here.
ALIGNMENT_MIDDLE_CENTER = 5

_ASS_SUBSTITUTIONS = str.maketrans({"{": "(", "}": ")", "\\": None})


class AssGenerationError(ValueError):
    """Our own failure, never the source's — it fails a post loudly (§4)."""


@dataclass(frozen=True)
class LyricLine:
    """One stored line. `start_ms` is absolute from the start of the *full*
    track (D-5) and typed optional on purpose: step 7 drops the column's
    NOT NULL, and this signature does not move when it does.
    """

    line_index: int
    start_ms: int | None
    text: str


@dataclass(frozen=True)
class _PlacedLine:
    """A line already shifted into fragment time and already escaped."""

    start_ms: int
    text: str


def escape_ass_text(value: str) -> str:
    """Substitution, not escaping. `sanitize_metadata_text` (`post.sanitize`,
    not a copy of it) folds line breaks and drops Cc/Cf — U+202E, U+200B and
    a raw newline among them; the translation table then removes the three
    characters that make override blocks spellable."""
    substituted = sanitize_metadata_text(value).translate(_ASS_SUBSTITUTIONS)
    return " ".join(substituted.split())


def wrap_for_slot(text: str, *, font_size_px: int, max_lines: int) -> str:
    """Our own break points, joined with a literal `\\N` — which is only
    spellable here because `escape_ass_text` already removed every backslash
    the source could have contributed."""
    wrapped = _wrap_words(text[:MAX_SOURCE_TEXT_CHARACTERS], font_size_px)

    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = _with_ellipsis(wrapped[-1], font_size_px)

    return "\\N".join(wrapped)


def build_ass_subtitles(
    lines: Sequence[LyricLine], *, fragment_start_ms: int, fragment_end_ms: int
) -> str | None:
    """The whole generator. Returns `None` when the fragment contains nothing
    to draw — that is the input `text_version=None` is computed from, and it
    is what makes «no text» a state rather than an empty file."""
    if fragment_end_ms <= fragment_start_ms:
        raise AssGenerationError(
            f"межі фрагмента невалідні: [{fragment_start_ms}, {fragment_end_ms})"
        )

    fragment_length_ms = fragment_end_ms - fragment_start_ms
    placed = _place_lines(lines, fragment_start_ms)
    events = _build_events(placed, fragment_length_ms)

    if not events:
        logger.info("ass_nothing_to_render", line_count=len(lines))
        return None

    logger.info("ass_generated", line_count=len(lines), event_count=len(events))
    return "\n".join([*_script_info(), "", *_styles(), "", *_events(events)]) + "\n"


def _place_lines(lines: Sequence[LyricLine], fragment_start_ms: int) -> list[_PlacedLine]:
    """Seams §2.1.1 and §2.1.2, in the order they matter: drop what has no
    timecode, then order by `line_index` and **never** by `start_ms`. Sorting
    by time would leave step 7's untimed lines nowhere to stand."""
    timed = [line for line in lines if line.start_ms is not None]
    ordered = sorted(timed, key=lambda line: line.line_index)

    return [
        _PlacedLine(
            # `ass_t = lyric.start_ms - post.fragment_start_ms` (D-5), the one
            # place absolute milliseconds become fragment-local ones.
            start_ms=_absolute_start_ms(line) - fragment_start_ms,
            text=escape_ass_text(line.text),
        )
        for line in ordered
    ]


def _absolute_start_ms(line: LyricLine) -> int:
    if line.start_ms is None:  # pragma: no cover - filtered out one frame up
        raise AssGenerationError("рядок без таймкоду не доходить сюди")
    return line.start_ms


def _build_events(placed: Sequence[_PlacedLine], fragment_length_ms: int) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    rendered_count = 0

    for index, line in enumerate(placed):
        if not line.text:
            continue

        window = _window(placed, index, fragment_length_ms)
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
        events.extend(_slot_events(placed, index, start_ms, end_ms))
        rendered_count += 1

    return events


def _window(
    placed: Sequence[_PlacedLine], index: int, fragment_length_ms: int
) -> tuple[int, int] | None:
    """§4 p.6: `end = min(next line, start + MAX_LINE_MS, fragment end)`, then
    clipped to the fragment. A pause entry counts as the next line, which is
    exactly what ends a verse."""
    start_ms = placed[index].start_ms
    end_ms = min(start_ms + MAX_LINE_MS, fragment_length_ms)

    if index + 1 < len(placed):
        end_ms = min(end_ms, placed[index + 1].start_ms)

    start_ms = max(start_ms, 0)
    if end_ms <= start_ms:
        return None
    return start_ms, end_ms


def _slot_events(
    placed: Sequence[_PlacedLine], index: int, start_ms: int, end_ms: int
) -> list[dict[str, str]]:
    """Three slots (D-85): the neighbours are the nearest lines that draw
    something, so a pause between verses never leaves a blank slot."""
    previous = _neighbour_text(placed, index, step=-1)
    following = _neighbour_text(placed, index, step=1)

    slots = [
        (previous, PREVIOUS_SLOT_Y, STYLE_ADJACENT, FONT_SIZE_ADJACENT_PX),
        (placed[index].text, CURRENT_SLOT_Y, STYLE_CURRENT, FONT_SIZE_CURRENT_PX),
        (following, NEXT_SLOT_Y, STYLE_ADJACENT, FONT_SIZE_ADJACENT_PX),
    ]

    events: list[dict[str, str]] = []
    for text, slot_y, style, font_size_px in slots:
        if not text:
            continue

        max_lines = (
            MAX_CURRENT_WRAPPED_LINES if style == STYLE_CURRENT else MAX_ADJACENT_WRAPPED_LINES
        )
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


def _neighbour_text(placed: Sequence[_PlacedLine], index: int, *, step: int) -> str:
    position = index + step
    while 0 <= position < len(placed):
        if placed[position].text:
            return placed[position].text
        position += step
    return ""


def ass_timestamp(milliseconds: int) -> str:
    """`H:MM:SS.CC` — ASS keeps centiseconds, so the tail is truncated, never
    rounded up past the next line's start."""
    total_centiseconds, _ = divmod(max(milliseconds, 0), 10)
    total_seconds, centiseconds = divmod(total_centiseconds, 100)
    total_minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _script_info() -> list[str]:
    return [
        "[Script Info]",
        "; Generated by pisniar — regenerated on every render, never edited.",
        "ScriptType: v4.00+",
        f"PlayResX: {PLAY_RES_X}",
        f"PlayResY: {PLAY_RES_Y}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
    ]


def _styles() -> list[str]:
    return [
        "[V4+ Styles]",
        "Format: " + ", ".join(STYLE_FORMAT_FIELDS),
        _style_row(STYLE_CURRENT, FONT_SIZE_CURRENT_PX, "&H00FFFFFF", "&H00101010"),
        _style_row(STYLE_ADJACENT, FONT_SIZE_ADJACENT_PX, "&H80FFFFFF", "&H80101010"),
    ]


def _style_row(name: str, font_size_px: int, primary_colour: str, outline_colour: str) -> str:
    """Positional by construction: the values are looked up in
    `STYLE_FORMAT_FIELDS` order, so a forgotten field is a `KeyError` here
    rather than a style libass silently ignores (§4 p.1)."""
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


def _events(events: Sequence[dict[str, str]]) -> list[str]:
    return [
        "[Events]",
        "Format: " + ", ".join(EVENT_FORMAT_FIELDS),
        *(_event_row(event) for event in events),
    ]


def _event_row(event: dict[str, str]) -> str:
    """`Text` is the last field, which is the only reason a comma inside the
    lyrics is harmless — pinned by a test rather than by memory (§5)."""
    return "Dialogue: " + ",".join(event[field] for field in EVENT_FORMAT_FIELDS)


def _wrap_words(text: str, font_size_px: int) -> list[str]:
    wrapped: list[str] = []
    current = ""

    for word in text.split():
        remainder = word
        while not _fits(remainder, font_size_px):
            head = longest_prefix_within(
                remainder, font_size_px=font_size_px, available_px=USABLE_TEXT_WIDTH_PX
            )
            if current:
                wrapped.append(current)
                current = ""

            wrapped.append(head)
            remainder = remainder[len(head) :]

        candidate = f"{current} {remainder}".strip()
        if _fits(candidate, font_size_px):
            current = candidate
            continue

        if current:
            wrapped.append(current)
        current = remainder

    if current:
        wrapped.append(current)
    return wrapped or [""]


def _fits(text: str, font_size_px: int) -> bool:
    return fits_within(text, font_size_px=font_size_px, available_px=USABLE_TEXT_WIDTH_PX)


def _with_ellipsis(line: str, font_size_px: int) -> str:
    """The ellipsis has to fit the same band the text did, so the tail is
    given back one character at a time until it does."""
    while line and not _fits(line + ELLIPSIS, font_size_px):
        line = line[:-1].rstrip()
    return line + ELLIPSIS
