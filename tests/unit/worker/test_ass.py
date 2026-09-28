"""The `.ass` generator, read back the way libass reads it: positional
`Style:` rows against their own `Format:`, an explicit `PlayRes`, three slots
placed by `\\pos`, the shift into fragment time, the line boundaries, and the
substitution that makes an override block unspellable.
"""

from __future__ import annotations

import pytest

from pisniar.worker import ass, noto_sans_widths, text_metrics
from pisniar.worker import render as video_render

FRAGMENT_START_MS = 0
FRAGMENT_END_MS = 45_000

# Only used to assert the slots do not overlap: libass draws a line roughly
# 1.2x its font size tall, so this is the pessimistic block height.
LINE_HEIGHT_RATIO = 1.2

# Measured on the worker image: libass stacks lines one `Fontsize` apart, so a
# two-line 44 px block inks 75 px where this model claims 88, the safe direction.
MEASURED_LINE_HEIGHT_RATIO = 1.0

# A 123-character line, too long for one line of the band.
LONG_LINE_OF_123_CHARACTERS = (
    "Цей рядок навмисно довгий, бо гейт показав, що стовідсотково "
    "не влізе в один рядок і його доведеться таки розбивати надвоє."
)
# 640 usable px / (44 px * 0.52 em): the budget of an average glyph width.
CHARACTERS_AT_AN_AVERAGE_GLYPH_WIDTH = 27


def lines_of(*pairs: tuple[int, str]) -> list[ass.LyricLine]:
    return [
        ass.LyricLine(line_index=index, start_ms=start_ms, text=text)
        for index, (start_ms, text) in enumerate(pairs)
    ]


def build(
    lines: list[ass.LyricLine],
    *,
    fragment_start_ms: int = FRAGMENT_START_MS,
    fragment_end_ms: int = FRAGMENT_END_MS,
) -> str:
    document = ass.build_ass_subtitles(
        lines, fragment_start_ms=fragment_start_ms, fragment_end_ms=fragment_end_ms
    )
    assert document is not None
    return document


def style_rows(document: str) -> list[str]:
    return [line for line in document.splitlines() if line.startswith("Style: ")]


def dialogue_rows(document: str) -> list[str]:
    return [line for line in document.splitlines() if line.startswith("Dialogue: ")]


def dialogue_fields(row: str) -> list[str]:
    return row.removeprefix("Dialogue: ").split(",", len(ass.EVENT_FORMAT_FIELDS) - 1)


def field_of(row: str, name: str) -> str:
    return dialogue_fields(row)[ass.EVENT_FORMAT_FIELDS.index(name)]


def current_rows(document: str) -> list[str]:
    return [row for row in dialogue_rows(document) if field_of(row, "Style") == ass.STYLE_CURRENT]


def test_the_script_declares_the_frame_it_is_drawn_onto() -> None:
    """Without `PlayResX/Y` libass assumes 384x288 and every coordinate below
    lands somewhere else."""
    document = build(lines_of((1_000, "рядок")))

    assert "PlayResX: 720" in document
    assert "PlayResY: 1280" in document
    assert "ScriptType: v4.00+" in document
    assert "WrapStyle: 2" in document


def test_every_style_row_is_positional_against_its_own_format_line() -> None:
    """libass silently ignores `Alignment=5` written as `key=value`, so the row
    is read back and counted instead."""
    document = build(lines_of((1_000, "рядок")))
    declared_fields = document.split("Format: ", 1)[1].split("\n", 1)[0].split(", ")

    assert declared_fields == list(ass.STYLE_FORMAT_FIELDS)
    for row in style_rows(document):
        values = row.removeprefix("Style: ").split(",")
        assert len(values) == len(ass.STYLE_FORMAT_FIELDS)
        assert "=" not in row


def test_alignment_is_a_number_and_the_vertical_margin_is_honestly_zero() -> None:
    """`\\an5` ignores `MarginV`, so a non-zero one would be a lie about where
    the line is."""
    document = build(lines_of((1_000, "рядок")))

    for row in style_rows(document):
        values = row.removeprefix("Style: ").split(",")
        assert values[ass.STYLE_FORMAT_FIELDS.index("Alignment")] == "5"
        assert values[ass.STYLE_FORMAT_FIELDS.index("MarginV")] == "0"


