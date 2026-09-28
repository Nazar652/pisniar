"""Lyric line width in the pixels libass draws. libass reads `Fontsize` as the
ascender-to-descender cell height, not the em square. The widths are a baked
table, pinned to the container font by `tests/integration/test_ass_font.py`.
"""

from __future__ import annotations

from pisniar.worker.noto_sans_widths import (
    ADVANCE_WIDTHS,
    HHEA_ASCENDER,
    HHEA_DESCENDER,
    UNITS_PER_EM,
)

FONT_SIZE_TO_EM_SCALE = UNITS_PER_EM / (HHEA_ASCENDER - HHEA_DESCENDER)

# An unknown codepoint renders in a substitute font; one full em errs wide, so
# the line wraps early instead of leaving the frame. Never lower to an average.
FALLBACK_ADVANCE_UNITS = UNITS_PER_EM


def advance_units(character: str) -> int:
    return ADVANCE_WIDTHS.get(ord(character), FALLBACK_ADVANCE_UNITS)


def text_width_px(text: str, font_size_px: int) -> float:
    """Kerning is ignored: that errs wide, the one direction that cannot overflow."""
    units = sum(advance_units(character) for character in text)
    return units * font_size_px * FONT_SIZE_TO_EM_SCALE / UNITS_PER_EM


def fits_within(text: str, *, font_size_px: int, available_px: float) -> bool:
    return text_width_px(text, font_size_px) <= available_px


def longest_prefix_within(text: str, *, font_size_px: int, available_px: float) -> str:
    """Never empty: a glyph wider than the band must still be drawn, or the wrapper
    loops forever. The formula matches `fits_within()` so the two agree exactly."""
    if not text:
        return ""

    units = 0
    for index, character in enumerate(text):
        units += advance_units(character)
        if units * font_size_px * FONT_SIZE_TO_EM_SCALE / UNITS_PER_EM > available_px:
            return text[:index] or text[:1]

    return text
