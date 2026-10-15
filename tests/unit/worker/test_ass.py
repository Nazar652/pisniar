"""The `.ass` generator, read back the way libass reads it: positional
`Style:` rows against their own `Format:`, an explicit `PlayRes`, three slots
placed by `\\pos`, the shift into fragment time, the line boundaries, and the
substitution that makes an override block unspellable.
"""

from __future__ import annotations

import pytest

from pisniar.worker import ass, layout, noto_sans_widths, text_metrics

FRAGMENT_START_MS = 0
FRAGMENT_END_MS = 45_000

# only used to assert the slots do not overlap: libass draws a line roughly
# 1.2x its font size tall, so this is the pessimistic block height.
LINE_HEIGHT_RATIO = 1.2

# measured on the worker image: libass stacks lines one `Fontsize` apart, so a
# two-line 52 px block inks about 89 px where this model claims 104, the safe direction.
MEASURED_LINE_HEIGHT_RATIO = 1.0

# a 123-character line, too long for one line of the band.
LONG_LINE_OF_123_CHARACTERS = (
    "Цей рядок навмисно довгий, бо гейт показав, що стовідсотково "
    "не влізе в один рядок і його доведеться таки розбивати надвоє."
)
# 576 usable px / (52 px * 0.52 em): the budget of an average glyph width.
CHARACTERS_AT_AN_AVERAGE_GLYPH_WIDTH = 21


def lines_of(*pairs: tuple[int, str]) -> list[ass.LyricLine]:
    return [
        ass.LyricLine(line_index=index, start_ms=start_ms, text=text) for index, (start_ms, text) in enumerate(pairs)
    ]


def build(
    lines: list[ass.LyricLine],
    *,
    fragment_start_ms: int = FRAGMENT_START_MS,
    fragment_end_ms: int = FRAGMENT_END_MS,
) -> str:
    document = ass.build_ass_subtitles(lines, fragment_start_ms=fragment_start_ms, fragment_end_ms=fragment_end_ms)
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
    """without `PlayResX/Y` libass assumes 384x288 and every coordinate below lands somewhere else."""
    document = build(lines_of((1_000, "рядок")))

    assert "PlayResX: 720" in document
    assert "PlayResY: 1280" in document
    assert "ScriptType: v4.00+" in document
    assert "WrapStyle: 2" in document


def test_every_style_row_is_positional_against_its_own_format_line() -> None:
    """libass silently ignores `Alignment=5` written as `key=value`, so the row is read back and counted instead."""
    document = build(lines_of((1_000, "рядок")))
    declared_fields = document.split("Format: ", 1)[1].split("\n", 1)[0].split(", ")

    assert declared_fields == list(ass.STYLE_FORMAT_FIELDS)
    for row in style_rows(document):
        values = row.removeprefix("Style: ").split(",")
        assert len(values) == len(ass.STYLE_FORMAT_FIELDS)
        assert "=" not in row


def test_alignment_is_a_number_and_the_vertical_margin_is_honestly_zero() -> None:
    """`\\an5` ignores `MarginV`, so a non-zero one would be a lie about where the line is."""
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
    """true today, and pinned here rather than remembered."""
    document = build(lines_of((1_000, "так, ось, кома")))

    assert ass.EVENT_FORMAT_FIELDS[-1] == "Text"
    assert field_of(current_rows(document)[0], "Text").endswith("так, ось, кома")


def test_each_line_is_placed_by_an_explicit_position_from_the_constants() -> None:
    """slot symmetry is a constant, not an accident of margins."""
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


def test_the_slots_are_the_screen_slots_centred_on_the_frame() -> None:
    assert (ass.PREVIOUS_SLOT_Y, ass.CURRENT_SLOT_Y, ass.NEXT_SLOT_Y) == (905, 1035, 1165)
    assert ass.SLOT_CENTER_X == 360
    assert (ass.FONT_SIZE_CURRENT_PX, ass.FONT_SIZE_ADJACENT_PX) == (52, 38)
    assert ass.USABLE_TEXT_WIDTH_PX == layout.LYRICS_TEXT_WIDTH_PX == 576
    assert ass.PLAY_RES_X == layout.FRAME_WIDTH_PX
    assert ass.PLAY_RES_Y == layout.FRAME_HEIGHT_PX