def test_the_font_family_is_the_one_the_gate_proved_carries_cyrillic() -> None:
    document = build(lines_of((1_000, "рядок")))

    assert ass.FONT_FAMILY == "Noto Sans"
    for row in style_rows(document):
        values = row.removeprefix("Style: ").split(",")
        assert values[ass.STYLE_FORMAT_FIELDS.index("Fontname")] == "Noto Sans"


def test_every_dialogue_row_has_exactly_the_declared_number_of_fields() -> None:
    document = build(lines_of((1_000, "рядок"), (3_000, "другий")))
    declared_fields = document.split("[Events]\n", 1)[1].split("\n", 1)[0]

    assert declared_fields == "Format: " + ", ".join(ass.EVENT_FORMAT_FIELDS)
    for row in dialogue_rows(document):
        assert len(dialogue_fields(row)) == len(ass.EVENT_FORMAT_FIELDS)


def test_a_comma_in_the_lyrics_is_harmless_because_text_is_the_last_field() -> None:
    """True today, and pinned here rather than remembered."""
    document = build(lines_of((1_000, "так, ось, кома")))

    assert ass.EVENT_FORMAT_FIELDS[-1] == "Text"
    assert field_of(current_rows(document)[0], "Text").endswith("так, ось, кома")


def test_each_line_is_placed_by_an_explicit_position_from_the_constants() -> None:
    """Slot symmetry is a constant, not an accident of margins."""
    document = build(lines_of((1_000, "перший"), (3_000, "другий"), (5_000, "третій")))
    middle = [row for row in dialogue_rows(document) if field_of(row, "Start") == "0:00:03.00"]

    positions = {field_of(row, "Text").split("}", 1)[0] + "}" for row in middle}
    assert positions == {
        f"{{\\pos(360,{ass.PREVIOUS_SLOT_Y})}}",
        f"{{\\pos(360,{ass.CURRENT_SLOT_Y})}}",
        f"{{\\pos(360,{ass.NEXT_SLOT_Y})}}",
    }


def test_three_slots_are_drawn_around_the_current_line() -> None:
    document = build(lines_of((1_000, "перший"), (3_000, "другий"), (5_000, "третій")))
    middle = [row for row in dialogue_rows(document) if field_of(row, "Start") == "0:00:03.00"]

    assert len(middle) == 3


def test_the_first_and_last_lines_have_one_neighbour_each() -> None:
    document = build(lines_of((1_000, "перший"), (3_000, "другий")))
    opening = [row for row in dialogue_rows(document) if field_of(row, "Start") == "0:00:01.00"]

    assert len(opening) == 2


def test_the_text_band_starts_where_the_lifted_cover_ends() -> None:
    """The `pad` offset and the slot geometry are one decision in two files, so
    they are pinned against each other."""
    assert ass.TEXT_BAND_TOP_PX == video_render.COVER_TOP_OFFSET_PX + video_render.VIDEO_WIDTH


def test_no_slot_reaches_into_its_neighbour_or_out_of_the_frame() -> None:
    current_half_height = (
        ass.MAX_CURRENT_WRAPPED_LINES * ass.FONT_SIZE_CURRENT_PX * (LINE_HEIGHT_RATIO / 2)
    )
    adjacent_half_height = (
        ass.MAX_ADJACENT_WRAPPED_LINES * ass.FONT_SIZE_ADJACENT_PX * (LINE_HEIGHT_RATIO / 2)
    )

    assert ass.PREVIOUS_SLOT_Y - adjacent_half_height > ass.TEXT_BAND_TOP_PX
    assert ass.PREVIOUS_SLOT_Y + adjacent_half_height < ass.CURRENT_SLOT_Y - current_half_height
    assert ass.CURRENT_SLOT_Y + current_half_height < ass.NEXT_SLOT_Y - adjacent_half_height
    assert ass.NEXT_SLOT_Y + adjacent_half_height < ass.PLAY_RES_Y


def block_extent(slot_y: int, *, wrapped_lines: int, font_size_px: int) -> tuple[float, float]:
    half_height = wrapped_lines * font_size_px * MEASURED_LINE_HEIGHT_RATIO / 2
    return slot_y - half_height, slot_y + half_height


