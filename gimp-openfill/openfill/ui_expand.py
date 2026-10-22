# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""The visual canvas editor of the Generative Expand dialog.

The top half of this module is plain Python (no ``gi``) and unit tested:
the margins model, aspect-ratio presets, the 3x3 anchor, the on-screen
layout of the thumbnail and the new-canvas frame, hit-testing of the eight
handles and the drag math. The bottom half is the GTK widget
(:class:`ExpandEditor`, a ``Gtk.DrawingArea``) that only draws that state
and turns pointer events into calls of the pure functions. It is defined
only when GTK can be imported.

The procedure keeps its arguments (left, top, right, bottom in pixels); a
negative value crops that side, so scripts that only add space are not affected.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from . import geometry

MAX_MARGIN = geometry.EXPAND_LIMIT  # the range (+-) of the left/top/right/bottom procedure arguments
MIN_FRAME = geometry.EXPAND_MIN_KEEP  # least part of the photo that stays inside the frame (px)
SNAP = 8  # dragging snaps to multiples of this many image pixels
ORIGINAL_GROW = 0.25  # "Original" (or a preset equal to the image ratio) grows each side of the canvas by 25 %
THUMB_PX = 360  # the image thumbnail fits in this many screen pixels
PAD = 14  # screen pixels between the new-canvas frame and the widget edge
HANDLE_PX = 9  # side of a drawn handle square
HIT_PX = 9  # how far from a handle or edge the pointer may be to grab it

SIDES = ("left", "top", "right", "bottom")

# (id, label, ratio) - ratio None: "original" uses the image ratio, "custom" keeps the margins.
PRESETS: tuple[tuple[str, str, tuple[int, int] | None], ...] = (
    ("original", "Original", None),
    ("1:1", "1:1", (1, 1)),
    ("5:4", "5:4", (5, 4)),
    ("4:5", "4:5", (4, 5)),
    ("4:3", "4:3", (4, 3)),
    ("3:4", "3:4", (3, 4)),
    ("3:2", "3:2", (3, 2)),
    ("2:3", "2:3", (2, 3)),
    ("16:9", "16:9", (16, 9)),
    ("9:16", "9:16", (9, 16)),
    ("custom", "Custom", None),
)
PRESET_IDS = tuple(p[0] for p in PRESETS)

# The ratio buttons show each shape once (landscape id); the Landscape/Portrait
# switch picks which way round it is applied.
RATIO_BUTTONS: tuple[tuple[str, str], ...] = (
    ("original", "Original"),
    ("1:1", "1:1"),
    ("5:4", "5:4"),
    ("4:3", "4:3"),
    ("3:2", "3:2"),
    ("16:9", "16:9"),
    ("custom", "Custom"),
)


def has_orientation(preset: str) -> bool:
    """True for a ratio that differs between landscape and portrait."""
    a, sep, b = preset.partition(":")
    return bool(sep) and a != b


def oriented(button: str, portrait: bool) -> str:
    """The preset id for a ratio button turned to ``portrait`` or landscape."""
    if not has_orientation(button):
        return button
    a, _, b = button.partition(":")
    hi, lo = (a, b) if int(a) >= int(b) else (b, a)
    return f"{lo}:{hi}" if portrait else f"{hi}:{lo}"


def ratio_button(preset: str) -> str:
    """The ratio button that shows ``preset`` (its landscape id)."""
    return oriented(preset, False)


def is_portrait(preset: str) -> bool | None:
    """Whether ``preset`` is the portrait form; None when it has no orientation."""
    if not has_orientation(preset):
        return None
    a, _, b = preset.partition(":")
    return int(a) < int(b)


# Anchor = where the old image sits in the new canvas, as (column, row) 0..2.
ANCHORS: dict[str, tuple[int, int]] = {
    "top-left": (0, 0),
    "top": (1, 0),
    "top-right": (2, 0),
    "left": (0, 1),
    "center": (1, 1),
    "right": (2, 1),
    "bottom-left": (0, 2),
    "bottom": (1, 2),
    "bottom-right": (2, 2),
}
ANCHOR_AT = {pos: name for name, pos in ANCHORS.items()}

MOVE_CURSOR = "move"  # inside the frame: drag to move it

