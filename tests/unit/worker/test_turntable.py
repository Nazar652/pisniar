"""The turntable frame: the layout numbers, and the properties of the five drawn layers. The layers are drawn once for
the module; pixel-level pins of the finished video live in the container smoke test."""

from __future__ import annotations

import ast
import math
import random
import statistics
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from pisniar.worker import layout, turntable

WORKER_DIRECTORY = Path(layout.__file__).parent
FRAME = (layout.FRAME_WIDTH_PX, layout.FRAME_HEIGHT_PX)
LAYER_FILE_NAMES = {"bg.png", "vinyl.png", "label-mask.png", "hub.png", "overlay.png"}
BLACK = (0, 0, 0)


def pixel(
    image: Image.Image,
    x: int,
    y: int,
) -> tuple[int, ...]:
    value = image.getpixel((x, y))
    assert isinstance(value, tuple)
    return value


def level(
    image: Image.Image,
    x: int,
    y: int,
) -> int:
    value = image.getpixel((x, y))
    assert isinstance(value, int)
    return value


def up(value: float) -> int:
    return round(turntable.up(value))


def record_up(value: float) -> int:
    return round(turntable.record_up(value))


def inside(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def boxes_apart(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    return first[2] <= second[0] or second[2] <= first[0] or first[3] <= second[1] or second[3] <= first[1]


def disc_bounds(centre: tuple[int, int], radius: int) -> tuple[int, int, int, int]:
    return (centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius)


def sector_means(vinyl: Image.Image) -> list[float]:
    """mean brightness of 72 sectors, each averaged over r 200..240 so the grooves cancel out."""
    middle = layout.VINYL_RADIUS_PX
    means = []
    for sector in range(72):
        angle = math.radians(sector * 5)
        values = []
        for radius in range(200, 240):
            red, green, blue = pixel(
                vinyl,
                round(middle + radius * math.cos(angle)),
                round(middle + radius * math.sin(angle)),
            )[:3]
            values.append(0.299 * red + 0.587 * green + 0.114 * blue)
        means.append(sum(values) / len(values))
    return means


@pytest.fixture(scope="module")
def layers(tmp_path_factory: pytest.TempPathFactory) -> turntable.TurntableLayers:
    return turntable.write_layers(tmp_path_factory.mktemp("turntable"))


@pytest.fixture(scope="module")
def background(layers: turntable.TurntableLayers) -> Image.Image:
    with Image.open(layers.background) as opened:
        return opened.copy()


@pytest.fixture(scope="module")
def vinyl(layers: turntable.TurntableLayers) -> Image.Image:
    with Image.open(layers.vinyl) as opened:
        return opened.copy()


@pytest.fixture(scope="module")
def label_mask(layers: turntable.TurntableLayers) -> Image.Image:
    with Image.open(layers.label_mask) as opened:
        return opened.copy()


@pytest.fixture(scope="module")
def hub(layers: turntable.TurntableLayers) -> Image.Image:
    with Image.open(layers.hub) as opened:
        return opened.copy()


@pytest.fixture(scope="module")
def overlay(layers: turntable.TurntableLayers) -> Image.Image:
    with Image.open(layers.overlay) as opened:
        return opened.copy()


# ---------------------------------------------------------------- layout


def test_layout_holds_constants_and_imports_nothing() -> None:
    tree = ast.parse((WORKER_DIRECTORY / "layout.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__"}
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    assert not [node for node in tree.body if isinstance(node, ast.FunctionDef | ast.ClassDef)]


def test_the_body_fits_the_frame_and_holds_everything_else() -> None:
    assert inside(layout.BODY_BOX_PX, (0, 0, *FRAME))
    vinyl_bounds = disc_bounds(layout.VINYL_CENTER_PX, layout.VINYL_RADIUS_PX)
    platter_bounds = disc_bounds(layout.VINYL_CENTER_PX, layout.PLATTER_RADIUS_PX)
    knob_bounds = disc_bounds(layout.KNOB_CENTER_PX, layout.KNOB_RADIUS_PX)
    switch_bounds = disc_bounds(layout.SWITCH_CENTER_PX, layout.KNOB_RADIUS_PX)
    for part in (
        vinyl_bounds,
        platter_bounds,
        layout.DISPLAY_BOX_PX,
        layout.SCREEN_BOX_PX,
        knob_bounds,
        switch_bounds,
    ):
        assert inside(part, layout.BODY_BOX_PX)
    assert inside(vinyl_bounds, platter_bounds)


def test_the_display_and_the_screen_overlap_neither_the_platter_nor_each_other() -> None:
    platter_bottom = layout.VINYL_CENTER_PX[1] + layout.PLATTER_RADIUS_PX
    assert platter_bottom < layout.DISPLAY_BOX_PX[1]
    assert layout.DISPLAY_BOX_PX[3] < layout.SCREEN_BOX_PX[1]
    knob_bounds = disc_bounds(layout.KNOB_CENTER_PX, layout.KNOB_RADIUS_PX)
    switch_bounds = disc_bounds(layout.SWITCH_CENTER_PX, layout.KNOB_RADIUS_PX)
    assert boxes_apart(knob_bounds, layout.DISPLAY_BOX_PX)
    assert boxes_apart(switch_bounds, layout.DISPLAY_BOX_PX)


def test_everything_but_the_tonearm_is_symmetric_about_the_centre_line() -> None:
    centre_line = layout.FRAME_CENTER_X_PX
    assert 2 * centre_line == layout.FRAME_WIDTH_PX
    for box in (layout.BODY_BOX_PX, layout.DISPLAY_BOX_PX, layout.SCREEN_BOX_PX):
        assert box[0] + box[2] == 2 * centre_line
    assert layout.VINYL_CENTER_PX[0] == centre_line
    assert layout.KNOB_CENTER_PX[0] + layout.SWITCH_CENTER_PX[0] == 2 * centre_line
    assert layout.KNOB_CENTER_PX[1] == layout.SWITCH_CENTER_PX[1]


def test_overlay_sizes_and_offsets_are_even() -> None:
    assert layout.VINYL_DIAMETER_PX % 2 == 0
    assert layout.LABEL_DIAMETER_PX % 2 == 0
    assert (layout.VINYL_OVERLAY_X_PX, layout.VINYL_OVERLAY_Y_PX) == (60, 98)
    assert layout.VINYL_OVERLAY_X_PX % 2 == 0
    assert layout.VINYL_OVERLAY_Y_PX % 2 == 0


def test_shared_widths_and_the_glass_colour() -> None:
    assert layout.GLASS_RGB == (5, 8, 11)
    assert layout.LYRICS_TEXT_WIDTH_PX == 576
    assert layout.DISPLAY_TEXT_WIDTH_PX == 340
    assert layout.SCREEN_BOX_PX[2] - layout.SCREEN_BOX_PX[0] > layout.LYRICS_TEXT_WIDTH_PX
    assert layout.DISPLAY_BOX_PX[2] - layout.DISPLAY_BOX_PX[0] > layout.DISPLAY_TEXT_WIDTH_PX


# ---------------------------------------------------------------- the module's own boundaries


def test_turntable_imports_only_pil_the_standard_library_and_layout() -> None:
    tree = ast.parse((WORKER_DIRECTORY / "turntable.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported == {
        "__future__",
        "math",
        "random",
        "dataclasses",
        "pathlib",
        "PIL",
        "pisniar.worker",
    }


def test_turntable_opens_no_file_and_decodes_no_outside_bytes() -> None:
    tree = ast.parse((WORKER_DIRECTORY / "turntable.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"open", "frombuffer", "fromarray", "read_bytes", "read_text"}


# ---------------------------------------------------------------- V126 vinyl


def test_vinyl_is_a_square_rgba_layer(vinyl: Image.Image) -> None:
    assert vinyl.mode == "RGBA"
    assert vinyl.size == (layout.VINYL_DIAMETER_PX, layout.VINYL_DIAMETER_PX)


def test_vinyl_is_transparent_in_the_corners_and_opaque_inside_the_rim(
    vinyl: Image.Image,
) -> None:
    last = layout.VINYL_DIAMETER_PX - 1
    for corner in ((0, 0), (last, 0), (0, last), (last, last)):
        assert pixel(vinyl, *corner)[3] == 0
    middle = layout.VINYL_RADIUS_PX
    for degrees in range(0, 360, 10):
        angle = math.radians(degrees)
        for radius, floor in ((layout.VINYL_RADIUS_PX - 4, 255), (layout.VINYL_RADIUS_PX - 3, 250)):
            x = round(middle + radius * math.cos(angle))
            y = round(middle + radius * math.sin(angle))
            assert pixel(vinyl, x, y)[3] >= floor


def test_vinyl_edge_falls_monotonically_from_opaque_to_transparent(vinyl: Image.Image) -> None:
    middle = layout.VINYL_RADIUS_PX
    alphas = [pixel(vinyl, middle + step, middle + step)[3] for step in range(190, 290)]
    assert alphas == sorted(alphas, reverse=True)
    assert alphas[0] == 255
    assert alphas[-1] == 0


def test_vinyl_colour_outside_the_circle_is_the_rim_colour(vinyl: Image.Image) -> None:
    middle = layout.VINYL_RADIUS_PX
    for step in (230, 250, 280):
        for sign in (1, -1):
            red, green, blue, alpha = pixel(vinyl, middle + sign * step, middle + step)
            assert alpha == 0
            assert (red, green, blue) == turntable.RIM_COLOUR


def test_vinyl_brightness_varies_around_the_disc_but_only_slowly(vinyl: Image.Image) -> None:
    means = sector_means(vinyl)
    spread = max(means) - min(means)
    steps = [abs(means[index] - means[index - 1]) for index in range(len(means))]
    assert statistics.pstdev(means) > 4
    assert max(steps) < 0.7 * spread


def test_build_vinyl_is_deterministic(vinyl: Image.Image) -> None:
    assert turntable.build_vinyl().tobytes() == vinyl.tobytes()


# ---------------------------------------------------------------- V127 label mask and hub


def test_label_mask_is_a_gray_circle(label_mask: Image.Image) -> None:
    assert label_mask.mode == "L"
    assert label_mask.size == (layout.LABEL_DIAMETER_PX, layout.LABEL_DIAMETER_PX)
    middle = layout.LABEL_DIAMETER_PX // 2
    assert level(label_mask, middle, middle) == 255
    last = layout.LABEL_DIAMETER_PX - 1
    for corner in ((0, 0), (last, 0), (0, last), (last, last)):
        assert level(label_mask, *corner) == 0


def test_hub_hole_is_opaque_and_the_groove_band_is_transparent(hub: Image.Image) -> None:
    assert hub.mode == "RGBA"
    assert hub.size == (layout.VINYL_DIAMETER_PX, layout.VINYL_DIAMETER_PX)
    middle = layout.VINYL_RADIUS_PX
    assert pixel(hub, middle, middle) == (0, 0, 0, 255)
    for degrees in range(0, 360, 30):
        angle = math.radians(degrees)
        radius = (turntable.GROOVE_BAND_INNER_PX + turntable.GROOVE_BAND_OUTER_PX) // 2
        x, y = round(middle + radius * math.cos(angle)), round(middle + radius * math.sin(angle))
        assert pixel(hub, x, y)[3] == 0
    assert pixel(hub, 0, 0)[3] == 0


# ---------------------------------------------------------------- V128 background


def test_background_is_an_opaque_frame_with_the_glass_colour_on_both_panes(
    background: Image.Image,
) -> None:
    assert background.mode == "RGB"
    assert background.size == FRAME
    screen = layout.SCREEN_BOX_PX
    display = layout.DISPLAY_BOX_PX
    for x, y in (
        (layout.FRAME_CENTER_X_PX, (screen[1] + screen[3]) // 2),
        (layout.FRAME_CENTER_X_PX, (display[1] + display[3]) // 2),
    ):
        assert all(
            abs(found - expected) <= 6
            for found, expected in zip(pixel(background, x, y), layout.GLASS_RGB, strict=True)
        )


def test_background_body_is_dark_and_the_wood_outside_it_is_warm(background: Image.Image) -> None:
    body = pixel(background, layout.BODY_BOX_PX[0] + 12, 1000)
    assert max(body) < 60
    red, green, blue = pixel(background, 5, layout.FRAME_HEIGHT_PX // 2)
    assert red > green > blue


# ---------------------------------------------------------------- V129 overlay


def test_overlay_offset_and_size_are_even_and_inside_the_frame(
    layers: turntable.TurntableLayers, overlay: Image.Image
) -> None:
    assert overlay.mode == "RGBA"
    assert layers.overlay_x % 2 == 0
    assert layers.overlay_y % 2 == 0
    assert overlay.width % 2 == 0
    assert overlay.height % 2 == 0
    assert layers.overlay_x >= 0
    assert layers.overlay_y >= 0
    assert layers.overlay_x + overlay.width <= layout.FRAME_WIDTH_PX
    assert layers.overlay_y + overlay.height <= layout.FRAME_HEIGHT_PX


def test_overlay_has_a_bright_opaque_needle_tip_near_the_needle_point(
    layers: turntable.TurntableLayers, overlay: Image.Image
) -> None:
    needle_x, needle_y = layout.NEEDLE_POINT_PX
    reach = layout.NEEDLE_TOLERANCE_PX
    tips = [
        (x, y)
        for x in range(needle_x - reach, needle_x + reach + 1)
        for y in range(needle_y - reach, needle_y + reach + 1)
        if pixel(overlay, x - layers.overlay_x, y - layers.overlay_y)[3] == 255
        and min(pixel(overlay, x - layers.overlay_x, y - layers.overlay_y)[:3]) >= 150
    ]
    assert tips


def test_overlay_leaves_free_points_transparent_and_covers_the_spindle(
    layers: turntable.TurntableLayers, overlay: Image.Image
) -> None:
    free_x, free_y = 680, 600
    assert pixel(overlay, free_x - layers.overlay_x, free_y - layers.overlay_y)[3] == 0
    centre_x, centre_y = layout.VINYL_CENTER_PX
    assert pixel(overlay, centre_x - layers.overlay_x, centre_y - layers.overlay_y)[3] == 255


def test_overlay_reflection_over_the_record_is_faint(layers: turntable.TurntableLayers, overlay: Image.Image) -> None:
    centre_x, centre_y = layout.VINYL_CENTER_PX
    alphas = [
        pixel(overlay, centre_x + dx - layers.overlay_x, centre_y + dy - layers.overlay_y)[3]
        for dx in range(-200, -19, 20)
        for dy in range(-200, 201, 20)
        if 12 < math.hypot(dx, dy) < 250
    ]
    assert 0 < max(alphas) <= 30


# ---------------------------------------------------------------- write_layers


def test_write_layers_writes_the_five_files_under_fixed_names(
    layers: turntable.TurntableLayers,
) -> None:
    paths = [layers.background, layers.vinyl, layers.label_mask, layers.hub, layers.overlay]
    assert {path.name for path in paths} == LAYER_FILE_NAMES
    assert {path.name for path in layers.background.parent.iterdir()} == LAYER_FILE_NAMES
    assert all(path.stat().st_size > 0 for path in paths)
    assert (layers.overlay_x, layers.overlay_y) == (60, 60)
    assert (layers.overlay_width, layers.overlay_height) == (644, 616)


def test_a_second_drawing_gives_the_same_pixels(background: Image.Image, overlay: Image.Image) -> None:
    assert turntable.build_background().tobytes() == background.tobytes()
    drawn, _ = turntable.build_overlay()
    assert drawn.tobytes() == overlay.tobytes()


def test_write_layers_replaces_files_already_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bg.png").write_bytes(b"stale")
    monkeypatch.setattr(turntable, "build_background", lambda: Image.new("RGB", (4, 2)))
    monkeypatch.setattr(turntable, "build_vinyl", lambda: Image.new("RGBA", (4, 2)))
    monkeypatch.setattr(turntable, "build_label_mask", lambda: Image.new("L", (4, 2)))
    monkeypatch.setattr(turntable, "build_hub", lambda: Image.new("RGBA", (4, 2)))
    monkeypatch.setattr(turntable, "build_overlay", lambda: (Image.new("RGBA", (6, 4)), (10, 20)))

    written = turntable.write_layers(tmp_path)

    assert {path.name for path in tmp_path.iterdir()} == LAYER_FILE_NAMES
    with Image.open(written.background) as replaced:
        assert replaced.size == (4, 2)
    assert (written.overlay_x, written.overlay_y) == (10, 20)
    assert (written.overlay_width, written.overlay_height) == (6, 4)


# ---------------------------------------------------------------- helpers: frame


def test_up_and_frame_canvas_scale_by_the_frame_supersample() -> None:
    assert turntable.up(5) == 5 * turntable.FRAME_SUPERSAMPLE
    assert turntable.frame_canvas() == (
        FRAME[0] * turntable.FRAME_SUPERSAMPLE,
        FRAME[1] * turntable.FRAME_SUPERSAMPLE,
    )


def test_rounded_mask_fills_the_box_and_not_its_corner_or_outside() -> None:
    mask = turntable.rounded_mask((100, 100, 200, 160), 20)
    assert mask.size == turntable.frame_canvas()
    assert level(mask, up(150), up(130)) == 255
    assert level(mask, up(100) + 1, up(100) + 1) == 0
    assert level(mask, up(50), up(130)) == 0


def test_rounded_mask_blur_softens_the_edge() -> None:
    mask = turntable.rounded_mask((100, 100, 200, 160), 20, blur=4)
    assert 0 < level(mask, up(100), up(130)) < 255


def test_circle_mask_fills_the_disc_and_blur_softens_its_edge() -> None:
    mask = turntable.circle_mask(200, 200, 30)
    assert level(mask, up(200), up(200)) == 255
    assert level(mask, up(200 + 40), up(200)) == 0
    soft = turntable.circle_mask(200, 200, 30, blur=3)
    assert 0 < level(soft, up(230), up(200)) < 255


def test_fill_paints_by_the_mask_strength() -> None:
    base = Image.new("RGB", (4, 1), (0, 0, 0))
    mask = Image.new("L", (4, 1), 255)
    assert pixel(turntable.fill(base, mask, (200, 100, 50)), 0, 0) == (200, 100, 50)
    assert pixel(turntable.fill(base, mask, (200, 100, 50), 0.5), 0, 0) == (100, 50, 25)
    assert pixel(turntable.fill(base, Image.new("L", (4, 1), 0), (200, 100, 50)), 0, 0) == BLACK


def test_shift_moves_a_mask_by_scaled_pixels() -> None:
    mask = Image.new("L", (30, 30), 0)
    mask.putpixel((3, 3), 255)
    moved = turntable.shift(mask, 1, 2)
    assert level(moved, 3 + turntable.FRAME_SUPERSAMPLE, 3 + 2 * turntable.FRAME_SUPERSAMPLE) == 255


def test_shrink_frame_divides_the_size_by_the_supersample() -> None:
    big = Image.new("RGB", (30, 60), (10, 20, 30))
    small = turntable.shrink_frame(big)
    assert small.size == (10, 20)
    assert pixel(small, 5, 5) == (10, 20, 30)


def test_bezier_runs_from_the_start_to_the_end() -> None:
    points = turntable.bezier((0, 0), (10, 0), (10, 10), (20, 10), steps=10)
    assert len(points) == 11
    assert points[0] == (0, 0)
    assert points[-1] == (20, 10)


# ---------------------------------------------------------------- helpers: background


def test_random_grid_is_seeded_and_sized() -> None:
    first = turntable.random_grid(random.Random(1), 4, 3, (40, 30))
    second = turntable.random_grid(random.Random(1), 4, 3, (40, 30))
    other = turntable.random_grid(random.Random(2), 4, 3, (40, 30))
    assert first.size == (40, 30)
    assert first.tobytes() == second.tobytes()
    assert first.tobytes() != other.tobytes()


def test_wood_texture_is_seeded_warm_and_sized() -> None:
    texture = turntable.wood_texture(60, 100, 5)
    assert texture.mode == "RGB"
    assert texture.size == (60, 100)
    assert texture.tobytes() == turntable.wood_texture(60, 100, 5).tobytes()
    red, green, blue = (statistics.fmean(band.tobytes()) for band in texture.split())
    assert red > green > blue


def test_vignette_darkens_the_edges_more_than_the_middle() -> None:
    flat = Image.new("RGB", FRAME, (200, 200, 200))
    shaded = turntable.vignette(flat, 0.55)
    assert shaded.size == FRAME
    middle = pixel(shaded, FRAME[0] // 2, FRAME[1] // 2)[0]
    corner = pixel(shaded, 0, 0)[0]
    assert corner < middle <= 200


@pytest.mark.parametrize(
    ("draw", "touched"),
    [
        (turntable.draw_body, (360, 640)),
        (turntable.draw_platter, (360, 640)),
        (turntable.draw_knob, layout.KNOB_CENTER_PX),
        (turntable.draw_speed_switch, layout.SWITCH_CENTER_PX),
        (turntable.draw_display, (360, 759)),
        (turntable.draw_screen, (360, 1035)),
    ],
)
def test_each_part_draws_where_it_belongs_and_leaves_the_far_corner(
    draw: Callable[[Image.Image], Image.Image], touched: tuple[int, int]
) -> None:
    black = Image.new("RGB", turntable.frame_canvas(), BLACK)
    drawn = draw(black)
    assert drawn.size == black.size
    assert pixel(drawn, up(touched[0]), up(touched[1])) != BLACK
    assert pixel(drawn, 3, 3) == BLACK


# ---------------------------------------------------------------- helpers: the record


def test_record_scaling_and_the_disc_box() -> None:
    assert turntable.record_up(3) == 3 * turntable.RECORD_SUPERSAMPLE
    box = turntable.disc_box(10)
    assert box[2] - box[0] == 2 * turntable.record_up(10)
    assert (box[0] + box[2]) / 2 == turntable.RECORD_CENTRE


def test_filled_disc_and_annulus() -> None:
    centre = int(turntable.RECORD_CENTRE)
    disc = turntable.filled_disc(100)
    assert disc.size == (turntable.RECORD_SIZE_PX, turntable.RECORD_SIZE_PX)
    assert level(disc, centre, centre) == 255
    assert level(disc, centre + record_up(110), centre) == 0
    ring = turntable.annulus(50, 100)
    assert level(ring, centre, centre) == 0
    assert level(ring, centre + record_up(75), centre) == 255
    soft = turntable.annulus(50, 100, 2)
    assert 0 < level(soft, centre + record_up(100), centre) < 255


def test_weakened_mix_and_tint_layer() -> None:
    assert level(turntable.weakened(Image.new("L", (2, 2), 200), 0.5), 0, 0) == 100
    assert turntable.mix((0, 0, 0), (100, 200, 50), 0.5) == (50, 100, 25)
    mask = Image.new("L", (2, 2), 255)
    tint = turntable.tint_layer((9, 8, 7), mask, 0.5)
    assert pixel(tint, 0, 0) == (9, 8, 7, 128)


def test_shrink_record_makes_a_square() -> None:
    assert turntable.shrink_record(Image.new("RGB", (80, 80), BLACK), 20).size == (20, 20)


def test_grooves_mask_has_rings_only_inside_the_groove_band() -> None:
    mask = turntable.grooves_mask()
    centre = int(turntable.RECORD_CENTRE)
    inner = record_up(turntable.GROOVE_BAND_INNER_PX - 8)
    assert level(mask, centre + inner, centre) == 0
    outer = record_up(turntable.GROOVE_BAND_OUTER_PX + 4)
    assert level(mask, centre + outer, centre) == 0
    samples = [
        level(mask, centre + record_up(radius), centre) for radius in range(turntable.GROOVE_BAND_INNER_PX + 10, 280)
    ]
    assert max(samples) > 200
    assert min(samples) < 60


def test_specks_mask_is_seeded() -> None:
    first = turntable.specks_mask(random.Random(3), 8)
    assert first.tobytes() == turntable.specks_mask(random.Random(3), 8).tobytes()
    assert first.getbbox() is not None


def test_smoke_mask_stays_inside_the_groove_band() -> None:
    mask = turntable.smoke_mask(random.Random(4))
    centre = int(turntable.RECORD_CENTRE)
    assert level(mask, centre, centre) == 0
    assert level(mask, 0, 0) == 0
    assert max(mask.tobytes()) > 0


def test_build_shine_is_faint_and_clipped_to_the_record() -> None:
    shine = turntable.build_shine()
    assert shine.size == (layout.VINYL_DIAMETER_PX, layout.VINYL_DIAMETER_PX)
    assert pixel(shine, 0, 0)[3] == 0
    assert 0 < max(shine.getchannel("A").tobytes()) <= 30


def test_build_shine_with_peg_has_an_opaque_spindle() -> None:
    layer = turntable.build_shine_with_peg()
    middle = layout.VINYL_RADIUS_PX
    assert pixel(layer, middle, middle)[3] == 255
    assert pixel(layer, 0, 0)[3] == 0


# ---------------------------------------------------------------- helpers: the tonearm


def test_draw_line_dot_and_headshell_paint_scaled_coordinates() -> None:
    size = 10 * turntable.FRAME_SUPERSAMPLE
    red = (200, 0, 0, 255)

    line = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    turntable.draw_line(line, [(1, 5), (9, 5)], red, 2, dx=0, dy=0)
    assert pixel(line, up(5), up(5)) == red
    assert pixel(line, up(5), up(1)) == (0, 0, 0, 0)

    dot = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    turntable.draw_dot(dot, 5, 5, 2, red)
    assert pixel(dot, up(5), up(5)) == red
    assert pixel(dot, up(9), up(9)) == (0, 0, 0, 0)

    shell = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    turntable.draw_headshell(shell, (5, 5), (0, 1), 6, 4, red)
    assert pixel(shell, up(5), up(7)) == red
    assert pixel(shell, up(5), up(9)) == (0, 0, 0, 0)
    assert pixel(shell, up(8), up(5)) == (0, 0, 0, 0)


def test_build_tonearm_is_a_frame_layer_with_the_pivot_opaque() -> None:
    arm = turntable.build_tonearm()
    assert arm.mode == "RGBA"
    assert arm.size == FRAME
    assert pixel(arm, *layout.TONEARM_PIVOT_PX)[3] == 255
    assert pixel(arm, 10, 10)[3] == 0


def test_even_bounds_grow_outwards_to_even_numbers_inside_the_frame() -> None:
    assert turntable.even_bounds((61, 99, 301, 401)) == (60, 98, 302, 402)
    assert turntable.even_bounds((60, 98, 300, 400)) == (60, 98, 300, 400)
    assert turntable.even_bounds((0, 0, 719, 1279)) == (0, 0, 720, 1280)
    assert turntable.even_bounds((0, 0, 721, 1281)) == (0, 0, 720, 1280)


def test_build_overlay_crops_to_what_it_draws(layers: turntable.TurntableLayers, overlay: Image.Image) -> None:
    drawn, offset = turntable.build_overlay()
    assert offset == (layers.overlay_x, layers.overlay_y)
    assert drawn.size == overlay.size
    box = drawn.getbbox()
    assert box is not None
    assert box[2] - box[0] >= drawn.width - 2
    assert box[3] - box[1] >= drawn.height - 2


def test_turntable_layers_is_frozen_and_so_hashable(layers: turntable.TurntableLayers) -> None:
    assert isinstance(hash(layers), int)
