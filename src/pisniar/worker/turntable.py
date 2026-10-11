"""The turntable layers the render composes: Pillow draws them once per render from the
numbers in `layout` and fixed seeds, ffmpeg turns the vinyl. This is the only module that
imports PIL, and it opens no file and takes no text from the outside.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from pisniar.worker import layout

BACKGROUND_FILE_NAME = "bg.png"
VINYL_FILE_NAME = "vinyl.png"
LABEL_MASK_FILE_NAME = "label-mask.png"
HUB_FILE_NAME = "hub.png"
OVERLAY_FILE_NAME = "overlay.png"

WOOD_SEED = 11
VINYL_SEED = 20260918

FRAME_SUPERSAMPLE = 3
RECORD_SUPERSAMPLE = 4

Colour = tuple[int, int, int]
Box = tuple[int, int, int, int]

WOOD_RAMP = ((78, 46, 24), (150, 96, 52), (214, 156, 98))
VIGNETTE_STRENGTH = 0.55
SPEED_LABEL_RGB = (205, 205, 210)
SPEED_LABELS = ("33", "45")

BLACK_VINYL = (12, 12, 13)
RIM_COLOUR = (52, 52, 56)
RIM_WIDTH_PX = 3
GROOVE_PERIOD_PX = 6
GROOVE_BAND_INNER_PX = layout.LABEL_DIAMETER_PX // 2 + 10
GROOVE_BAND_OUTER_PX = layout.VINYL_RADIUS_PX - 12
SPINDLE_HOLE_DIAMETER_PX = 16
RECORD_SIZE_PX = layout.VINYL_DIAMETER_PX * RECORD_SUPERSAMPLE
RECORD_CENTRE = RECORD_SIZE_PX / 2

NEEDLE_ANGLE_DEGREES = 40
NEEDLE_RADIUS_FRACTION = 0.8


@dataclass(frozen=True)
class TurntableLayers:
    """where `write_layers` put the files, and where the overlay sits in the frame."""

    background: Path
    vinyl: Path
    label_mask: Path
    hub: Path
    overlay: Path
    overlay_x: int
    overlay_y: int
    overlay_width: int
    overlay_height: int


# ---------------------------------------------------------------- frame helpers (supersampled)


def up(value: float) -> float:
    return value * FRAME_SUPERSAMPLE


def frame_canvas() -> tuple[int, int]:
    return (layout.FRAME_WIDTH_PX * FRAME_SUPERSAMPLE, layout.FRAME_HEIGHT_PX * FRAME_SUPERSAMPLE)


def rounded_mask(
    box: Box,
    radius: float,
    blur: float = 0,
) -> Image.Image:
    mask = Image.new("L", frame_canvas(), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (up(box[0]), up(box[1]), up(box[2]), up(box[3])), radius=up(radius), fill=255
    )
    return mask.filter(ImageFilter.GaussianBlur(up(blur))) if blur else mask


def circle_mask(
    centre_x: float,
    centre_y: float,
    radius: float,
    blur: float = 0,
) -> Image.Image:
    mask = Image.new("L", frame_canvas(), 0)
    ImageDraw.Draw(mask).ellipse(
        (
            up(centre_x - radius),
            up(centre_y - radius),
            up(centre_x + radius),
            up(centre_y + radius),
        ),
        fill=255,
    )
    return mask.filter(ImageFilter.GaussianBlur(up(blur))) if blur else mask


def weakened(mask: Image.Image, strength: float) -> Image.Image:
    return mask.point(lambda value: round(value * strength))


def fill(
    image: Image.Image,
    mask: Image.Image,
    colour: Colour,
    strength: float = 1.0,
) -> Image.Image:
    return Image.composite(Image.new("RGB", image.size, colour), image, weakened(mask, strength))


def shift(
    mask: Image.Image,
    dx: float,
    dy: float,
) -> Image.Image:
    return ImageChops.offset(mask, round(up(dx)), round(up(dy)))


def shrink_frame(image: Image.Image) -> Image.Image:
    size = (image.width // FRAME_SUPERSAMPLE, image.height // FRAME_SUPERSAMPLE)
    return image.resize(size, Image.Resampling.LANCZOS)


def bezier(
    start: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
    end: tuple[float, float],
    steps: int = 80,
) -> list[tuple[float, float]]:
    points = []
    for index in range(steps + 1):
        t = index / steps
        u = 1 - t
        points.append(
            (
                u**3 * start[0] + 3 * u * u * t * first[0] + 3 * u * t * t * second[0] + t**3 * end[0],
                u**3 * start[1] + 3 * u * u * t * first[1] + 3 * u * t * t * second[1] + t**3 * end[1],
            )
        )
    return points


# ---------------------------------------------------------------- the scene background


def random_grid(
    rng: random.Random,
    columns: int,
    rows: int,
    size: tuple[int, int],
) -> Image.Image:
    data = bytes(rng.randrange(256) for _ in range(columns * rows))
    grid = Image.frombytes("L", (columns, rows), data)
    return grid.resize(size, Image.Resampling.BICUBIC)


def wood_texture(
    width: int,
    height: int,
    seed: int,
) -> Image.Image:
    """horizontal grain: grids at three scales, a slow sine warp of the rows, a 3-stop ramp."""
    rng = random.Random(seed)
    size = (width, height)
    fine = random_grid(rng, 5, 700, size)
    medium = random_grid(rng, 9, 150, size)
    coarse = random_grid(rng, 3, 28, size)
    grain = Image.blend(Image.blend(fine, medium, 0.55), coarse, 0.35)
    grain = ImageOps.autocontrast(grain, cutoff=1)
    warped = Image.new("L", size)
    for x in range(0, width, 6):
        offset = round(7 * math.sin(x / 95.0) + 4 * math.sin(x / 31.0 + 1.3))
        warped.paste(grain.crop((x, 0, x + 6, height)), (x, offset))
    return ImageOps.colorize(
        warped.filter(ImageFilter.GaussianBlur(0.6)),
        black=WOOD_RAMP[0],
        mid=WOOD_RAMP[1],
        white=WOOD_RAMP[2],
    )


def vignette(image: Image.Image, strength: float) -> Image.Image:
    centre_y = layout.FRAME_HEIGHT_PX // 2
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((-120, centre_y - 760, image.width + 120, centre_y + 760), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(160)).point(lambda value: 255 - value)
    return fill(image, mask, (0, 0, 0), strength)


def draw_body(image: Image.Image) -> Image.Image:
    left, top, right, bottom = layout.BODY_BOX_PX
    radius = layout.BODY_CORNER_RADIUS_PX
    image = fill(
        image,
        rounded_mask((left + 4, top + 12, right + 4, bottom + 16), radius, blur=14),
        (0, 0, 0),
        0.75,
    )
    body = rounded_mask(layout.BODY_BOX_PX, radius)
    ramp = (
        Image.linear_gradient("L").resize((frame_canvas()[0], round(up(bottom - top)))).point(lambda value: 255 - value)
    )
    gradient = Image.new("L", image.size, 0)
    gradient.paste(ramp, (0, round(up(top))))
    image = fill(image, body, (17, 17, 19))
    image = fill(image, ImageChops.multiply(body, gradient), (36, 36, 40), 0.9)
    inner = rounded_mask((left + 2, top + 2, right - 2, bottom - 2), radius - 2)
    edge = ImageChops.subtract(body, inner)
    return fill(image, ImageChops.multiply(edge, gradient), (92, 92, 98), 0.9)


def draw_platter(image: Image.Image) -> Image.Image:
    centre_x, centre_y = layout.VINYL_CENTER_PX
    radius = layout.PLATTER_RADIUS_PX
    image = fill(image, circle_mask(centre_x + 3, centre_y + 6, radius + 6, blur=7), (0, 0, 0), 0.7)
    image = fill(image, circle_mask(centre_x, centre_y, radius), (118, 118, 126))
    image = fill(image, circle_mask(centre_x, centre_y, radius - 3), (44, 44, 49))
    image = fill(image, circle_mask(centre_x, centre_y, radius - 7), (68, 68, 74))
    shadow = circle_mask(centre_x + 2, centre_y + 4, layout.VINYL_RADIUS_PX + 3, blur=4)
    return fill(image, shadow, (0, 0, 0), 0.55)


def draw_knob(image: Image.Image) -> Image.Image:
    centre_x, centre_y = layout.KNOB_CENTER_PX
    radius = layout.KNOB_RADIUS_PX
    image = fill(image, circle_mask(centre_x + 2, centre_y + 4, radius + 3, blur=4), (0, 0, 0), 0.8)
    rings = (
        (radius, (24, 24, 27)),
        (radius - 3, (150, 152, 158)),
        (radius - 5, (176, 178, 184)),
        (radius - radius * 0.4, (146, 148, 154)),
    )
    for ring_radius, colour in rings:
        image = fill(image, circle_mask(centre_x, centre_y, ring_radius), colour)
    highlight = circle_mask(centre_x - radius * 0.12, centre_y - radius * 0.16, radius * 0.45, blur=2)
    image = fill(image, highlight, (215, 217, 222), 0.7)
    ImageDraw.Draw(image).line(
        (
            up(centre_x),
            up(centre_y - radius * 0.12),
            up(centre_x),
            up(centre_y - radius + 5),
        ),
        fill=(30, 30, 33),
        width=round(up(3)),
    )
    return image


def draw_speed_switch(image: Image.Image) -> Image.Image:
    """the 33/45 slider; it is as wide as the knob, so the two mirror each other."""
    centre_x, centre_y = layout.SWITCH_CENTER_PX
    half = layout.KNOB_RADIUS_PX
    track = rounded_mask((centre_x - half, centre_y - 6, centre_x + half, centre_y + 6), 6)
    image = fill(image, track, (8, 8, 10))
    handle = rounded_mask((centre_x - half + 4, centre_y - 12, centre_x - half + 22, centre_y + 12), 4)
    image = fill(image, shift(handle, 1, 2), (0, 0, 0), 0.6)
    image = fill(image, handle, (168, 170, 176))
    grip = rounded_mask((centre_x - half + 6, centre_y - 10, centre_x - half + 20, centre_y - 3), 3)
    image = fill(image, grip, (205, 207, 212), 0.8)
    font = ImageFont.load_default(round(up(17)))
    draw = ImageDraw.Draw(image)
    slow, fast = SPEED_LABELS
    draw.text(
        (up(centre_x - half + 11), up(centre_y - 20)),
        slow,
        fill=SPEED_LABEL_RGB,
        font=font,
        anchor="ms",
    )
    draw.text(
        (up(centre_x + half - 11), up(centre_y - 20)),
        fast,
        fill=SPEED_LABEL_RGB,
        font=font,
        anchor="ms",
    )
    return image


def draw_display(image: Image.Image) -> Image.Image:
    """the title display: a bevelled glass pane with a faint reflection at its upper left."""
    left, top, right, bottom = layout.DISPLAY_BOX_PX
    shadow = rounded_mask((left - 1, top - 1, right + 1, bottom + 2), 9)
    image = fill(image, shadow, (0, 0, 0), 0.8)
    image = fill(image, rounded_mask((left, top, right, bottom + 1), 8), (66, 66, 72))
    image = fill(image, rounded_mask((left, top, right, bottom - 1), 8), (22, 22, 25))
    pane = rounded_mask((left + 3, top + 3, right - 3, bottom - 4), 5)
    image = fill(image, pane, layout.GLASS_RGB)
    reflection = Image.new("L", image.size, 0)
    ImageDraw.Draw(reflection).polygon(
        [
            (up(left + 4), up(top + 4)),
            (up(left + 170), up(top + 4)),
            (up(left + 110), up(top + 26)),
            (up(left + 4), up(top + 26)),
        ],
        fill=255,
    )
    reflection = reflection.filter(ImageFilter.GaussianBlur(up(1.5)))
    return fill(image, ImageChops.multiply(reflection, pane), (255, 255, 255), 0.07)


def draw_screen(image: Image.Image) -> Image.Image:
    """the lyrics screen: a larger glass pane that the subtitles are burnt into."""
    left, top, right, bottom = layout.SCREEN_BOX_PX
    shadow = rounded_mask((left - 1, top - 1, right + 1, bottom + 2), 15)
    image = fill(image, shadow, (0, 0, 0), 0.8)
    image = fill(image, rounded_mask((left, top, right, bottom + 1), 14), (66, 66, 72))
    image = fill(image, rounded_mask((left, top, right, bottom - 1), 14), (22, 22, 25))
    pane = rounded_mask((left + 4, top + 4, right - 4, bottom - 5), 10)
    image = fill(image, pane, layout.GLASS_RGB)
    width, height = right - left, bottom - top
    reflection = Image.new("L", image.size, 0)
    ImageDraw.Draw(reflection).polygon(
        [
            (up(left + 6), up(top + 6)),
            (up(left + 0.55 * width), up(top + 6)),
            (up(left + 0.38 * width), up(top + 0.30 * height)),
            (up(left + 6), up(top + 0.30 * height)),
        ],
        fill=255,
    )
    reflection = reflection.filter(ImageFilter.GaussianBlur(up(2)))
    return fill(image, ImageChops.multiply(reflection, pane), (255, 255, 255), 0.05)


def build_background() -> Image.Image:
    """RGB frame: wood, body, platter, knob, switch, the title display and the glass screen."""
    wood = vignette(wood_texture(layout.FRAME_WIDTH_PX, layout.FRAME_HEIGHT_PX, WOOD_SEED), VIGNETTE_STRENGTH)
    image = wood.resize(frame_canvas(), Image.Resampling.BICUBIC)
    image = draw_body(image)
    image = draw_platter(image)
    image = draw_knob(image)
    image = draw_speed_switch(image)
    image = draw_display(image)
    image = draw_screen(image)
    return shrink_frame(image)


# ---------------------------------------------------------------- the record (supersampled)


def record_up(value: float) -> float:
    return value * RECORD_SUPERSAMPLE


def disc_box(radius: float) -> tuple[float, float, float, float]:
    reach = record_up(radius)
    return (
        RECORD_CENTRE - reach,
        RECORD_CENTRE - reach,
        RECORD_CENTRE + reach,
        RECORD_CENTRE + reach,
    )


def filled_disc(radius: float) -> Image.Image:
    mask = Image.new("L", (RECORD_SIZE_PX, RECORD_SIZE_PX), 0)
    ImageDraw.Draw(mask).ellipse(disc_box(radius), fill=255)
    return mask


def annulus(
    inner: float,
    outer: float,
    softness: float = 0.0,
) -> Image.Image:
    mask = filled_disc(outer)
    ImageDraw.Draw(mask).ellipse(disc_box(inner), fill=0)
    return mask.filter(ImageFilter.GaussianBlur(record_up(softness))) if softness else mask


def mix(
    first: Colour,
    second: Colour,
    amount: float,
) -> Colour:
    return (
        round(first[0] + (second[0] - first[0]) * amount),
        round(first[1] + (second[1] - first[1]) * amount),
        round(first[2] + (second[2] - first[2]) * amount),
    )


def shrink_record(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.Resampling.LANCZOS)


def tint_layer(
    colour: Colour,
    mask: Image.Image,
    strength: float,
) -> Image.Image:
    layer = Image.new("RGBA", mask.size, (*colour, 0))
    layer.putalpha(weakened(mask, strength))
    return layer


def grooves_mask() -> Image.Image:
    """concentric rings with two smooth gaps: the look of a record, never the motion."""
    mask = Image.new("L", (RECORD_SIZE_PX, RECORD_SIZE_PX), 0)
    draw = ImageDraw.Draw(mask)
    period = record_up(GROOVE_PERIOD_PX)
    radius = record_up(GROOVE_BAND_OUTER_PX)
    while radius > record_up(GROOVE_BAND_INNER_PX):
        box = (
            RECORD_CENTRE - radius,
            RECORD_CENTRE - radius,
            RECORD_CENTRE + radius,
            RECORD_CENTRE + radius,
        )
        draw.ellipse(box, outline=255, width=round(period / 2))
        radius -= period
    span = GROOVE_BAND_OUTER_PX - GROOVE_BAND_INNER_PX
    for fraction, gap_width in ((0.66, 5), (0.37, 4)):
        gap_centre = GROOVE_BAND_INNER_PX + fraction * span
        draw.ellipse(disc_box(gap_centre + gap_width), outline=0, width=round(record_up(gap_width * 2)))
    return mask.filter(ImageFilter.GaussianBlur(period * 0.16))


def specks_mask(rng: random.Random, count: int) -> Image.Image:
    mask = Image.new("L", (RECORD_SIZE_PX, RECORD_SIZE_PX), 0)
    draw = ImageDraw.Draw(mask)
    for _ in range(count):
        angle = rng.uniform(0, 2 * math.pi)
        radius = rng.uniform(GROOVE_BAND_INNER_PX, GROOVE_BAND_OUTER_PX)
        size = record_up(rng.uniform(0.9, 2.2))
        x = RECORD_CENTRE + record_up(radius) * math.cos(angle)
        y = RECORD_CENTRE + record_up(radius) * math.sin(angle)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(record_up(0.4)))


def smoke_mask(rng: random.Random) -> Image.Image:
    """marbled smoke, two seeded octaves kept to the groove band: low in frequency and
    uneven around the disc, so the turning shows."""
    octaves = []
    for cells in (22, 8):
        data = bytes(rng.randrange(256) for _ in range(cells * cells))
        grid = Image.frombytes("L", (cells, cells), data)
        octaves.append(grid.resize((RECORD_SIZE_PX, RECORD_SIZE_PX), Image.Resampling.BICUBIC))
    field = Image.blend(octaves[0], octaves[1], 0.55)
    field = field.point(lambda value: max(0, min(255, round((value - 105) * 3.2))))
    field = field.filter(ImageFilter.GaussianBlur(record_up(6)))
    band = annulus(GROOVE_BAND_INNER_PX - 6, layout.VINYL_RADIUS_PX - 4, 6)
    return ImageChops.multiply(field, band)


def build_vinyl() -> Image.Image:
    """RGBA record: smoke, grooves, rim. Outside the circle alpha is 0 and RGB is the rim colour."""
    rng = random.Random(VINYL_SEED)
    base = BLACK_VINYL
    light = mix(base, (255, 255, 255), 0.22)
    canvas = Image.new("RGB", (RECORD_SIZE_PX, RECORD_SIZE_PX), RIM_COLOUR)
    canvas = fill(canvas, filled_disc(layout.VINYL_RADIUS_PX - RIM_WIDTH_PX), base)
    band = annulus(GROOVE_BAND_INNER_PX - 4, GROOVE_BAND_OUTER_PX + 4, 3)
    canvas = fill(canvas, smoke_mask(rng), mix(base, (170, 172, 184), 0.42), 0.50)
    canvas = fill(canvas, ImageChops.multiply(grooves_mask(), band), light, 0.20)
    canvas = fill(canvas, specks_mask(rng, 16), mix(base, (215, 218, 235), 0.75), 0.55)
    lead_in = annulus(GROOVE_BAND_OUTER_PX + 4, layout.VINYL_RADIUS_PX - RIM_WIDTH_PX, 2)
    canvas = fill(canvas, lead_in, mix(base, (0, 0, 0), 0.35), 0.7)
    vinyl = shrink_record(canvas, layout.VINYL_DIAMETER_PX).convert("RGBA")
    vinyl.putalpha(shrink_record(filled_disc(layout.VINYL_RADIUS_PX), layout.VINYL_DIAMETER_PX))
    return vinyl


def build_hub() -> Image.Image:
    """RGBA, over the label: the seat shadow ring and the spindle hole."""
    seat = layout.LABEL_DIAMETER_PX / 2
    layer = Image.new("RGBA", (RECORD_SIZE_PX, RECORD_SIZE_PX), (0, 0, 0, 0))
    layer = Image.alpha_composite(layer, tint_layer((0, 0, 0), annulus(seat - 1, seat + 3.5, 0.8), 0.60))
    hole = SPINDLE_HOLE_DIAMETER_PX / 2
    layer = Image.alpha_composite(layer, tint_layer((0, 0, 0), annulus(hole, hole + 3, 1), 0.40))
    ImageDraw.Draw(layer).ellipse(disc_box(hole), fill=(0, 0, 0, 255))
    return shrink_record(layer, layout.VINYL_DIAMETER_PX)


def build_label_mask() -> Image.Image:
    """L, the circle the cover is merged into."""
    size = layout.LABEL_DIAMETER_PX * RECORD_SUPERSAMPLE
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask.resize((layout.LABEL_DIAMETER_PX,) * 2, Image.Resampling.LANCZOS)


def build_shine() -> Image.Image:
    """the stationary reflection: two soft diagonal bands clipped to the record, strength 0.10."""
    band = Image.new("L", (RECORD_SIZE_PX, RECORD_SIZE_PX), 0)
    draw = ImageDraw.Draw(band)
    scale = layout.VINYL_DIAMETER_PX / 680
    angle = math.radians(-35)
    across = (math.cos(angle + math.pi / 2), math.sin(angle + math.pi / 2))
    along = (math.cos(angle), math.sin(angle))
    for offset, width, level in ((-150 * scale, 70 * scale, 255), (120 * scale, 26 * scale, 150)):
        centre_x = RECORD_CENTRE + record_up(offset) * across[0]
        centre_y = RECORD_CENTRE + record_up(offset) * across[1]
        half_width, length = record_up(width), record_up(500)
        draw.polygon(
            [
                (
                    centre_x + along[0] * length + across[0] * half_width,
                    centre_y + along[1] * length + across[1] * half_width,
                ),
                (
                    centre_x + along[0] * length - across[0] * half_width,
                    centre_y + along[1] * length - across[1] * half_width,
                ),
                (
                    centre_x - along[0] * length - across[0] * half_width,
                    centre_y - along[1] * length - across[1] * half_width,
                ),
                (
                    centre_x - along[0] * length + across[0] * half_width,
                    centre_y - along[1] * length + across[1] * half_width,
                ),
            ],
            fill=level,
        )
    band = band.filter(ImageFilter.GaussianBlur(record_up(22 * scale)))
    band = ImageChops.multiply(band, filled_disc(layout.VINYL_RADIUS_PX - 2))
    return shrink_record(tint_layer((255, 255, 255), band, 0.10), layout.VINYL_DIAMETER_PX)


def build_shine_with_peg() -> Image.Image:
    layer = build_shine()
    peg = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    centre, radius = layout.VINYL_DIAMETER_PX / 2, 5
    draw = ImageDraw.Draw(peg)
    draw.ellipse(
        (centre - radius, centre - radius, centre + radius, centre + radius),
        fill=(150, 152, 158, 255),
    )
    draw.ellipse(
        (centre - radius + 1.5, centre - radius + 1, centre + radius - 2, centre + radius - 2.5),
        fill=(225, 227, 232, 255),
    )
    return Image.alpha_composite(layer, peg)


# ---------------------------------------------------------------- the tonearm and the overlay


def draw_line(
    layer: Image.Image,
    points: list[tuple[float, float]],
    colour: tuple[int, int, int, int],
    width: float,
    dx: float = 0.0,
    dy: float = 0.0,
) -> None:
    ImageDraw.Draw(layer).line(
        [(up(x + dx), up(y + dy)) for x, y in points],
        fill=colour,
        width=round(up(width)),
        joint="curve",
    )


def draw_dot(
    layer: Image.Image,
    x: float,
    y: float,
    radius: float,
    colour: tuple[int, int, int, int],
) -> None:
    ImageDraw.Draw(layer).ellipse((up(x - radius), up(y - radius), up(x + radius), up(y + radius)), fill=colour)


def draw_headshell(
    layer: Image.Image,
    centre: tuple[float, float],
    direction: tuple[float, float],
    length: float,
    width: float,
    colour: tuple[int, int, int, int],
) -> None:
    """a rectangle along `direction`."""
    x, y = centre
    dx, dy = direction
    nx, ny = -dy, dx
    corners = [
        (x - dx * length / 2 + nx * width / 2, y - dy * length / 2 + ny * width / 2),
        (x + dx * length / 2 + nx * width / 2, y + dy * length / 2 + ny * width / 2),
        (x + dx * length / 2 - nx * width / 2, y + dy * length / 2 - ny * width / 2),
        (x - dx * length / 2 - nx * width / 2, y - dy * length / 2 - ny * width / 2),
    ]
    ImageDraw.Draw(layer).polygon([(up(a), up(b)) for a, b in corners], fill=colour)


def build_tonearm() -> Image.Image:
    """RGBA frame layer: an S-shaped arm from the pivot to a needle on the record, with shadow."""
    pivot_x, pivot_y = layout.TONEARM_PIVOT_PX
    centre_x, centre_y = layout.VINYL_CENTER_PX
    angle = math.radians(NEEDLE_ANGLE_DEGREES)
    reach = NEEDLE_RADIUS_FRACTION * layout.VINYL_RADIUS_PX
    target = (centre_x + reach * math.cos(angle), centre_y + reach * math.sin(angle))
    end = (target[0] + 4, target[1] - 44)
    span = end[1] - pivot_y
    path = bezier(
        (pivot_x, pivot_y),
        (pivot_x + 50, pivot_y + 0.2 * span),
        (end[0] + 5, end[1] - 0.4 * span),
        end,
    )
    tx, ty = path[-1][0] - path[-4][0], path[-1][1] - path[-4][1]
    norm = math.hypot(tx, ty)
    direction = (tx / norm, ty / norm)
    tip = (end[0] + direction[0] * 44, end[1] + direction[1] * 44)
    bx, by = path[0][0] - path[3][0], path[0][1] - path[3][1]
    back = math.hypot(bx, by)
    weight = (pivot_x + bx / back * 48, pivot_y + by / back * 48)

    def headshell_centre(
        distance: float,
        dx: float = 0.0,
        dy: float = 0.0,
    ) -> tuple[float, float]:
        return (end[0] + direction[0] * distance + dx, end[1] + direction[1] * distance + dy)

    shadow = Image.new("RGBA", frame_canvas(), (0, 0, 0, 0))
    black = (0, 0, 0, 255)
    draw_line(shadow, path, black, 11, 8, 12)
    draw_dot(shadow, weight[0] + 8, weight[1] + 12, 19, black)
    draw_dot(shadow, pivot_x + 8, pivot_y + 12, 32, black)
    draw_headshell(shadow, headshell_centre(22, 8, 12), direction, 44, 26, black)
    softened = shadow.getchannel("A").filter(ImageFilter.GaussianBlur(up(5)))
    shadow.putalpha(softened.point(lambda value: round(value * 0.5)))

    arm = Image.new("RGBA", frame_canvas(), (0, 0, 0, 0))
    draw_line(arm, [weight, (pivot_x, pivot_y)], (46, 46, 50, 255), 3.5)
    for radius, colour in (
        (19, (40, 40, 44, 255)),
        (16, (170, 172, 178, 255)),
        (11, (132, 134, 140, 255)),
    ):
        draw_dot(arm, weight[0], weight[1], radius, colour)
    draw_dot(arm, weight[0] - 3, weight[1] - 4, 5, (222, 224, 228, 200))
    draw_line(arm, path, (120, 122, 128, 255), 10)
    draw_line(arm, path, (176, 179, 186, 255), 7)
    draw_line(arm, path, (232, 234, 238, 255), 2.6, -1.4, -1.4)
    for radius, colour in (
        (33, (38, 38, 42, 255)),
        (29, (112, 114, 120, 255)),
        (22, (170, 172, 178, 255)),
        (12, (60, 60, 66, 255)),
    ):
        draw_dot(arm, pivot_x, pivot_y, radius, colour)
    draw_dot(arm, pivot_x - 4, pivot_y - 5, 6, (225, 227, 232, 180))
    draw_headshell(arm, headshell_centre(22), direction, 44, 25, (22, 22, 25, 255))
    draw_headshell(arm, headshell_centre(20), direction, 30, 15, (46, 46, 52, 255))
    draw_dot(arm, tip[0], tip[1], 2.2, (230, 230, 235, 255))
    return shrink_frame(Image.alpha_composite(shadow, arm))


def even_bounds(box: Box) -> Box:
    """the box grown outwards to even coordinates inside the frame."""
    return (
        box[0] // 2 * 2,
        box[1] // 2 * 2,
        min(-(-box[2] // 2) * 2, layout.FRAME_WIDTH_PX),
        min(-(-box[3] // 2) * 2, layout.FRAME_HEIGHT_PX),
    )


def build_overlay() -> tuple[Image.Image, tuple[int, int]]:
    """the stationary RGBA layer over the turning record, cropped to what it draws, with its offset:
    the reflection and the spindle peg, then the tonearm and its shadow."""
    frame = Image.new("RGBA", (layout.FRAME_WIDTH_PX, layout.FRAME_HEIGHT_PX), (0, 0, 0, 0))
    frame.alpha_composite(build_shine_with_peg(), (layout.VINYL_OVERLAY_X_PX, layout.VINYL_OVERLAY_Y_PX))
    frame = Image.alpha_composite(frame, build_tonearm())
    drawn = frame.getbbox()
    if drawn is None:
        raise ValueError("the overlay is empty")
    bounds = even_bounds(drawn)
    return frame.crop(bounds), (bounds[0], bounds[1])


def write_layers(directory: Path) -> TurntableLayers:
    """draws the five layers into `directory` under fixed names, replacing earlier files."""
    layers_overlay, offset = build_overlay()
    paths = TurntableLayers(
        background=directory / BACKGROUND_FILE_NAME,
        vinyl=directory / VINYL_FILE_NAME,
        label_mask=directory / LABEL_MASK_FILE_NAME,
        hub=directory / HUB_FILE_NAME,
        overlay=directory / OVERLAY_FILE_NAME,
        overlay_x=offset[0],
        overlay_y=offset[1],
        overlay_width=layers_overlay.width,
        overlay_height=layers_overlay.height,
    )
    build_background().save(paths.background, format="PNG")
    build_vinyl().save(paths.vinyl, format="PNG")
    build_label_mask().save(paths.label_mask, format="PNG")
    build_hub().save(paths.hub, format="PNG")
    layers_overlay.save(paths.overlay, format="PNG")
    return paths