def test_a_current_line_that_wraps_to_two_still_clears_its_smaller_neighbours() -> None:
    """The tightest case: the current block doubles while the side slots stay one
    smaller line; 35 px of black is left on each side, 23 px under the 1.2x reading."""
    current_top, current_bottom = block_extent(
        ass.CURRENT_SLOT_Y,
        wrapped_lines=ass.MAX_CURRENT_WRAPPED_LINES,
        font_size_px=ass.FONT_SIZE_CURRENT_PX,
    )
    previous_top, previous_bottom = block_extent(
        ass.PREVIOUS_SLOT_Y,
        wrapped_lines=ass.MAX_ADJACENT_WRAPPED_LINES,
        font_size_px=ass.FONT_SIZE_ADJACENT_PX,
    )
    next_top, next_bottom = block_extent(
        ass.NEXT_SLOT_Y,
        wrapped_lines=ass.MAX_ADJACENT_WRAPPED_LINES,
        font_size_px=ass.FONT_SIZE_ADJACENT_PX,
    )

    assert current_top - previous_bottom > 0
    assert next_top - current_bottom > 0
    assert previous_top > ass.TEXT_BAND_TOP_PX
    assert next_bottom < ass.PLAY_RES_Y


def test_the_two_line_case_is_the_one_the_frame_has_to_show() -> None:
    """The clearance above matters only if the wrapper can still produce a
    two-line current block at the measured glyph widths."""
    document = build(lines_of((1_000, LONG_LINE_OF_123_CHARACTERS)))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    assert body.count("\\N") == 1


def test_an_absolute_start_is_shifted_into_fragment_time() -> None:
    document = build(lines_of((32_000, "рядок")), fragment_start_ms=30_000, fragment_end_ms=75_000)

    assert field_of(current_rows(document)[0], "Start") == "0:00:02.00"


def test_a_line_before_the_fragment_never_appears() -> None:
    document = build(
        lines_of((5_000, "до фрагмента"), (32_000, "у фрагменті")),
        fragment_start_ms=30_000,
        fragment_end_ms=75_000,
    )

    assert [field_of(row, "Text").split("}", 1)[1] for row in current_rows(document)] == [
        "у фрагменті"
    ]


def test_a_line_after_the_fragment_never_appears() -> None:
    document = build(lines_of((1_000, "у фрагменті"), (50_000, "після фрагмента")))

    assert [field_of(row, "Text").split("}", 1)[1] for row in current_rows(document)] == [
        "у фрагменті"
    ]


def test_a_line_crossing_the_far_edge_is_cut_at_it() -> None:
    document = build(lines_of((44_000, "останній")))

    assert field_of(current_rows(document)[0], "End") == "0:00:45.00"


def test_a_line_ends_when_the_next_one_starts() -> None:
    document = build(lines_of((1_000, "перший"), (2_500, "другий")))

    assert field_of(current_rows(document)[0], "End") == "0:00:02.50"


def test_a_long_gap_is_capped_instead_of_hanging_for_a_minute() -> None:
    document = build(
        lines_of((1_000, "перший"), (61_000, "другий")),
        fragment_start_ms=0,
        fragment_end_ms=90_000,
    )

    assert field_of(current_rows(document)[0], "End") == "0:00:09.00"


def test_a_pause_entry_ends_the_line_before_it_and_draws_nothing() -> None:
    document = build(lines_of((1_000, "рядок"), (3_000, ""), (10_000, "наступний")))

    assert field_of(current_rows(document)[0], "End") == "0:00:03.00"
    assert len(current_rows(document)) == 2


def test_two_lines_sharing_a_start_do_not_produce_a_zero_length_event() -> None:
    document = build(lines_of((1_000, "перший"), (1_000, "другий")))

    assert [field_of(row, "Start") for row in current_rows(document)] == ["0:00:01.00"]


def test_a_fragment_with_no_lines_in_it_produces_no_file_at_all() -> None:
    """`None`, not an empty document: that is what `text_version=None` is
    computed from, and what keeps «no text» a state instead of a blank."""
    assert (
        ass.build_ass_subtitles(
            lines_of((50_000, "після")), fragment_start_ms=0, fragment_end_ms=45_000
        )
        is None
    )


def test_inverted_fragment_bounds_are_our_own_error_and_are_loud() -> None:
    with pytest.raises(ass.AssGenerationError):
        ass.build_ass_subtitles(
            lines_of((1_000, "рядок")), fragment_start_ms=45_000, fragment_end_ms=45_000
        )