def glass_pane() -> tuple[int, int, int, int]:
    """the inner pane of the screen: the box less the 4 px bezel `turntable` draws."""
    left, top, right, bottom = layout.SCREEN_BOX_PX
    return left + 4, top + 4, right - 4, bottom - 5


def test_no_slot_reaches_into_its_neighbour_or_out_of_the_glass() -> None:
    current_half_height = ass.MAX_CURRENT_WRAPPED_LINES * ass.FONT_SIZE_CURRENT_PX * (LINE_HEIGHT_RATIO / 2)
    adjacent_half_height = ass.MAX_ADJACENT_WRAPPED_LINES * ass.FONT_SIZE_ADJACENT_PX * (LINE_HEIGHT_RATIO / 2)
    pane_left, pane_top, pane_right, pane_bottom = glass_pane()

    assert ass.PREVIOUS_SLOT_Y - adjacent_half_height > pane_top
    assert ass.PREVIOUS_SLOT_Y + adjacent_half_height < ass.CURRENT_SLOT_Y - current_half_height
    assert ass.CURRENT_SLOT_Y + current_half_height < ass.NEXT_SLOT_Y - adjacent_half_height
    assert ass.NEXT_SLOT_Y + adjacent_half_height < pane_bottom
    half_width = ass.USABLE_TEXT_WIDTH_PX / 2
    assert ass.SLOT_CENTER_X - half_width - ass.LYRICS_OUTLINE_PX > pane_left
    assert ass.SLOT_CENTER_X + half_width + ass.LYRICS_OUTLINE_PX < pane_right


def test_the_lyrics_styles_use_the_screen_colours_and_a_glass_coloured_outline() -> None:
    rows = {
        row.removeprefix("Style: ").split(",")[0]: row.removeprefix("Style: ").split(",")
        for row in style_rows(build(lines_of((1_000, "рядок"))))
    }
    colour = ass.STYLE_FORMAT_FIELDS.index("PrimaryColour")
    outline_colour = ass.STYLE_FORMAT_FIELDS.index("OutlineColour")
    outline = ass.STYLE_FORMAT_FIELDS.index("Outline")
    size = ass.STYLE_FORMAT_FIELDS.index("Fontsize")

    assert set(rows) == {ass.STYLE_CURRENT, ass.STYLE_ADJACENT}
    assert rows[ass.STYLE_CURRENT][colour] == "&H00FFFFFF"
    assert rows[ass.STYLE_ADJACENT][colour] == "&H00AA9C96"
    assert rows[ass.STYLE_CURRENT][size] == "52"
    assert rows[ass.STYLE_ADJACENT][size] == "38"
    for values in rows.values():
        assert values[outline_colour] == "&H000B0805"
        assert values[outline] == "2"
    assert ass.ass_colour(layout.GLASS_RGB) == "&H000B0805"


def block_extent(
    slot_y: int,
    *,
    wrapped_lines: int,
    font_size_px: int,
) -> tuple[float, float]:
    half_height = wrapped_lines * font_size_px * MEASURED_LINE_HEIGHT_RATIO / 2
    return slot_y - half_height, slot_y + half_height


def test_a_current_line_that_wraps_to_two_still_clears_its_smaller_neighbours() -> None:
    """the tightest case: the current block doubles while the side slots stay one
    smaller line; 59 px of glass is left on each side, 45 px under the 1.2x reading."""
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
    pane = glass_pane()
    assert previous_top > pane[1]
    assert next_bottom < pane[3]


def test_the_two_line_case_is_the_one_the_frame_has_to_show() -> None:
    """the clearance above matters only if the wrapper can still produce a
    two-line current block at the measured glyph widths."""
    document = build(lines_of((1_000, LONG_LINE_OF_123_CHARACTERS)))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    assert body.count("\\N") == 1