HANDLES = ("top-left", "top", "top-right", "right", "bottom-right", "bottom", "bottom-left", "left")
# CSS cursor names (Gdk.Cursor.new_from_name) for each handle.
CURSORS = {
    "left": "w-resize",
    "right": "e-resize",
    "top": "n-resize",
    "bottom": "s-resize",
    "top-left": "nw-resize",
    "top-right": "ne-resize",
    "bottom-left": "sw-resize",
    "bottom-right": "se-resize",
}


# ------------------------------------------------------------- margins ----


@dataclass(frozen=True)
class Margins:
    """Pixels added on each side of the image; negative values crop that side."""

    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @classmethod
    def from_dict(cls, values: dict, limit: int = MAX_MARGIN) -> Margins:
        """From a dict with left/top/right/bottom (missing or junk values are 0), clamped to +-limit."""
        out = {}
        for side in SIDES:
            try:
                out[side] = clamp_margin(int(values.get(side, 0) or 0), limit)
            except (TypeError, ValueError):
                out[side] = 0
        return cls(**out)

    def as_dict(self) -> dict[str, int]:
        return {side: getattr(self, side) for side in SIDES}

    def canvas(self, image_w: int, image_h: int) -> tuple[int, int]:
        """The new canvas size."""
        return image_w + self.left + self.right, image_h + self.top + self.bottom

    def crop(self) -> tuple[int, int, int, int]:
        """Pixels cut off (left, top, right, bottom)."""
        return geometry.split_crop_grow((self.left, self.top, self.right, self.bottom))[0]

    def grow(self) -> tuple[int, int, int, int]:
        """Pixels added (left, top, right, bottom)."""
        return geometry.split_crop_grow((self.left, self.top, self.right, self.bottom))[1]

    def is_empty(self) -> bool:
        return not (self.left or self.top or self.right or self.bottom)

    def replace(self, **sides: int) -> Margins:
        values = self.as_dict()
        values.update(sides)
        return Margins(**values)


def clamp_margin(value: float, limit: int = MAX_MARGIN) -> int:
    """A margin as a whole number of pixels between -``limit`` (crop) and ``limit``."""
    if value != value:  # NaN
        return 0
    return int(max(-limit, min(limit, math.floor(value + 0.5))))


def clamp_margins(margins: Margins, limit: int = MAX_MARGIN) -> Margins:
    return Margins(*(clamp_margin(getattr(margins, s), limit) for s in SIDES))


def fit_margins(
    margins: Margins, image_w: int, image_h: int, previous: Margins | None = None, limit: int = MAX_MARGIN
) -> Margins:
    """``margins`` limited to +-``limit`` and so that at least ``MIN_FRAME`` px of
    the photo stay in the frame on each axis (the sides that changed give way first)."""
    prev = None if previous is None else tuple(getattr(previous, s) for s in SIDES)
    values = geometry.fit_expand_margins(
        tuple(getattr(margins, s) for s in SIDES), image_w, image_h, prev, limit, MIN_FRAME
    )
    return Margins(*values)


def side_range(margins: Margins, side: str, image_w: int, image_h: int, limit: int = MAX_MARGIN) -> tuple[int, int]:
    """(lowest, highest) value one side may take with the others as they are."""
    pairs = {
        "left": (image_w, "right"),
        "right": (image_w, "left"),
        "top": (image_h, "bottom"),
        "bottom": (image_h, "top"),
    }
    if side not in pairs:
        return 0, 0
    extent, other = pairs[side]
    keep = min(MIN_FRAME, max(1, extent))
    lowest = -(extent - max(0, -getattr(margins, other)) - keep)
    return max(-limit, min(0, lowest)), limit


# ------------------------------------------------------ presets/anchor ----


def split(extra: int, index: int) -> tuple[int, int]:
    """Split ``extra`` pixels (negative: the frame is smaller than the image)
    into (before, after) for anchor column/row ``index``
    (0: image at the start, all the space after it; 1: centred; 2: at the end)."""
    extra = int(extra)
    if index <= 0:
        return 0, extra
    if index >= 2:
        return extra, 0
    before = extra // 2
    return before, extra - before


def preset_ratio(preset: str, image_w: int, image_h: int) -> tuple[int, int] | None:
    """The (width, height) ratio of ``preset``; None for "custom" or an unknown id."""
    if preset == "original":
        return (max(1, image_w), max(1, image_h))
    for pid, _label, ratio in PRESETS:
        if pid == preset:
            return ratio
    return None


