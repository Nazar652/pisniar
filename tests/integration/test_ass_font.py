"""The ASS font guard. libass falls back silently: `fc-match` answers a missing family with
the same file as an installed one, so a wrong name shows up only as Cyrillic rectangles in
a video. Also pins `noto_sans_widths` to that file. Runs only in the worker image, where the
fonts live (`make test-fonts`); `fontTools` is a dev dependency the runtime never imports.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from pisniar.worker import ass, noto_sans_widths, text_metrics

pytestmark = [pytest.mark.integration, pytest.mark.fonts]


def families_covering_ukrainian() -> set[str]:
    executable = shutil.which("fc-list")
    assert executable is not None, "fc-list is missing — run this inside the worker container"

    listing = subprocess.run(
        [executable, ":lang=uk", "family"], capture_output=True, text=True, check=True
    )

    families: set[str] = set()
    for line in listing.stdout.splitlines():
        families.update(name.strip() for name in line.split(",") if name.strip())
    return families


def test_the_family_named_in_the_style_row_is_installed_and_covers_ukrainian() -> None:
    assert ass.FONT_FAMILY in families_covering_ukrainian()


def test_the_music_note_of_an_instrumental_break_comes_from_a_fallback_family() -> None:
    """Noto Sans lacks U+266A, so libass draws the note from a fallback Noto family."""
    executable = shutil.which("fc-list")
    assert executable is not None, "fc-list is missing — run this inside the worker container"

    listing = subprocess.run(
        [executable, ":charset=266a", "family"], capture_output=True, text=True, check=True
    )

    families = {name.strip() for line in listing.stdout.splitlines() for name in line.split(",")}
    assert families & {"Noto Sans Symbols", "Noto Music"}


def test_the_family_the_plan_originally_named_is_not_installed_at_all() -> None:
    """naming `Montserrat` would produce a correct-looking `.ass` and a video
    rendered in some other font."""
    assert "Montserrat" not in families_covering_ukrainian()


@pytest.fixture(scope="module")
def installed_font() -> TTFont:
    return TTFont(noto_sans_widths.SOURCE_FONT_FILE)


def table_metric(font: TTFont, table: str, attribute: str) -> int:
    """fontTools builds table attributes while parsing, so `ty` cannot see them;
    this is the one place that says so instead of scattered `type: ignore`."""
    value = getattr(font[table], attribute)
    assert isinstance(value, int)
    return value


def character_map_of(font: TTFont) -> dict[int, str]:
    character_map = font.getBestCmap()
    assert character_map is not None, "the font carries no usable cmap subtable"
    return character_map


def test_the_file_the_table_was_baked_from_is_the_file_fontconfig_resolves() -> None:
    """a width table pinned against the wrong file pins nothing."""
    executable = shutil.which("fc-match")
    assert executable is not None, "fc-match is missing — run this inside the worker container"

    resolved = subprocess.run(
        [executable, "-f", "%{file}", ass.FONT_FAMILY], capture_output=True, text=True, check=True
    )

    assert resolved.stdout.strip() == noto_sans_widths.SOURCE_FONT_FILE
    assert Path(noto_sans_widths.SOURCE_FONT_FILE).is_file()


def test_the_metrics_that_turn_a_font_unit_into_a_pixel_still_hold(
    installed_font: TTFont,
) -> None:
    """`FONT_SIZE_TO_EM_SCALE` is built out of these three; a font update
    that moved the ascender would silently rescale every line."""
    assert table_metric(installed_font, "head", "unitsPerEm") == noto_sans_widths.UNITS_PER_EM
    assert table_metric(installed_font, "hhea", "ascender") == noto_sans_widths.HHEA_ASCENDER
    assert table_metric(installed_font, "hhea", "descender") == noto_sans_widths.HHEA_DESCENDER


def test_every_baked_advance_width_matches_the_installed_font(installed_font: TTFont) -> None:
    character_map = character_map_of(installed_font)
    horizontal_metrics = installed_font["hmtx"]

    divergent = {
        codepoint: (baked, horizontal_metrics[character_map[codepoint]][0])
        for codepoint, baked in noto_sans_widths.ADVANCE_WIDTHS.items()
        if codepoint in character_map and horizontal_metrics[character_map[codepoint]][0] != baked
    }
    missing = [
        codepoint for codepoint in noto_sans_widths.ADVANCE_WIDTHS if codepoint not in character_map
    ]

    assert not divergent, "regenerate with scripts/generate_glyph_widths.py"
    assert not missing, "regenerate with scripts/generate_glyph_widths.py"


def test_the_fallback_is_not_narrower_than_anything_it_stands_in_for(
    installed_font: TTFont,
) -> None:
    """an under-estimate is the one error that puts text off the frame, so
    the fallback has to be at least a full em of this font."""
    assert table_metric(installed_font, "head", "unitsPerEm") <= (
        text_metrics.FALLBACK_ADVANCE_UNITS
    )