def test_an_absolute_start_is_shifted_into_fragment_time() -> None:
    document = build(lines_of((32_000, "рядок")), fragment_start_ms=30_000, fragment_end_ms=75_000)

    assert field_of(current_rows(document)[0], "Start") == "0:00:02.00"


def test_a_line_that_ended_before_the_fragment_never_appears_but_one_still_held_at_its_start_does() -> None:
    document = build(
        lines_of((5_000, "до фрагмента"), (20_000, "ще на склі"), (32_000, "у фрагменті")),
        fragment_start_ms=30_000,
        fragment_end_ms=75_000,
    )

    rows = current_rows(document)
    assert [field_of(row, "Text").split("}", 1)[1] for row in rows] == ["ще на склі", "у фрагменті"]
    assert (field_of(rows[0], "Start"), field_of(rows[0], "End")) == ("0:00:00.00", "0:00:02.00")


def test_a_line_after_the_fragment_never_appears() -> None:
    document = build(lines_of((1_000, "у фрагменті"), (50_000, "після фрагмента")))

    assert [field_of(row, "Text").split("}", 1)[1] for row in current_rows(document)] == ["у фрагменті"]


def test_a_line_crossing_the_far_edge_is_cut_at_it() -> None:
    document = build(lines_of((44_000, "останній")))

    assert field_of(current_rows(document)[0], "End") == "0:00:45.00"


def test_a_line_ends_when_the_next_one_starts() -> None:
    document = build(lines_of((1_000, "перший"), (2_500, "другий")))

    assert field_of(current_rows(document)[0], "End") == "0:00:02.50"


def test_a_line_followed_by_a_long_gap_stays_until_the_next_one() -> None:
    document = build(
        lines_of((1_000, "перший"), (61_000, "другий")),
        fragment_start_ms=0,
        fragment_end_ms=90_000,
    )

    assert field_of(current_rows(document)[0], "End") == "0:01:01.00"


def test_a_line_followed_by_a_gap_over_the_ceiling_does_not_blank_the_glass() -> None:
    document = build(lines_of((88_430, "Вдихай! Видихай!"), (99_270, "далі")), fragment_end_ms=120_000)

    assert field_of(current_rows(document)[0], "End") == "0:01:39.27"


def test_the_last_line_of_the_text_gets_the_ceiling() -> None:
    document = build(lines_of((1_000, "перший"), (2_000, "останній")), fragment_end_ms=60_000)

    assert field_of(current_rows(document)[-1], "End") == ass.ass_timestamp(2_000 + ass.LAST_LINE_MAX_MS)


def test_a_line_before_a_later_one_outside_the_fragment_stays_until_the_fragment_ends() -> None:
    document = build(lines_of((1_000, "перший"), (90_000, "після фрагмента")))

    assert field_of(current_rows(document)[0], "End") == "0:00:45.00"


def test_a_pause_leaves_the_middle_empty_and_shows_the_nearest_lines_on_the_sides() -> None:
    document = build(lines_of((1_000, "до"), (3_000, ""), (10_000, "після")))
    during_pause = [row for row in dialogue_rows(document) if field_of(row, "Start") == "0:00:03.00"]

    assert len(current_rows(document)) == 2
    assert [(field_of(row, "Style"), field_of(row, "End")) for row in during_pause] == [
        (ass.STYLE_ADJACENT, "0:00:10.00"),
        (ass.STYLE_ADJACENT, "0:00:10.00"),
    ]
    assert [field_of(row, "Text").split("}", 1)[1] for row in during_pause] == ["до", "після"]
    assert f"\\pos({ass.SLOT_CENTER_X},{ass.CURRENT_SLOT_Y})" not in "".join(field_of(r, "Text") for r in during_pause)


def test_a_pause_as_the_last_entry_shows_its_neighbour_for_the_ceiling() -> None:
    document = build(lines_of((1_000, "рядок"), (3_000, "")), fragment_end_ms=60_000)
    during_pause = [row for row in dialogue_rows(document) if field_of(row, "Start") == "0:00:03.00"]

    assert [field_of(row, "Text").split("}", 1)[1] for row in during_pause] == ["рядок"]
    assert field_of(during_pause[0], "End") == ass.ass_timestamp(3_000 + ass.LAST_LINE_MAX_MS)