def target_canvas(image_w: int, image_h: int, ratio: tuple[int, int], grow: float = ORIGINAL_GROW) -> tuple[int, int]:
    """The smallest canvas with ``ratio`` that contains the image. When the
    image already has that ratio, the canvas grows by ``grow`` on both axes
    (the ratio is kept), so a preset always adds some space."""
    rw, rh = ratio
    if image_w * rh < image_h * rw:  # image narrower than the ratio: add width
        width = math.ceil(image_h * rw / rh - 1e-9)
        if width - image_w >= 1:
            return width, image_h
    elif image_w * rh > image_h * rw:  # image wider: add height
        height = math.ceil(image_w * rh / rw - 1e-9)
        if height - image_h >= 1:
            return image_w, height
    width = image_w + max(1, round(image_w * grow))
    return width, max(image_h, round(width * image_h / image_w))


def anchor_margins(
    image_w: int, image_h: int, canvas_w: int, canvas_h: int, anchor: str, limit: int = MAX_MARGIN
) -> Margins:
    """Margins that place the image at ``anchor`` in a ``canvas_w`` x ``canvas_h`` canvas."""
    col, row = ANCHORS.get(anchor, (1, 1))
    left, right = split(canvas_w - image_w, col)
    top, bottom = split(canvas_h - image_h, row)
    return clamp_margins(Margins(left, top, right, bottom), limit)


def preset_margins(
    image_w: int, image_h: int, preset: str, anchor: str = "center", limit: int = MAX_MARGIN
) -> Margins | None:
    """Margins for an aspect-ratio preset and anchor; None for "custom"."""
    ratio = preset_ratio(preset, image_w, image_h)
    if ratio is None or image_w <= 0 or image_h <= 0:
        return None
    canvas_w, canvas_h = target_canvas(image_w, image_h, ratio)
    return anchor_margins(image_w, image_h, canvas_w, canvas_h, anchor, limit)


def reanchor(margins: Margins, anchor: str, limit: int = MAX_MARGIN) -> Margins:
    """Keep the added width and height, move the image to ``anchor``."""
    col, row = ANCHORS.get(anchor, (1, 1))
    left, right = split(margins.left + margins.right, col)
    top, bottom = split(margins.top + margins.bottom, row)
    return clamp_margins(Margins(left, top, right, bottom), limit)


def anchor_of(margins: Margins, fallback: str = "center") -> str:
    """The anchor that describes ``margins``. An axis without added space
    keeps the column or row of ``fallback``."""
    fcol, frow = ANCHORS.get(fallback, (1, 1))

    def index(before: int, after: int, keep: int) -> int:
        if before == 0 and after == 0:
            return keep
        if before == 0:
            return 0
        if after == 0:
            return 2
        return 1

    col = index(margins.left, margins.right, fcol)
    row = index(margins.top, margins.bottom, frow)
    return ANCHOR_AT[(col, row)]


def matching_preset(image_w: int, image_h: int, margins: Margins, _anchor: str = "") -> str:
    """The preset whose canvas has exactly the size ``margins`` give (wherever
    the frame sits), else "custom"."""
    size = margins.canvas(image_w, image_h)
    for pid in PRESET_IDS:
        ratio = preset_ratio(pid, image_w, image_h) if pid != "custom" else None
        if ratio is not None and target_canvas(image_w, image_h, ratio) == size:
            return pid
    return "custom"


# -------------------------------------------------------------- frame -----


def frame_rect(margins: Margins, image_w: int, image_h: int) -> tuple[int, int, int, int]:
    """The frame (x, y, width, height) in image coordinates."""
    return -margins.left, -margins.top, *margins.canvas(image_w, image_h)


def margins_of_frame(x: int, y: int, width: int, height: int, image_w: int, image_h: int) -> Margins:
    return Margins(-x, -y, x + width - image_w, y + height - image_h)


def _position_range(size: int, extent: int, limit: int) -> tuple[int, int]:
    """Where the start of a frame of ``size`` may be along an axis of ``extent``."""
    keep = min(MIN_FRAME, extent, size)
    lowest = max(keep - size, -limit, extent - size - limit)
    highest = min(extent - keep, limit, extent - size + limit)
    return lowest, highest


CENTER_MAGNET_PX = 12  # an edge drag snaps to the centre from this many screen pixels away
CENTER_PULL = 0.06  # the frame snaps to the centre from this far away (share of the photo side)


