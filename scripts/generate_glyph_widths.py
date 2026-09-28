"""Bakes `pisniar.worker.noto_sans_widths` from the font file in the worker image.
Copy it out first (`docker compose cp worker:<SOURCE_FONT_FILE> ./noto.ttf`), then
`uv run python scripts/generate_glyph_widths.py ./noto.ttf`. `fontTools` stays a dev
dependency; `tests/integration/test_ass_font.py` pins the table to the font.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from fontTools.ttLib import TTFont

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPOSITORY_ROOT / "src" / "pisniar" / "worker" / "noto_sans_widths.py"

# Song text is Ukrainian first, but artist names and inserts are not, so Latin
# and the European diacritics travel with it.
CODEPOINT_RANGES: tuple[tuple[int, int, str], ...] = (
    (
        0x0020,
        0x007E,
        "ASCII: Latin letters, digits, the straight apostrophe and quote, punctuation",
    ),
    (0x00A0, 0x00FF, "Latin-1: accented Latin, the guillemets, the acute accent, degree"),
    (0x0100, 0x017F, "Latin Extended-A: the Central-European diacritics in artist names"),
    (
        0x02B9,
        0x02BC,
        "modifier letters: U+02BC, the apostrophe Ukrainian orthography calls for",
    ),
    (0x0400, 0x04FF, "Cyrillic: Ukrainian (U+0456, U+0457, U+0454, U+0491) through Belarusian"),
    (0x2010, 0x2027, "dashes, both curly quote shapes, the bullet, the ellipsis"),
    (0x2030, 0x203A, "per mille, the primes, the single guillemets"),
    (0x20A0, 0x20BF, "currency: hryvnia, euro, rouble"),
    (0x2116, 0x2116, "the numero sign, still common in Cyrillic typography"),
    (0x2122, 0x2122, "the trademark sign"),
)

ENTRIES_PER_LINE = 6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font_path", type=Path, help="NotoSans-Regular.ttf copied out of the image")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    arguments = parser.parse_args()

    font = TTFont(arguments.font_path)
    arguments.output.write_text(render_module(font, arguments.font_path), encoding="utf-8")

    print(f"wrote {arguments.output}")


def render_module(font: TTFont, font_path: Path) -> str:
    character_map = font.getBestCmap()
    horizontal_metrics = font["hmtx"]

    blocks: list[str] = []
    covered = 0
    for first, last, description in CODEPOINT_RANGES:
        rows: list[str] = []
        pairs = [
            (codepoint, horizontal_metrics[character_map[codepoint]][0])
            for codepoint in range(first, last + 1)
            if codepoint in character_map
        ]
        covered += len(pairs)

        for index in range(0, len(pairs), ENTRIES_PER_LINE):
            chunk = pairs[index : index + ENTRIES_PER_LINE]
            rows.append("    " + " ".join(f"0x{point:04X}: {width}," for point, width in chunk))

        blocks.append(f"    # {description}\n" + "\n".join(rows))

    header = _header(font, font_path, covered)
    table = "\n".join(blocks)

    # `# fmt: off` keeps `ruff format` from exploding 648 entries into 648
    # lines — the table is read as columns of numbers or not at all.
    return f"{header}\n# fmt: off\nADVANCE_WIDTHS: dict[int, int] = {{\n{table}\n}}\n# fmt: on\n"


def _header(font: TTFont, font_path: Path, covered: int) -> str:
    head = font["head"]
    horizontal_header = font["hhea"]
    generated_on = dt.datetime.now(tz=dt.UTC).date().isoformat()

    return f'''"""Advance widths of `{font_path.name}` in font units, baked by
`scripts/generate_glyph_widths.py` on {generated_on}. **Generated — do not edit
by hand.** {covered} codepoints from `CODEPOINT_RANGES`; any other codepoint
is measured as `text_metrics.FALLBACK_ADVANCE_UNITS`.
"""

from __future__ import annotations

SOURCE_FONT_FILE = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"

UNITS_PER_EM = {head.unitsPerEm}

# libass reads `Fontsize` as the ascender-to-descender cell height, so these two
# turn a font unit into a pixel (`text_metrics.FONT_SIZE_TO_EM_SCALE`).
HHEA_ASCENDER = {horizontal_header.ascender}
HHEA_DESCENDER = {horizontal_header.descender}

'''


if __name__ == "__main__":
    main()