def test_a_pause_with_no_line_around_it_draws_nothing() -> None:
    assert ass.build_ass_subtitles(lines_of((3_000, "")), fragment_start_ms=0, fragment_end_ms=45_000) is None


def test_pauses_count_toward_the_drawn_window_budget() -> None:
    many = lines_of(*((index * 10 + 1, "" if index % 2 else f"рядок {index}") for index in range(400)))

    document = build(many)

    assert len({field_of(row, "Start") for row in dialogue_rows(document)}) == ass.MAX_RENDERED_LINES


def test_two_lines_sharing_a_start_do_not_produce_a_zero_length_event() -> None:
    document = build(lines_of((1_000, "перший"), (1_000, "другий")))

    assert [field_of(row, "Start") for row in current_rows(document)] == ["0:00:01.00"]


def test_a_fragment_with_no_lines_in_it_produces_no_file_at_all() -> None:
    """`None`, not an empty document: that is what `text_version=None` is
    computed from, and what keeps «no text» a state instead of a blank."""
    assert ass.build_ass_subtitles(lines_of((50_000, "після")), fragment_start_ms=0, fragment_end_ms=45_000) is None


def test_inverted_fragment_bounds_are_our_own_error_and_are_loud() -> None:
    with pytest.raises(ass.AssGenerationError):
        ass.build_ass_subtitles(lines_of((1_000, "рядок")), fragment_start_ms=45_000, fragment_end_ms=45_000)


def test_the_order_of_the_frames_is_the_stored_line_index_never_the_timecode() -> None:
    """untimed lines will have only `line_index` to stand on, so a source's
    out-of-order timecodes must not reorder the lines."""
    lines = [
        ass.LyricLine(line_index=0, start_ms=5_000, text="перший"),
        ass.LyricLine(line_index=1, start_ms=1_000, text="другий"),
    ]

    placed = ass.place_lines(lines, 0)

    assert [line.text for line in placed] == ["перший", "другий"]


def test_a_source_that_timed_its_lines_out_of_order_is_not_resequenced() -> None:
    """ordering by time would quietly repair the source and draw both lines, which is a different song's order."""
    lines = [
        ass.LyricLine(line_index=0, start_ms=5_000, text="перший"),
        ass.LyricLine(line_index=1, start_ms=1_000, text="другий"),
    ]

    document = build(lines)
    texts = [field_of(row, "Text").split("}", 1)[1] for row in current_rows(document)]

    assert texts == ["другий"]


