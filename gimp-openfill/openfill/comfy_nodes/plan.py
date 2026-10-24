# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""How the native LaMa node splits a crop to fit the graphics memory (no torch here).

LaMa (big-lama) is fully convolutional, so it runs on the crop at its own pixels.
``plan_native`` returns one pass at 1:1 when the padded crop fits ``budget_px``;
otherwise a few large overlapping windows at 1:1 that are blended in the node;
only when the hole is too wide for windows that each still see known pixels across
it, one pass at the largest scale that fits (never a fixed small size).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MULTIPLE = 8  # LaMa downsamples three times
# A window must see known pixels across the hole: its shorter side is at least the
# hole's shorter side divided by this (the band rule of the old 512 px tiles).
BAND = 0.75
MIN_OVERLAP = 64


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def intersects(self, other: Box) -> bool:
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom


@dataclass(frozen=True)
class NativePlan:
    scale: float  # 1.0 except for the last-resort single pass
    windows: tuple[Box, ...]  # in crop pixels (empty: one pass over the whole crop)
    work_w: int  # size of the pass (scaled crop or largest window), padded
    work_h: int

    @property
    def one_pass(self) -> bool:
        return not self.windows


def padded(value: int, multiple: int = MULTIPLE) -> int:
    return -(-int(value) // multiple) * multiple


def _starts(lo: int, hi: int, size: int, limit: int, overlap: int) -> list[int]:
    """Window starts covering [lo, hi) with windows of ``size`` inside [0, limit),
    neighbours overlapping by at least ``overlap``."""
    span = hi - lo
    if span <= size:
        start = min(max(0, lo - (size - span) // 2), max(0, limit - size))
        return [start]
    count = max(2, math.ceil((span - overlap) / (size - overlap)))
    step = (span - size) / (count - 1)
    out = []
    for i in range(count):
        start = round(lo + i * step)
        out.append(min(max(0, start), max(0, limit - size)))
    return sorted(set(out))


def plan_native(
    width: int,
    height: int,
    hole: Box | None,
    budget_px: int,
    overlap: int = 256,
    max_windows: int = 12,
) -> NativePlan:
    """Plan a LaMa pass over a ``width`` x ``height`` crop whose hole has bounding
    box ``hole`` (None: no hole), with at most ``budget_px`` pixels per pass."""
    if width <= 0 or height <= 0:
        raise ValueError("size must be positive")
    budget_px = max(MULTIPLE * MULTIPLE, int(budget_px))
    pw, ph = padded(width), padded(height)
    if hole is None or pw * ph <= budget_px:
        return NativePlan(1.0, (), pw, ph)
    overlap = max(MIN_OVERLAP, int(overlap))
    # Window: as large as the budget allows, the image's own size where it is smaller.
    side = max(MULTIPLE, int(math.sqrt(budget_px)) // MULTIPLE * MULTIPLE)
    ww = min(width, side)
    wh = min(height, max(MULTIPLE, budget_px // padded(ww) // MULTIPLE * MULTIPLE))
    if wh == height and ww < width:
        ww = min(width, max(MULTIPLE, budget_px // padded(wh) // MULTIPLE * MULTIPLE))
    fits_band = min(hole.w, hole.h) <= BAND * min(ww, wh) and ww > 2 * overlap and wh > 2 * overlap
    if fits_band:
        lo_x, hi_x = max(0, hole.x - overlap), min(width, hole.right + overlap)
        lo_y, hi_y = max(0, hole.y - overlap), min(height, hole.bottom + overlap)
        xs = _starts(lo_x, hi_x, ww, width, overlap)
        ys = _starts(lo_y, hi_y, wh, height, overlap)
        windows = tuple(Box(x, y, ww, wh) for y in ys for x in xs if Box(x, y, ww, wh).intersects(hole))
        if windows and len(windows) <= max_windows:
            return NativePlan(1.0, windows, padded(ww), padded(wh))
    # Last resort: the whole crop once, at the largest scale that fits.
    scale = math.sqrt(budget_px / float(pw * ph))
    sw = max(MULTIPLE, int(width * scale) // MULTIPLE * MULTIPLE)
    sh = max(MULTIPLE, int(height * scale) // MULTIPLE * MULTIPLE)
    while sw * sh > budget_px and sw > MULTIPLE and sh > MULTIPLE:
        sw, sh = sw - MULTIPLE, sh - MULTIPLE
    return NativePlan(min(sw / width, sh / height), (), sw, sh)


def window_order(windows: list[Box], known_fraction) -> list[int]:
    """Greedy order: next the window with the largest share of known pixels, where
    the parts earlier windows filled count as known. ``known_fraction(i, done)``
    gives that share for window ``i`` after the windows in ``done``."""
    left, done = list(range(len(windows))), []
    while left:
        best = max(left, key=lambda i: known_fraction(i, done))
        left.remove(best)
        done.append(best)
    return done
