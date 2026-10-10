"""The 720x1280 turntable frame: geometry and shared widths, constants only and no imports,
so the drawing, the subtitles and the filtergraph read one set of numbers.
"""

from __future__ import annotations

FRAME_WIDTH_PX = 720
FRAME_HEIGHT_PX = 1280
FRAME_CENTER_X_PX = 360

# left, top, right, bottom.
BODY_BOX_PX = (20, 28, 700, 1252)
BODY_CORNER_RADIUS_PX = 30

VINYL_CENTER_PX = (360, 398)
VINYL_DIAMETER_PX = 600
VINYL_RADIUS_PX = VINYL_DIAMETER_PX // 2
PLATTER_RADIUS_PX = VINYL_RADIUS_PX + 10
LABEL_DIAMETER_PX = 300

# where the square vinyl layer sits in the frame.
VINYL_OVERLAY_X_PX = VINYL_CENTER_PX[0] - VINYL_RADIUS_PX
VINYL_OVERLAY_Y_PX = VINYL_CENTER_PX[1] - VINYL_RADIUS_PX

TONEARM_PIVOT_PX = (652, 125)
NEEDLE_POINT_PX = (545, 552)
NEEDLE_TOLERANCE_PX = 2

DISPLAY_BOX_PX = (170, 724, 550, 794)
KNOB_CENTER_PX = (110, 759)
KNOB_RADIUS_PX = 34
SWITCH_CENTER_PX = (610, 759)
SCREEN_BOX_PX = (44, 842, 676, 1228)

GLASS_RGB = (5, 8, 11)

LYRICS_TEXT_WIDTH_PX = 576
DISPLAY_TEXT_WIDTH_PX = 340