def test_a_line_without_a_timecode_is_kept_out_of_the_frame() -> None:
    """the predicate is spelled out while it is still a tautology, so dropping
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
def test_the_characters_that_make_an_override_block_are_substituted(raw: str, expected: str) -> None:
    """ASS has no escapes inside a line, so this is substitution, not escaping;
    unsubstituted, the first case draws red 90 px pixels outside its slot."""
    assert ass.escape_ass_text(raw) == expected


def test_no_override_block_survives_into_a_dialogue_body() -> None:
    document = build(lines_of((1_000, "{\\an7\\fs90\\c&H0000FF&}червоний")))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    assert "{" not in body
    assert "}" not in body
    assert "\\" not in body.replace("\\N", "")


def test_our_own_position_tag_is_still_the_only_brace_in_the_row() -> None:
    document = build(lines_of((1_000, "{\\pos(0,0)}підміна")))
    text = field_of(current_rows(document)[0], "Text")

    assert text.count("{") == 1
    assert text.startswith(f"{{\\pos(360,{ass.CURRENT_SLOT_Y})}}")


def test_a_line_that_is_only_control_characters_becomes_a_pause() -> None:
    document = build(lines_of((1_000, "рядок"), (3_000, "\\\\​"), (5_000, "третій")))

    assert len(current_rows(document)) == 2


def test_a_long_line_is_wrapped_by_us_and_never_by_libass() -> None:
    """left to libass, a 123-character line runs off both edges of the frame;
    `WrapStyle: 2` plus our own measurement is what stops it."""
    long_line = "дуже довгий рядок тексту пісні " * 5

    document = build(lines_of((1_000, long_line)))
    body = field_of(current_rows(document)[0], "Text").split("}", 1)[1]

    assert body.count("\\N") == ass.MAX_CURRENT_WRAPPED_LINES - 1
    assert body.endswith(ass.ELLIPSIS)
    for visual_line in body.split("\\N"):
        assert text_metrics.text_width_px(visual_line, ass.FONT_SIZE_CURRENT_PX) <= ass.USABLE_TEXT_WIDTH_PX


def test_a_wrapped_line_uses_the_width_the_band_actually_has() -> None:
    """counted in characters on purpose: measuring the result with the function
    that produced it would pass however wrong that function got."""
    assert len(LONG_LINE_OF_123_CHARACTERS) == 123

    document = build(lines_of((1_000, LONG_LINE_OF_123_CHARACTERS)))
    first_visual_line = field_of(current_rows(document)[0], "Text").split("}", 1)[1].split("\\N")[0]

    assert len(first_visual_line) > CHARACTERS_AT_AN_AVERAGE_GLYPH_WIDTH
    assert text_metrics.text_width_px(first_visual_line, ass.FONT_SIZE_CURRENT_PX) <= ass.USABLE_TEXT_WIDTH_PX


def test_an_apostrophe_and_both_quote_shapes_are_measured_from_the_table() -> None:
    """ukrainian spells the apostrophe four ways and the source picks one; none may
    fall through to the fallback, which is over-wide by design."""
    for character in "'’ʼ«»“”„":
        assert ord(character) in noto_sans_widths.ADVANCE_WIDTHS


def test_a_codepoint_the_table_does_not_cover_is_still_measured() -> None:
    """song text can hold anything; an unmeasurable glyph must widen the line, never raise and never count as zero."""
    emoji = "\U0001f525"

    assert ord(emoji) not in noto_sans_widths.ADVANCE_WIDTHS
    assert text_metrics.text_width_px(emoji, ass.FONT_SIZE_CURRENT_PX) > 0
    assert text_metrics.text_width_px(f"а{emoji}", ass.FONT_SIZE_CURRENT_PX) > (
        text_metrics.text_width_px("аа", ass.FONT_SIZE_CURRENT_PX)
    )


def test_a_side_slot_never_grows_to_a_second_line() -> None:
    """one wrapped line, not two: a second one in a side slot is exactly how
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
        assert text_metrics.text_width_px(visual_line, ass.FONT_SIZE_CURRENT_PX) <= ass.USABLE_TEXT_WIDTH_PX


def test_the_number_of_drawn_lines_is_capped() -> None:
    """a thousand lines over 45 seconds is a broken LRC or a bill for ffmpeg's CPU, not a song."""
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
    """the wrapper asks for the longest head that fits; an empty answer
    would spin it forever, so the floor is one character."""
    wrapped = ass.wrap_for_slot("щщщ", font_size_px=10_000, max_lines=2)

    assert wrapped.split("\\N")[0] == "щ"


def test_fits_text_width_and_with_ellipsis_take_the_width_they_are_given() -> None:
    word = "рядок тексту"
    size = ass.FONT_SIZE_CURRENT_PX
    width = text_metrics.text_width_px(word, size)

    assert ass.fits_text_width(word, size, width)
    assert not ass.fits_text_width(word, size, width - 1)
    assert ass.with_ellipsis(word, size, width + 100) == word + ass.ELLIPSIS
    cut = ass.with_ellipsis(word, size, width / 2)
    assert cut.endswith(ass.ELLIPSIS)
    assert text_metrics.text_width_px(cut, size) <= width / 2


# ---------------------------------------------------------------- colours, styles, documents