def test_the_order_of_the_frames_is_the_stored_line_index_never_the_timecode() -> None:
    """Untimed lines will have only `line_index` to stand on, so a source's
    out-of-order timecodes must not reorder the lines."""
    lines = [
        ass.LyricLine(line_index=0, start_ms=5_000, text="перший"),
        ass.LyricLine(line_index=1, start_ms=1_000, text="другий"),
    ]

    placed = ass._place_lines(lines, 0)

    assert [line.text for line in placed] == ["перший", "другий"]


def test_a_source_that_timed_its_lines_out_of_order_is_not_resequenced() -> None:
    """Ordering by time would quietly repair the source and draw both lines,
    which is a different song's order."""
    lines = [
        ass.LyricLine(line_index=0, start_ms=5_000, text="перший"),
        ass.LyricLine(line_index=1, start_ms=1_000, text="другий"),
    ]

    document = build(lines)
    texts = [field_of(row, "Text").split("}", 1)[1] for row in current_rows(document)]

    assert texts == ["другий"]


def test_a_line_without_a_timecode_is_kept_out_of_the_frame() -> None:
    """The predicate is spelled out while it is still a tautology, so dropping
    NOT NULL on `start_ms` later changes nothing here."""
    lines = [
        ass.LyricLine(line_index=0, start_ms=None, text="без таймкоду"),
        ass.LyricLine(line_index=1, start_ms=1_000, text="із таймкодом"),
    ]

    document = build(lines)
    texts = [field_of(row, "Text").split("}", 1)[1] for row in current_rows(document)]

    assert texts == ["із таймкодом"]
    assert "без таймкоду" not in document


def test_an_untimed_line_is_not_even_a_neighbour_in_a_side_slot() -> None:
    lines = [
        ass.LyricLine(line_index=0, start_ms=None, text="без таймкоду"),
        ass.LyricLine(line_index=1, start_ms=1_000, text="із таймкодом"),
    ]

    assert len(dialogue_rows(build(lines))) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{\\an7\\fs90\\c&H0000FF&}червоний", "(an7fs90c&H0000FF&)червоний"),
        ("рядок\\Nдругий", "рядокNдругий"),
        ("твердий\\hпробіл", "твердийhпробіл"),
        ("зворотний\\слеш", "зворотнийслеш"),
        ("перенос\nрядка", "перенос рядка"),
        ("‮перевернуто", "перевернуто"),
        ("нуль​ширини", "нульширини"),
    ],
)
def test_the_characters_that_make_an_override_block_are_substituted(
    raw: str, expected: str
) -> None:
    """ASS has no escapes inside a line, so this is substitution, not escaping;
    unsubstituted, the first case draws red 90 px pixels outside its slot."""
    assert ass.escape_ass_text(raw) == expected


def test_no_override_block_survives_into_a_dialogue_body() -> None:
    document = build(lines_of((1_000, "{\\an7\\fs90\\c&H0000FF&}червоний")))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    assert "{" not in body
    assert "}" not in body
    assert "\\" not in body


def test_our_own_position_tag_is_still_the_only_brace_in_the_row() -> None:
    document = build(lines_of((1_000, "{\\pos(0,0)}підміна")))
    text = field_of(current_rows(document)[0], "Text")

    assert text.count("{") == 1
    assert text.startswith(f"{{\\pos(360,{ass.CURRENT_SLOT_Y})}}")


def test_a_line_that_is_only_control_characters_becomes_a_pause() -> None:
    document = build(lines_of((1_000, "рядок"), (3_000, "\\\\​"), (5_000, "третій")))

    assert len(current_rows(document)) == 2


def test_a_long_line_is_wrapped_by_us_and_never_by_libass() -> None:
    """Left to libass, a 123-character line runs off both edges of the frame;
    `WrapStyle: 2` plus our own measurement is what stops it."""
    long_line = "дуже довгий рядок тексту пісні " * 5

    document = build(lines_of((1_000, long_line)))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    assert body.count("\\N") == ass.MAX_CURRENT_WRAPPED_LINES - 1
    assert body.endswith(ass.ELLIPSIS)
    for visual_line in body.split("\\N"):
        assert (
            text_metrics.text_width_px(visual_line, ass.FONT_SIZE_CURRENT_PX)
            <= ass.USABLE_TEXT_WIDTH_PX
        )