def _magnet(value: float, edges: tuple[int, ...], step: int, center: int | None = None, pull: int = 0) -> int:
    """``value`` snapped to the centre when within ``pull`` of it, to the photo
    edges when within ``step`` of one, else to a multiple of ``step``."""
    if center is not None and abs(value - center) <= max(step, pull):
        return center
    for edge in edges:
        if abs(value - edge) <= step:
            return edge
    return snap(value, step)


def move_frame(
    start: Margins, dx: float, dy: float, image_w: int, image_h: int, limit: int = MAX_MARGIN, step: int = SNAP
) -> Margins:
    """The frame moved by (dx, dy) image pixels with its size kept, snapped to
    ``step`` and kept overlapping the photo."""
    x, y, width, height = frame_rect(start, image_w, image_h)
    out = []
    for pos, delta, size, extent in ((x, dx, width, image_w), (y, dy, height, image_h)):
        lowest, highest = _position_range(size, extent, limit)
        center = round((extent - size) / 2)
        pull = round(extent * CENTER_PULL)
        new = _magnet(pos + delta, (0, extent - size), step, center, pull) if delta else pos
        out.append(new if lowest > highest else max(lowest, min(highest, new)))
    return margins_of_frame(out[0], out[1], width, height, image_w, image_h)


def frame_valid(margins: Margins, image_w: int, image_h: int, limit: int = MAX_MARGIN) -> bool:
    return fit_margins(margins, image_w, image_h, None, limit) == margins


def resize_locked(
    start: Margins,
    corner: str,
    dx: float,
    dy: float,
    ratio: tuple[int, int],
    image_w: int,
    image_h: int,
    limit: int = MAX_MARGIN,
    step: int = SNAP,
) -> Margins:
    """Drag a corner of the frame by (dx, dy) image pixels with the ratio kept:
    the opposite corner stays, the width follows the larger movement and
    snaps to ``step``. A result that leaves the allowed range is shortened."""
    if "-" not in corner or corner not in CURSORS:
        return start
    rw, rh = ratio
    x, y, width, height = frame_rect(start, image_w, image_h)
    sx = 1 if "right" in corner else -1
    sy = 1 if "bottom" in corner else -1
    w_by_x = width + sx * dx
    w_by_y = (height + sy * dy) * rw / rh
    new_w = w_by_x if abs(w_by_x - width) >= abs(w_by_y - width) else w_by_y
    least = math.ceil(MIN_FRAME * max(1.0, rw / rh))
    target = max(least, snap(new_w, step))
    # the wanted width, or the nearest one towards the current width that is allowed
    back = 1 if width > target else -1
    for candidate_w in range(target, width + back, back * step):
        new_h = max(1, round(candidate_w * rh / rw))
        nx = x if sx > 0 else x + width - candidate_w
        ny = y if sy > 0 else y + height - new_h
        candidate = margins_of_frame(nx, ny, candidate_w, new_h, image_w, image_h)
        if candidate_w >= least and frame_valid(candidate, image_w, image_h, limit):
            return candidate
    return start


# ------------------------------------------------------------- layout -----


@dataclass(frozen=True)
class View:
    """Screen geometry: ``scale`` screen px per image px, the image rectangle
    (ix, iy, iw, ih) and the new-canvas frame (fx, fy, fw, fh)."""

    scale: float
    ix: float
    iy: float
    iw: float
    ih: float
    fx: float
    fy: float
    fw: float
    fh: float


def fit_view(
    area_w: int,
    area_h: int,
    image_w: int,
    image_h: int,
    margins: Margins,
    thumb_px: int = THUMB_PX,
    pad: int = PAD,
) -> View:
    """Scale so the image fits ``thumb_px`` and the photo and the new-canvas
    frame (which a crop makes smaller than the photo) fit the area (minus
    ``pad``), and centre both in the area."""
    image_w, image_h = max(1, image_w), max(1, image_h)
    x0, x1 = min(0, -margins.left), max(image_w, image_w + margins.right)
    y0, y1 = min(0, -margins.top), max(image_h, image_h + margins.bottom)
    room_w, room_h = max(1, area_w - 2 * pad), max(1, area_h - 2 * pad)
    scale = min(thumb_px / max(image_w, image_h), room_w / (x1 - x0), room_h / (y1 - y0))
    ix = (area_w - (x1 - x0) * scale) / 2 - x0 * scale
    iy = (area_h - (y1 - y0) * scale) / 2 - y0 * scale
    return view_at(scale, ix, iy, image_w, image_h, margins)