def test_colours_are_written_blue_first_in_both_forms() -> None:
    assert ass.ass_colour((255, 196, 110)) == "&H006EC4FF"
    assert ass.tag_colour((255, 196, 110)) == "&H6EC4FF&"
    assert ass.ass_colour(ass.ADJACENT_RGB) == "&H00AA9C96"


def test_the_style_row_uses_the_given_colours_and_outline() -> None:
    values = ass.style_row("probe", 33, "&H00010203", "&H00040506", 7).removeprefix("Style: ")
    fields = values.split(",")

    assert fields[ass.STYLE_FORMAT_FIELDS.index("Name")] == "probe"
    assert fields[ass.STYLE_FORMAT_FIELDS.index("Fontsize")] == "33"
    assert fields[ass.STYLE_FORMAT_FIELDS.index("PrimaryColour")] == "&H00010203"
    assert fields[ass.STYLE_FORMAT_FIELDS.index("OutlineColour")] == "&H00040506"
    assert fields[ass.STYLE_FORMAT_FIELDS.index("Outline")] == "7"


def test_the_style_sections_hold_their_own_rows_under_one_header() -> None:
    lyrics = ass.styles_section(ass.lyric_style_rows())
    display = ass.styles_section(ass.display_style_rows())

    assert lyrics[:2] == display[:2] == ["[V4+ Styles]", "Format: " + ", ".join(ass.STYLE_FORMAT_FIELDS)]
    assert [row.split(",")[0] for row in lyrics[2:]] == [
        f"Style: {ass.STYLE_CURRENT}",
        f"Style: {ass.STYLE_ADJACENT}",
    ]
    assert [row.split(",")[0] for row in display[2:]] == [
        f"Style: {ass.STYLE_DISPLAY_TITLE}",
        f"Style: {ass.STYLE_DISPLAY_ARTISTS}",
    ]


def test_the_display_styles_have_the_display_sizes_and_colours() -> None:
    title, artists = (row.removeprefix("Style: ").split(",") for row in ass.display_style_rows())
    size = ass.STYLE_FORMAT_FIELDS.index("Fontsize")
    colour = ass.STYLE_FORMAT_FIELDS.index("PrimaryColour")

    assert (title[size], artists[size]) == ("28", "19")
    assert title[colour] == ass.ass_colour((255, 196, 110))
    assert artists[colour] == ass.ass_colour((190, 140, 80))


def test_every_style_of_both_documents_names_the_proved_font_family() -> None:
    documents = [
        build(lines_of((1_000, "рядок"))),
        ass.build_display_subtitles("Назва", ["Виконавець"], 5_000),
    ]
    names = []
    for document in documents:
        for row in style_rows(document):
            values = row.removeprefix("Style: ").split(",")
            assert values[ass.STYLE_FORMAT_FIELDS.index("Fontname")] == ass.FONT_FAMILY
            names.append(values[0])
    assert names == [
        ass.STYLE_CURRENT,
        ass.STYLE_ADJACENT,
        ass.STYLE_DISPLAY_TITLE,
        ass.STYLE_DISPLAY_ARTISTS,
    ]


def test_dialogue_event_fills_every_declared_field() -> None:
    event = ass.dialogue_event(1, 1_000, 2_500, "probe", "{\\pos(1,2)}текст")

    assert list(event) == list(ass.EVENT_FORMAT_FIELDS)
    assert (event["Layer"], event["Start"], event["End"]) == ("1", "0:00:01.00", "0:00:02.50")
    assert event["Style"] == "probe"
    assert event["Text"] == "{\\pos(1,2)}текст"


def test_document_text_joins_header_styles_and_events() -> None:
    event = ass.dialogue_event(0, 0, 1_000, "probe", "текст")
    document = ass.document_text([ass.style_row("probe", 10, "&H00000000", "&H00000000", 0)], [event])

    assert document.startswith("[Script Info]\n")
    assert document.index("[V4+ Styles]") < document.index("[Events]")
    assert document.endswith("Dialogue: 0,0:00:00.00,0:00:01.00,probe,,0,0,0,,текст\n")


# ---------------------------------------------------------------- the display


