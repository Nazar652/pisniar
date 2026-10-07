"""Per-glyph text measurement, read back against numbers measured on the worker
image: an em of Noto Sans renders at 0.734 of the ASS `Fontsize`, «Цей рядок
навмисно довгий,» is 460 px of advance at `Fontsize: 44`, and an unknown
codepoint is one full em wide.
"""

from __future__ import annotations

from pisniar.worker import noto_sans_widths, text_metrics

GATE_CALIBRATION_LINE = "Цей рядок навмисно довгий,"
# measured on the worker image: 456 px of ink against the 454.8 px this table
# predicts (antialiasing); the em-square reading of `Fontsize` predicts 619.4.
MEASURED_ADVANCE_PX_AT_44 = 460.4


def test_the_font_size_is_the_cell_height_and_not_the_em_square() -> None:
    """an average of 0.52 em cuts a line a third short, because libass renders
    the em at `Fontsize * unitsPerEm / (ascender - descender)`."""
    expected = noto_sans_widths.UNITS_PER_EM / (
        noto_sans_widths.HHEA_ASCENDER - noto_sans_widths.HHEA_DESCENDER
    )

    assert expected == text_metrics.FONT_SIZE_TO_EM_SCALE
    assert round(text_metrics.FONT_SIZE_TO_EM_SCALE, 3) == 0.734


def test_a_measured_line_matches_what_the_image_rendered() -> None:
    assert round(text_metrics.text_width_px(GATE_CALIBRATION_LINE, 44), 1) == (
        MEASURED_ADVANCE_PX_AT_44
    )


def test_the_empty_string_is_zero_wide() -> None:
    assert text_metrics.text_width_px("", 44) == 0


def test_width_scales_with_the_font_size() -> None:
    assert text_metrics.text_width_px("рядок", 88) == 2 * text_metrics.text_width_px("рядок", 44)


def test_a_known_codepoint_is_read_from_the_table_and_not_from_the_fallback() -> None:
    assert text_metrics.advance_units("і") == noto_sans_widths.ADVANCE_WIDTHS[ord("і")]
    assert text_metrics.advance_units("і") != text_metrics.FALLBACK_ADVANCE_UNITS


def test_an_unknown_codepoint_falls_back_to_one_full_em() -> None:
    """over-estimating is the safe direction: the line wraps early instead
    of running off the frame."""
    emoji = "\U0001f525"

    assert ord(emoji) not in noto_sans_widths.ADVANCE_WIDTHS
    assert text_metrics.advance_units(emoji) == text_metrics.FALLBACK_ADVANCE_UNITS
    assert text_metrics.FALLBACK_ADVANCE_UNITS == noto_sans_widths.UNITS_PER_EM


def test_the_fallback_is_wider_than_every_letter_the_table_covers() -> None:
    letters = "абвгдеєжзиіїйклмнопрстуфхцчшщьюяabcdefghijklmnopqrstuvwxyz0123456789"

    assert all(
        text_metrics.advance_units(letter) < text_metrics.FALLBACK_ADVANCE_UNITS
        for letter in letters
    )


def test_a_string_that_fits_is_reported_as_fitting() -> None:
    assert text_metrics.fits_within("рядок", font_size_px=44, available_px=640)
    assert not text_metrics.fits_within(GATE_CALIBRATION_LINE, font_size_px=44, available_px=400)


def test_the_boundary_is_inclusive() -> None:
    width = text_metrics.text_width_px("рядок", 44)

    assert text_metrics.fits_within("рядок", font_size_px=44, available_px=width)
    assert not text_metrics.fits_within("рядок", font_size_px=44, available_px=width - 0.01)


def test_the_longest_prefix_is_the_last_one_that_still_fits() -> None:
    prefix = text_metrics.longest_prefix_within(
        GATE_CALIBRATION_LINE, font_size_px=44, available_px=200
    )

    assert GATE_CALIBRATION_LINE.startswith(prefix)
    assert text_metrics.fits_within(prefix, font_size_px=44, available_px=200)
    assert not text_metrics.fits_within(
        GATE_CALIBRATION_LINE[: len(prefix) + 1], font_size_px=44, available_px=200
    )


def test_the_longest_prefix_of_a_string_that_fits_is_the_whole_string() -> None:
    assert text_metrics.longest_prefix_within("рядок", font_size_px=44, available_px=640) == "рядок"


def test_the_longest_prefix_is_never_empty_when_even_one_glyph_is_too_wide() -> None:
    """the wrapper loops on this answer, so an empty one would hang it."""
    assert text_metrics.longest_prefix_within("щщщ", font_size_px=44, available_px=1) == "щ"


def test_an_empty_string_has_an_empty_prefix() -> None:
    assert text_metrics.longest_prefix_within("", font_size_px=44, available_px=640) == ""