def view_at(scale: float, ix: float, iy: float, image_w: int, image_h: int, margins: Margins) -> View:
    """The view with a fixed scale and image position (used while dragging,
    so the image does not move under the pointer)."""
    return View(
        scale,
        ix,
        iy,
        image_w * scale,
        image_h * scale,
        ix - margins.left * scale,
        iy - margins.top * scale,
        (image_w + margins.left + margins.right) * scale,
        (image_h + margins.top + margins.bottom) * scale,
    )


def handle_points(view: View) -> dict[str, tuple[float, float]]:
    """Centre of each of the eight handles on the frame."""
    x0, y0 = view.fx, view.fy
    x1, y1 = view.fx + view.fw, view.fy + view.fh
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    return {
        "top-left": (x0, y0),
        "top": (xm, y0),
        "top-right": (x1, y0),
        "right": (x1, ym),
        "bottom-right": (x1, y1),
        "bottom": (xm, y1),
        "bottom-left": (x0, y1),
        "left": (x0, ym),
    }


def hit_test(view: View, x: float, y: float, tolerance: float = HIT_PX) -> str | None:
    """The handle under the pointer: a corner, else an edge (anywhere along
    it), else None. Corners win over edges so small frames stay usable."""
    best, best_d = None, None
    for name, (hx, hy) in handle_points(view).items():
        if "-" not in name:
            continue
        d = max(abs(x - hx), abs(y - hy))
        if d <= tolerance and (best_d is None or d < best_d):
            best, best_d = name, d
    if best:
        return best
    x0, y0 = view.fx, view.fy
    x1, y1 = view.fx + view.fw, view.fy + view.fh
    within_y = y0 - tolerance <= y <= y1 + tolerance
    within_x = x0 - tolerance <= x <= x1 + tolerance
    candidates = []
    if within_y:
        candidates += [(abs(x - x0), "left"), (abs(x - x1), "right")]
    if within_x:
        candidates += [(abs(y - y0), "top"), (abs(y - y1), "bottom")]
    candidates = [c for c in candidates if c[0] <= tolerance]
    return min(candidates)[1] if candidates else None


def hit_region(view: View, x: float, y: float, tolerance: float = HIT_PX) -> str | None:
    """Like :func:`hit_test`, and "move" for a point inside the frame."""
    handle = hit_test(view, x, y, tolerance)
    if handle:
        return handle
    if view.fx <= x <= view.fx + view.fw and view.fy <= y <= view.fy + view.fh:
        return "move"
    return None


def crop_strips(view: View) -> list[tuple[float, float, float, float]]:
    """Rectangles (x, y, w, h) of the photo that lie outside the frame: what a crop cuts."""
    ix1, iy1 = view.ix + view.iw, view.iy + view.ih
    fx1, fy1 = view.fx + view.fw, view.fy + view.fh
    out = []
    top = min(iy1, view.fy) - view.iy
    if top > 0:
        out.append((view.ix, view.iy, view.iw, top))
    bottom = iy1 - max(view.iy, fy1)
    if bottom > 0:
        out.append((view.ix, iy1 - bottom, view.iw, bottom))
    y0, y1 = max(view.iy, view.fy), min(iy1, fy1)
    if y1 > y0:
        left = min(ix1, view.fx) - view.ix
        if left > 0:
            out.append((view.ix, y0, left, y1 - y0))
        right = ix1 - max(view.ix, fx1)
        if right > 0:
            out.append((ix1 - right, y0, right, y1 - y0))
    return out


def snap(value: float, step: int = SNAP) -> int:
    """Round to the nearest multiple of ``step`` (halves round up)."""
    step = max(1, int(step))
    return math.floor(value / step + 0.5) * step


def drag_margins(
    start: Margins, handle: str, dx: float, dy: float, scale: float, limit: int = MAX_MARGIN, step: int = SNAP
) -> Margins:
    """Margins after dragging ``handle`` by (dx, dy) screen pixels from where
    the drag started with ``start``. Outward adds space, inward removes it
    and, past the photo edge, crops (within +-``limit``); results snap to ``step`` pixels."""
    if scale <= 0 or handle not in CURSORS:
        return start
    px, py = dx / scale, dy / scale
    values = start.as_dict()
    if "left" in handle:
        values["left"] = start.left - px
    if "right" in handle:
        values["right"] = start.right + px
    if "top" in handle:
        values["top"] = start.top - py
    if "bottom" in handle:
        values["bottom"] = start.bottom + py
    moved = {side: clamp_margin(snap(v, step), limit) if v != getattr(start, side) else v for side, v in values.items()}
    # Magnet to the centre: an edge dragged close to its opposite side's margin
    # takes the same value, so the photo sits centred on that axis.
    pull = CENTER_MAGNET_PX / scale
    for side, opposite in (("left", "right"), ("right", "left"), ("top", "bottom"), ("bottom", "top")):
        if (
            side in handle
            and opposite not in handle
            and getattr(start, opposite)
            and abs(values[side] - getattr(start, opposite)) <= pull
        ):
            moved[side] = getattr(start, opposite)
    return Margins(**{s: int(v) for s, v in moved.items()})