def display_document(title: str = "Назва", artists: list[str] | None = None) -> str:
    return ass.build_display_subtitles(title, ["Виконавець"] if artists is None else artists, 12_000)


def display_text_of(row: str) -> str:
    return field_of(row, "Text").split("}", 1)[1]


def test_the_display_has_two_events_per_line_at_the_display_positions() -> None:
    rows = dialogue_rows(display_document())

    assert len(rows) == 4
    positions = [field_of(row, "Text").split("\\", 2)[1] for row in rows]
    assert positions == [
        f"pos({ass.SLOT_CENTER_X},{ass.DISPLAY_TITLE_Y})",
        f"pos({ass.SLOT_CENTER_X},{ass.DISPLAY_TITLE_Y})",
        f"pos({ass.SLOT_CENTER_X},{ass.DISPLAY_ARTISTS_Y})",
        f"pos({ass.SLOT_CENTER_X},{ass.DISPLAY_ARTISTS_Y})",
    ]
    assert (ass.DISPLAY_TITLE_Y, ass.DISPLAY_ARTISTS_Y, ass.SLOT_CENTER_X) == (746, 775, 360)


def test_each_display_line_is_a_glow_layer_under_a_crisp_layer() -> None:
    glow, crisp = dialogue_rows(display_document())[:2]

    assert (field_of(glow, "Layer"), field_of(crisp, "Layer")) == ("0", "1")
    assert "\\blur6" in field_of(glow, "Text")
    assert "\\alpha&H90&" in field_of(glow, "Text")
    assert "\\bord3" in field_of(glow, "Text")
    assert "\\3c&H6EC4FF&" in field_of(glow, "Text")
    assert "\\bord0" in field_of(crisp, "Text")
    assert "\\blur" not in field_of(crisp, "Text")
    assert display_text_of(glow) == display_text_of(crisp) == "Назва"


def test_the_display_lasts_the_whole_fragment() -> None:
    for row in dialogue_rows(display_document()):
        assert field_of(row, "Start") == "0:00:00.00"
        assert field_of(row, "End") == "0:00:12.00"


def test_the_display_styles_follow_the_lines() -> None:
    styles = [field_of(row, "Style") for row in dialogue_rows(display_document())]

    assert styles == [ass.STYLE_DISPLAY_TITLE] * 2 + [ass.STYLE_DISPLAY_ARTISTS] * 2


def test_override_blocks_in_a_title_do_not_survive() -> None:
    document = display_document("{\\an7\\fs90}Назва\\Nдруга")

    for row in dialogue_rows(document)[:2]:
        body = display_text_of(row)
        assert body == "(an7fs90)НазваNдруга"
        assert "\\" not in body
        assert field_of(row, "Text").count("{") == 1


def test_line_breaks_and_direction_marks_cannot_split_or_flip_a_display_row() -> None:
    """a break inside a `Text` would end the event early and turn the rest into a new line
    of the file; U+202E would reverse what is drawn."""
    hostile = "пер\nший\r\nдру гий‮третій"
    document = display_document(hostile, [hostile])

    rows = dialogue_rows(document)
    assert len(rows) == 4
    for row in rows:
        text = field_of(row, "Text")
        assert not {"\n", "\r", " ", "‮"} & set(text)
    assert display_text_of(rows[0]) == "пер ший дру гийтретій"
    assert display_text_of(rows[2]) == "пер ший дру гийтретій"


def test_artists_are_joined_with_a_comma_and_a_space() -> None:
    document = display_document(artists=["Перший", "Другий", "Третій"])

    assert display_text_of(dialogue_rows(document)[2]) == "Перший, Другий, Третій"


def test_an_empty_name_among_the_artists_leaves_no_stray_separator() -> None:
    document = display_document(artists=["Перший", "", "\\", "Другий"])

    assert display_text_of(dialogue_rows(document)[2]) == "Перший, Другий"


