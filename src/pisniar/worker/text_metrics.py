"""How wide a lyric line actually is, in the pixels libass will draw it at
(D-94).

The old answer was an average advance of 0.52 em against the ASS `Fontsize`,
which cut a line at 27 characters where ~38 fit — and the reason is a
convention, not a bad average: **libass reads `Fontsize` as the
ascender-to-descender cell height, not as the em square**. The em is
therefore rendered at `font_size_px * FONT_SIZE_TO_EM_SCALE` pixels, which
for this font is 0.734 of it.

Measured on the worker image before this module was written: the string
«Цей рядок навмисно довгий,» at `Fontsize: 44` renders 456 px of ink, and the
table below predicts 454.8 px for it. The em-square reading predicts 619.4 px.

No new runtime dependency: `noto_sans_widths` is a baked table
(`scripts/generate_glyph_widths.py`), pinned back against the font inside the
container by `tests/integration/test_ass_font.py`.
"""

from __future__ import annotations

from pisniar.worker.noto_sans_widths import (
    ADVANCE_WIDTHS,
    HHEA_ASCENDER,
    HHEA_DESCENDER,
    UNITS_PER_EM,
)

FONT_SIZE_TO_EM_SCALE = UNITS_PER_EM / (HHEA_ASCENDER - HHEA_DESCENDER)

# Song text is untrusted and may hold anything the table does not cover — an
# emoji, a CJK glyph, a Greek letter — and whatever fontconfig substitutes
# for it is not this font, so its real advance is unknowable from here. One
# full em is the width a full-width glyph conventionally takes and is wider
# than all but a handful of entries in the table, which makes an unknown
# codepoint **over**-estimated: the line wraps early instead of running off
# the frame. Never lower this to an average — that is the D-94 mistake again.
FALLBACK_ADVANCE_UNITS = UNITS_PER_EM


def advance_units(character: str) -> int:
    """One glyph's advance in font units — the single place the fallback is
    applied, so there is exactly one way an unknown codepoint is measured."""
    return ADVANCE_WIDTHS.get(ord(character), FALLBACK_ADVANCE_UNITS)


def text_width_px(text: str, font_size_px: int) -> float:
    """The advance width of `text`, summed glyph by glyph. Kerning is
    ignored, which errs wide — the one direction that cannot overflow."""
    units = sum(advance_units(character) for character in text)
    return units * font_size_px * FONT_SIZE_TO_EM_SCALE / UNITS_PER_EM


def fits_within(text: str, *, font_size_px: int, available_px: float) -> bool:
    return text_width_px(text, font_size_px) <= available_px


def longest_prefix_within(text: str, *, font_size_px: int, available_px: float) -> str:
    """The longest head of `text` that still fits, and **never** the empty
    string: a single glyph wider than the whole band still has to be drawn
    somewhere, and returning nothing would loop the wrapper forever.

    Accumulated in font units, not in pixels, so it agrees with
    `fits_within()` exactly rather than to within a float's patience."""
    if not text:
        return ""

    units = 0
    for index, character in enumerate(text):
        units += advance_units(character)
        if units * font_size_px * FONT_SIZE_TO_EM_SCALE / UNITS_PER_EM > available_px:
            return text[:index] or text[:1]

    return text