# -------------------------------------------------------------- state -----


class ExpandState:
    """Margins, preset and anchor of the editor, kept consistent.

    Every change goes through a method here, so the dialog widgets (editor,
    preset buttons, anchor grid, spin boxes) only display this state.
    ``listener`` is called after each change. Margins may be negative (crop).
    """

    def __init__(
        self,
        image_w: int,
        image_h: int,
        margins: Margins | None = None,
        limit: int = MAX_MARGIN,
        listener: Callable[[], None] | None = None,
    ) -> None:
        self.image_w = max(1, int(image_w))
        self.image_h = max(1, int(image_h))
        self.limit = limit
        self.margins = fit_margins(margins or Margins(), self.image_w, self.image_h, None, limit)
        self.anchor = anchor_of(self.margins)
        self.preset = matching_preset(self.image_w, self.image_h, self.margins)
        portrait = is_portrait(self.preset)
        self.portrait = self.image_h > self.image_w if portrait is None else portrait
        self.listener = listener

    def _changed(self) -> None:
        if self.listener is not None:
            self.listener()

    @property
    def canvas(self) -> tuple[int, int]:
        return self.margins.canvas(self.image_w, self.image_h)

    @property
    def ratio(self) -> tuple[int, int] | None:
        """The locked ratio of the active preset; None for Custom."""
        return preset_ratio(self.preset, self.image_w, self.image_h) if self.preset != "custom" else None

    def side_range(self, side: str) -> tuple[int, int]:
        return side_range(self.margins, side, self.image_w, self.image_h, self.limit)

    def _store(self, margins: Margins, keep_preset: bool) -> None:
        margins = fit_margins(margins, self.image_w, self.image_h, self.margins, self.limit)
        if margins == self.margins:
            return
        self.margins = margins
        self.anchor = anchor_of(margins, self.anchor)
        if not keep_preset:
            self.preset = matching_preset(self.image_w, self.image_h, margins)
            portrait = is_portrait(self.preset)
            if portrait is not None:
                self.portrait = portrait
        self._changed()

    def set_margins(self, margins: Margins) -> None:
        """A free change (edge drag or spin box): the preset follows the result."""
        self._store(margins, False)

    def set_side(self, side: str, value: float) -> None:
        if side in SIDES:
            self.set_margins(self.margins.replace(**{side: clamp_margin(value, self.limit)}))

    def move_from(self, start: Margins, dx: float, dy: float) -> None:
        """Move the frame (size and preset kept) by (dx, dy) image pixels from ``start``."""
        self._store(move_frame(start, dx, dy, self.image_w, self.image_h, self.limit), True)

    def resize_from(self, start: Margins, corner: str, dx: float, dy: float) -> None:
        """Drag a frame corner by (dx, dy) image pixels from ``start``: with a
        ratio preset the ratio stays locked (and the preset), else it is free."""
        ratio = self.ratio
        if ratio is None:
            self.set_margins(drag_margins(start, corner, dx, dy, 1.0, self.limit))
        else:
            self._store(resize_locked(start, corner, dx, dy, ratio, self.image_w, self.image_h, self.limit), True)

    def apply_preset(self, preset: str) -> None:
        """Pick an aspect-ratio preset; "custom" keeps the margins as they are."""
        if preset not in PRESET_IDS:
            return
        self.preset = preset
        portrait = is_portrait(preset)
        if portrait is not None:
            self.portrait = portrait
        margins = preset_margins(self.image_w, self.image_h, preset, self.anchor, self.limit)
        if margins is not None:
            self.margins = margins
        self._changed()

    def apply_ratio(self, button: str) -> None:
        """Pick a ratio button; it is applied in the current orientation."""
        self.apply_preset(oriented(button, self.portrait))

    def set_portrait(self, portrait: bool) -> None:
        """Switch between landscape and portrait; an oriented ratio turns round."""
        if portrait == self.portrait:
            return
        self.portrait = portrait
        if has_orientation(self.preset):
            self.apply_preset(oriented(self.preset, portrait))
        else:
            self._changed()

    def set_anchor(self, anchor: str) -> None:
        """Snap the frame to ``anchor`` (where the photo sits in it), keeping its size."""
        if anchor not in ANCHORS:
            return
        self.anchor = anchor
        self.margins = fit_margins(
            reanchor(self.margins, anchor, self.limit), self.image_w, self.image_h, None, self.limit
        )
        self._changed()