@pytest.mark.parametrize(
    ("title", "artists", "expected_events"),
    [("", ["Виконавець"], 2), ("Назва", [], 2), ("Назва", [""], 2), ("", [], 0), ("\\", ["\\"], 0)],
)
def test_an_empty_field_draws_nothing(
    title: str,
    artists: list[str],
    expected_events: int,
) -> None:
    assert len(dialogue_rows(display_document(title, artists))) == expected_events


def test_a_long_title_is_cut_to_the_display_width_with_an_ellipsis() -> None:
    title = "Дуже довга назва пісні " * 8
    row = dialogue_rows(display_document(title))[0]
    body = display_text_of(row)

    assert body.endswith(ass.ELLIPSIS)
    assert text_metrics.text_width_px(body, ass.FONT_SIZE_DISPLAY_TITLE_PX) <= layout.DISPLAY_TEXT_WIDTH_PX
    assert body.startswith("Дуже довга назва")


def test_long_artists_are_cut_to_the_display_width_at_the_smaller_size() -> None:
    names = [f"Виконавець номер {index}" for index in range(30)]
    body = display_text_of(dialogue_rows(display_document(artists=names))[2])

    assert body.endswith(ass.ELLIPSIS)
    assert text_metrics.text_width_px(body, ass.FONT_SIZE_DISPLAY_ARTISTS_PX) <= layout.DISPLAY_TEXT_WIDTH_PX


def test_a_title_that_fits_is_left_alone() -> None:
    assert ass.display_text("Коротка", ass.FONT_SIZE_DISPLAY_TITLE_PX) == "Коротка"


def test_the_input_is_cut_to_the_source_limit_before_the_width_cut() -> None:
    """500 blanks in front of a real name: only the cut before escaping leaves nothing to draw."""
    padded = " " * ass.MAX_SOURCE_TEXT_CHARACTERS

    assert ass.display_text(padded + "Назва", ass.FONT_SIZE_DISPLAY_TITLE_PX) == ""
    document = ass.build_display_subtitles(padded + "Назва", [padded + "Виконавець"], 5_000)
    assert dialogue_rows(document) == []


def test_display_text_escapes_before_it_measures() -> None:
    assert ass.display_text("{a}\\b", ass.FONT_SIZE_DISPLAY_TITLE_PX) == "(a)b"


def test_display_events_are_empty_for_empty_text_and_a_pair_otherwise() -> None:
    assert ass.display_events("", 746, ass.STYLE_DISPLAY_TITLE, (1, 2, 3), 1_000) == []
    pair = ass.display_events("текст", 746, ass.STYLE_DISPLAY_TITLE, (1, 2, 3), 1_000)

    assert [event["Layer"] for event in pair] == ["0", "1"]
    assert "\\3c&H030201&" in pair[0]["Text"]


@pytest.mark.parametrize("length_ms", [0, -1])
def test_a_display_without_a_duration_is_our_own_error(length_ms: int) -> None:
    with pytest.raises(ass.AssGenerationError):
        ass.build_display_subtitles("Назва", ["Виконавець"], length_ms)


def test_the_display_lines_sit_inside_the_display_pane() -> None:
    left, top, right, bottom = layout.DISPLAY_BOX_PX
    title_half = ass.FONT_SIZE_DISPLAY_TITLE_PX * LINE_HEIGHT_RATIO / 2
    artists_half = ass.FONT_SIZE_DISPLAY_ARTISTS_PX * LINE_HEIGHT_RATIO / 2

    assert ass.DISPLAY_TITLE_Y - title_half > top + 3
    assert ass.DISPLAY_TITLE_Y + title_half < ass.DISPLAY_ARTISTS_Y - artists_half
    assert ass.DISPLAY_ARTISTS_Y + artists_half < bottom - 4
    half_width = layout.DISPLAY_TEXT_WIDTH_PX / 2
    assert ass.SLOT_CENTER_X - half_width > left
    assert ass.SLOT_CENTER_X + half_width < right


def test_a_song_without_text_still_gets_a_display_but_no_lyrics_file() -> None:
    assert ass.build_ass_subtitles([], fragment_start_ms=FRAGMENT_START_MS, fragment_end_ms=45_000) is None
    assert dialogue_rows(display_document())