def test_a_wrapped_line_uses_the_width_the_band_actually_has() -> None:
    """Counted in characters on purpose: measuring the result with the function
    that produced it would pass however wrong that function got."""
    assert len(LONG_LINE_OF_123_CHARACTERS) == 123

    document = build(lines_of((1_000, LONG_LINE_OF_123_CHARACTERS)))
    first_visual_line = field_of(current_rows(document)[0], "Text").split("}", 1)[1].split("\\N")[0]

    assert len(first_visual_line) > CHARACTERS_AT_AN_AVERAGE_GLYPH_WIDTH
    assert (
        text_metrics.text_width_px(first_visual_line, ass.FONT_SIZE_CURRENT_PX)
        <= ass.USABLE_TEXT_WIDTH_PX
    )


def test_an_apostrophe_and_both_quote_shapes_are_measured_from_the_table() -> None:
    """Ukrainian spells the apostrophe four ways and the source picks one; none may
    fall through to the fallback, which is over-wide by design."""
    for character in "'’ʼ«»“”„":
        assert ord(character) in noto_sans_widths.ADVANCE_WIDTHS


def test_a_codepoint_the_table_does_not_cover_is_still_measured() -> None:
    """Song text can hold anything; an unmeasurable glyph must widen the
    line, never raise and never count as zero."""
    emoji = "\U0001f525"

    assert ord(emoji) not in noto_sans_widths.ADVANCE_WIDTHS
    assert text_metrics.text_width_px(emoji, ass.FONT_SIZE_CURRENT_PX) > 0
    assert text_metrics.text_width_px(f"а{emoji}", ass.FONT_SIZE_CURRENT_PX) > (
        text_metrics.text_width_px("аа", ass.FONT_SIZE_CURRENT_PX)
    )


def test_a_side_slot_never_grows_to_a_second_line() -> None:
    """One wrapped line, not two: a second one in a side slot is exactly how
    a long line climbs into the neighbouring slot."""
    long_line = "дуже довгий рядок тексту пісні " * 5

    document = build(lines_of((1_000, long_line), (3_000, "короткий")))
    side = [
        row
        for row in dialogue_rows(document)
        if field_of(row, "Style") == ass.STYLE_ADJACENT and field_of(row, "Start") == "0:00:03.00"
    ]

    body = field_of(side[0], "Text").split("}", 1)[1]
    assert "\\N" not in body
    assert text_metrics.text_width_px(body, ass.FONT_SIZE_ADJACENT_PX) <= ass.USABLE_TEXT_WIDTH_PX


def test_a_word_wider_than_the_band_is_split_instead_of_overflowing() -> None:
    document = build(lines_of((1_000, "а" * 200)))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    for visual_line in body.split("\\N"):
        assert (
            text_metrics.text_width_px(visual_line, ass.FONT_SIZE_CURRENT_PX)
            <= ass.USABLE_TEXT_WIDTH_PX
        )


def test_the_number_of_drawn_lines_is_capped() -> None:
    """A thousand lines over 45 seconds is a broken LRC or a bill for ffmpeg's
    CPU, not a song."""
    many = lines_of(*((index * 10 + 1, f"рядок {index}") for index in range(400)))

    document = build(many)

    assert len(current_rows(document)) == ass.MAX_RENDERED_LINES


def test_the_timestamp_is_centiseconds_and_is_truncated_not_rounded_up() -> None:
    assert ass.ass_timestamp(0) == "0:00:00.00"
    assert ass.ass_timestamp(1_239) == "0:00:01.23"
    assert ass.ass_timestamp(3_661_000) == "1:01:01.00"


def test_the_same_text_fits_on_fewer_pixels_in_the_smaller_style() -> None:
    assert text_metrics.text_width_px("рядок", ass.FONT_SIZE_ADJACENT_PX) < (
        text_metrics.text_width_px("рядок", ass.FONT_SIZE_CURRENT_PX)
    )


def test_a_single_glyph_wider_than_the_whole_band_still_gets_a_line() -> None:
    """The wrapper asks for the longest head that fits; an empty answer
    would spin it forever, so the floor is one character."""
    wrapped = ass.wrap_for_slot("щщщ", font_size_px=10_000, max_lines=2)

    assert wrapped.split("\\N")[0] == "щ"