# ---------------------------------------------------------------- GTK -----

try:  # the pure part above must import without GTK (unit tests)
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk
except (ImportError, ValueError):  # pragma: no cover - no GTK in the test Python
    Gtk = None  # type: ignore[assignment]


if Gtk is not None:

    class ExpandEditor(Gtk.DrawingArea):
        """Thumbnail of the image inside the dashed frame of the new canvas.

        Drag an edge or corner handle outward to add space or inward to crop;
        the new area is shaded, the cropped part of the photo darkened, and the
        amounts are written on them. Dragging inside the frame moves it; with
        a ratio preset the corners resize with the ratio locked. While a drag
        runs the scale is frozen (the image stays under the pointer); on
        release the view fits the frame again.
        """

        def __init__(self, state: ExpandState, thumbnail: object | None = None) -> None:
            super().__init__()
            self.state = state
            self.thumbnail = thumbnail  # GdkPixbuf.Pixbuf or None
            self.hover: str | None = None
            self._drag: tuple[str, float, float, Margins, View] | None = None
            self.set_size_request(560, 340)
            self.set_hexpand(True)
            self.set_can_focus(False)
            self.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.BUTTON_RELEASE_MASK
                | Gdk.EventMask.POINTER_MOTION_MASK
                | Gdk.EventMask.LEAVE_NOTIFY_MASK
            )
            self.connect("draw", self._draw)
            self.connect("button-press-event", self._press)
            self.connect("button-release-event", self._release)
            self.connect("motion-notify-event", self._motion)
            self.connect("leave-notify-event", self._leave)

        # geometry -------------------------------------------------------

        def view(self) -> View:
            """The current view (frozen scale and image position while dragging)."""
            s = self.state
            if self._drag is not None:
                v = self._drag[4]
                return view_at(v.scale, v.ix, v.iy, s.image_w, s.image_h, s.margins)
            return fit_view(self.get_allocated_width(), self.get_allocated_height(), s.image_w, s.image_h, s.margins)

        # drag (also used by the test hooks) ----------------------------

        def begin_drag(self, handle: str, x: float, y: float) -> None:
            self._drag = (handle, x, y, self.state.margins, self.view())
            self.hover = handle

        def update_drag(self, x: float, y: float) -> None:
            if self._drag is None:
                return
            handle, x0, y0, start, view = self._drag
            s = self.state
            dx, dy = x - x0, y - y0
            if handle == "move":
                # keep the whole frame inside the widget so it cannot be lost out of sight
                dx = max(-view.fx, min(self.get_allocated_width() - view.fx - view.fw, dx))
                dy = max(-view.fy, min(self.get_allocated_height() - view.fy - view.fh, dy))
                s.move_from(start, dx / view.scale, dy / view.scale)
            elif "-" in handle and s.ratio is not None:
                s.resize_from(start, handle, dx / view.scale, dy / view.scale)
            else:
                s.set_margins(drag_margins(start, handle, dx, dy, view.scale, s.limit))
            self.queue_draw()

        def end_drag(self) -> None:
            self._drag = None
            self.queue_draw()

        def drag_handle_by(self, handle: str, dx: float, dy: float) -> None:
            """Test hook: drag ``handle`` by (dx, dy) screen pixels, as a user would."""
            view = self.view()
            points = handle_points(view)
            if handle == "move":
                x, y = view.fx + view.fw / 2, view.fy + view.fh / 2
            elif handle in points:
                x, y = points[handle]
            else:
                return
            self.begin_drag(handle, x, y)
            self.update_drag(x + dx, y + dy)
            self.end_drag()

        # events ---------------------------------------------------------

        def _set_cursor(self, handle: str | None) -> None:
            window = self.get_window()
            if window is None:
                return
            name = CURSORS.get(handle, MOVE_CURSOR) if handle else None
            cursor = Gdk.Cursor.new_from_name(self.get_display(), name) if name else None
            window.set_cursor(cursor)

        def _press(self, _w: Gtk.Widget, event: Gdk.EventButton) -> bool:
            if event.button != 1:
                return False
            handle = hit_region(self.view(), event.x, event.y)
            if handle is None:
                return False
            self.begin_drag(handle, event.x, event.y)
            return True

        def _release(self, _w: Gtk.Widget, event: Gdk.EventButton) -> bool:
            if event.button != 1 or self._drag is None:
                return False
            self.end_drag()
            self.hover = hit_region(self.view(), event.x, event.y)
            self._set_cursor(self.hover)
            return True

        def _motion(self, _w: Gtk.Widget, event: Gdk.EventMotion) -> bool:
            if self._drag is not None:
                self.update_drag(event.x, event.y)
                return True
            handle = hit_region(self.view(), event.x, event.y)
            if handle != self.hover:
                self.hover = handle
                self._set_cursor(handle)
                self.queue_draw()
            return False

        def _leave(self, *_: object) -> bool:
            if self._drag is None and self.hover is not None:
                self.hover = None
                self._set_cursor(None)
                self.queue_draw()
            return False

        # drawing --------------------------------------------------------

        def _draw(self, _w: Gtk.Widget, cr: object) -> bool:
            v = self.view()
            s = self.state
            m = s.margins
            # new area: shaded frame
            cr.set_source_rgba(0.18, 0.44, 0.77, 0.28)
            cr.rectangle(v.fx, v.fy, v.fw, v.fh)
            cr.fill()
            # the image
            if self.thumbnail is not None:
                cr.save()
                cr.translate(v.ix, v.iy)
                cr.scale(v.iw / max(1, self.thumbnail.get_width()), v.ih / max(1, self.thumbnail.get_height()))
                Gdk.cairo_set_source_pixbuf(cr, self.thumbnail, 0, 0)
                cr.paint()
                cr.restore()
            else:
                cr.set_source_rgb(0.42, 0.42, 0.42)
                cr.rectangle(v.ix, v.iy, v.iw, v.ih)
                cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.55)
            cr.set_line_width(1)
            cr.rectangle(v.ix + 0.5, v.iy + 0.5, v.iw - 1, v.ih - 1)
            cr.stroke()
            # the cropped part of the photo, darkened
            cr.set_source_rgba(0, 0, 0, 0.6)
            for sx, sy, sw, sh in crop_strips(v):
                cr.rectangle(sx, sy, sw, sh)
            cr.fill()
            # amounts on the new area (+) and on the cropped strips (-)
            cr.set_font_size(12)
            sides = {
                "left": (min(v.fx, v.ix), v.iy, abs(v.ix - v.fx), v.ih, m.left),
                "right": (min(v.ix + v.iw, v.fx + v.fw), v.iy, abs(v.fx + v.fw - v.ix - v.iw), v.ih, m.right),
                "top": (v.ix, min(v.fy, v.iy), v.iw, abs(v.iy - v.fy), m.top),
                "bottom": (v.ix, min(v.iy + v.ih, v.fy + v.fh), v.iw, abs(v.fy + v.fh - v.iy - v.ih), m.bottom),
            }
            for x, y, w, h, amount in sides.values():
                if amount == 0:
                    continue
                text = f"+{amount}" if amount > 0 else f"-{-amount}"
                ext = cr.text_extents(text)
                if ext.width + 6 > w or ext.height + 6 > h:
                    continue
                cr.set_source_rgba(1, 1, 1, 0.9)
                cr.move_to(x + (w - ext.width) / 2 - ext.x_bearing, y + (h - ext.height) / 2 - ext.y_bearing)
                cr.show_text(text)
            # dashed frame of the new canvas
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.rectangle(v.fx + 0.75, v.fy + 0.75, v.fw - 1.5, v.fh - 1.5)
            cr.stroke()
            cr.set_dash([])
            # handles
            half = HANDLE_PX / 2
            for name, (hx, hy) in handle_points(v).items():
                active = name == self.hover
                cr.rectangle(hx - half, hy - half, HANDLE_PX, HANDLE_PX)
                if active:
                    cr.set_source_rgb(0.23, 0.50, 0.84)
                else:
                    cr.set_source_rgb(1, 1, 1)
                cr.fill_preserve()
                cr.set_source_rgb(0.1, 0.1, 0.1)
                cr.set_line_width(1)
                cr.stroke()
            return False
