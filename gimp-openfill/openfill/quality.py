# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Cheap quality metrics for ranking the variations of a fill.

Every candidate is compared with the original context around the selection,
on small buffers (a downsampled context, see ``caption.sample``):

- seam: gradient on the selection edge against the gradient a few pixels to
  either side (a step at the edge shows up as a ratio above 1);
- softness: Laplacian variance inside the selection against a ring outside
  it (a smeared fill is far below 1; for removal an invented object is far
  above it);
- frame: colour difference between a thin band just inside the edge and one
  just outside it (a pale frame or a pasted panel);
- colour: mean and spread of the whole inside against the ring (for removal
  lower is better; a prompted fill that hardly differs drew little);
- leak: change outside the selection, where the layer mask's feather shows
  the generated pixels.

Lower scores are better. Pure Python; no GIMP imports.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

INF = 1 << 20


# Inside-against-ring colour distance below which a prompted fill counts as
# having drawn little (calibrated on 4 x 4 variations of strawberry and balloon).
PROMPT_CONTENT = 0.35


@dataclass(frozen=True)
class Buffer:
    """An 8-bit interleaved pixel buffer."""

    width: int
    height: int
    data: bytes
    channels: int = 3

    def luminance(self) -> list[float]:
        c, d = self.channels, self.data
        if c < 3:
            return [d[i * c] / 255.0 for i in range(self.width * self.height)]
        return [
            (0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]) / 255.0
            for i in range(0, self.width * self.height * c, c)
        ]

    def rgb(self, index: int) -> tuple[float, float, float]:
        o = index * self.channels
        if self.channels < 3:
            v = self.data[o] / 255.0
            return v, v, v
        return self.data[o] / 255.0, self.data[o + 1] / 255.0, self.data[o + 2] / 255.0


@dataclass(frozen=True)
class Score:
    seam: float  # edge gradient / gradient beside it
    lap_ratio: float  # Laplacian variance inside / ring
    frame: float  # colour step across the edge, inside band against outside band
    colour: float  # inside against ring (mean and spread)
    leak: float  # mean absolute change outside the selection
    total: float  # weighted penalty, lower is better


def signed_distance(width: int, height: int, mask: bytes, threshold: int = 128) -> list[int]:
    """Chessboard distance to the selection edge: positive outside the
    selection (mask below ``threshold``), zero or negative inside it."""
    n = width * height
    inside = [mask[i] >= threshold for i in range(n)]

    def chamfer(seed: list[bool]) -> list[int]:
        d = [0 if s else INF for s in seed]
        for y in range(height):
            row = y * width
            for x in range(width):
                i = row + x
                if d[i] == 0:
                    continue
                best = d[i]
                if x > 0:
                    best = min(best, d[i - 1] + 1)
                if y > 0:
                    best = min(best, d[i - width] + 1)
                    if x > 0:
                        best = min(best, d[i - width - 1] + 1)
                    if x < width - 1:
                        best = min(best, d[i - width + 1] + 1)
                d[i] = best
        for y in range(height - 1, -1, -1):
            row = y * width
            for x in range(width - 1, -1, -1):
                i = row + x
                best = d[i]
                if x < width - 1:
                    best = min(best, d[i + 1] + 1)
                if y < height - 1:
                    best = min(best, d[i + width] + 1)
                    if x < width - 1:
                        best = min(best, d[i + width + 1] + 1)
                    if x > 0:
                        best = min(best, d[i + width - 1] + 1)
                d[i] = best
        return d

    to_inside = chamfer(inside)  # outside pixels: distance to the selection
    to_outside = chamfer([not s for s in inside])  # inside pixels: distance to the outside
    return [to_inside[i] if not inside[i] else 1 - to_outside[i] for i in range(n)]


def _grad_lap(lum: list[float], width: int, height: int) -> tuple[list[float], list[float]]:
    grad = [0.0] * (width * height)
    lap = [0.0] * (width * height)
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            i = row + x
            up, dn, lf, rt = lum[i - width], lum[i + width], lum[i - 1], lum[i + 1]
            grad[i] = math.hypot(rt - lf, dn - up) / 2
            lap[i] = up + dn + lf + rt - 4 * lum[i]
    return grad, lap


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _var(values: list[float]) -> float:
    if not values:
        return 0.0
    m = _mean(values)
    return sum((v - m) ** 2 for v in values) / len(values)


def _colour_stats(buf: Buffer, indices: list[int]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not indices:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    chans = list(zip(*(buf.rgb(i) for i in indices), strict=False))
    means = tuple(_mean(list(c)) for c in chans)
    spreads = tuple(math.sqrt(_var(list(c))) for c in chans)
    return means, spreads


def score(
    original: Buffer,
    candidate: Buffer,
    mask: bytes,
    removal: bool,
    distance: list[int] | None = None,
) -> Score:
    """Score ``candidate`` (the generated context) against ``original`` (the
    same context before the fill); ``mask`` has one byte per pixel, 255 inside
    the selection. Buffers must have the same size."""
    w, h = candidate.width, candidate.height
    if (original.width, original.height) != (w, h):
        raise ValueError("original and candidate differ in size")
    d = distance if distance is not None else signed_distance(w, h, mask)
    lum = candidate.luminance()
    grad, lap = _grad_lap(lum, w, h)
    interior = [y * w + x for y in range(1, h - 1) for x in range(1, w - 1)]
    edge = [i for i in interior if -1 <= d[i] <= 1]
    beside = [i for i in interior if 3 <= abs(d[i]) <= 8]
    inner = [i for i in interior if d[i] <= -2]
    ring = [i for i in interior if 2 <= d[i] <= 10]
    band_in = [i for i in interior if -4 <= d[i] <= -1]
    band_out = [i for i in interior if 2 <= d[i] <= 5]
    seam = _mean([grad[i] for i in edge]) / max(1e-6, _mean([grad[i] for i in beside])) if edge and beside else 1.0
    lap_ratio = _var([lap[i] for i in inner]) / max(1e-9, _var([lap[i] for i in ring])) if inner and ring else 1.0
    m_in, _ = _colour_stats(candidate, band_in)
    m_out, _ = _colour_stats(candidate, band_out)
    frame = math.sqrt(sum((a - b) ** 2 for a, b in zip(m_in, m_out, strict=False)))
    whole_in, s_in = _colour_stats(candidate, inner)
    m_ring, s_ring = _colour_stats(original, ring)
    colour = math.sqrt(sum((a - b) ** 2 for a, b in zip(whole_in, m_ring, strict=False))) + 0.5 * math.sqrt(
        sum((a - b) ** 2 for a, b in zip(s_in, s_ring, strict=False))
    )
    outside = [i for i in interior if d[i] >= 2]
    leak = (
        _mean([abs(a - b) for i in outside for a, b in zip(candidate.rgb(i), original.rgb(i), strict=False)])
        if outside
        else 0.0
    )
    penalty = 2.0 * max(0.0, seam - 1.0)
    penalty += 0.6 * max(0.0, -math.log(max(lap_ratio, 1e-6)))  # softer than the surroundings
    if removal:
        penalty += 0.6 * max(0.0, math.log(max(lap_ratio, 1e-6) / 1.5))  # busier: something was invented
        penalty += 3.0 * colour
    else:
        # a prompted fill should put something there: a fill that barely
        # differs from its surroundings (no object, or a tiny one) ranks lower
        penalty += 3.0 * max(0.0, PROMPT_CONTENT - colour)
    penalty += 6.0 * frame + 8.0 * leak
    return Score(seam, lap_ratio, frame, colour, leak, penalty)


def rank(original: Buffer, candidates: list[Buffer], mask: bytes, removal: bool) -> list[int]:
    """Indices of ``candidates``, best first (stable for equal scores)."""
    if len(candidates) < 2:
        return list(range(len(candidates)))
    d = signed_distance(original.width, original.height, mask)
    totals = [score(original, c, mask, removal, d).total for c in candidates]
    return sorted(range(len(candidates)), key=lambda i: (totals[i], i))


# --------------------------------------------------------------- grain -------

# Residual of a pixel against the mean of its four neighbours, for pure
# Gaussian noise of deviation s, has deviation s * sqrt(1.25).
_RESIDUAL_GAIN = math.sqrt(1.25)
# Largest local luminance range (8-bit levels) of a pixel neighbourhood that
# still counts as flat: noise is measured there, not texture or edges.
FLAT_RANGE = 24


def noise_sigma(
    width: int, height: int, data: bytes, channels: int, mask: bytes | None = None, inside: bool = False, step: int = 2
) -> float:
    """Noise deviation (8-bit levels) of the luminance in flat areas, from the
    median absolute residual against the four neighbours. With ``mask`` only
    pixels outside it (or inside it with ``inside``) count; every ``step``-th
    row and column is sampled. 0 when there are too few flat pixels."""
    c = channels
    residuals = []
    for y in range(1, height - 1, step):
        for x in range(1, width - 1, step):
            i = y * width + x
            if mask is not None and (mask[i] >= 128) != inside:
                continue

            def lum(j: int) -> float:
                o = j * c
                if c < 3:
                    return float(data[o])
                return 0.299 * data[o] + 0.587 * data[o + 1] + 0.114 * data[o + 2]

            v, up, dn, lf, rt = lum(i), lum(i - width), lum(i + width), lum(i - 1), lum(i + 1)
            if max(v, up, dn, lf, rt) - min(v, up, dn, lf, rt) > FLAT_RANGE:
                continue
            residuals.append(abs(v - (up + dn + lf + rt) / 4))
    if len(residuals) < 64:
        return 0.0
    residuals.sort()
    return 1.4826 * residuals[len(residuals) // 2] / _RESIDUAL_GAIN


def grain_to_add(ring_sigma: float, inside_sigma: float, minimum: float = 1.0) -> float:
    """Deviation of the noise that brings ``inside_sigma`` up to ``ring_sigma``
    (they add in quadrature); 0 below ``minimum`` levels (invisible)."""
    add = math.sqrt(max(0.0, ring_sigma * ring_sigma - inside_sigma * inside_sigma))
    return add if add >= minimum else 0.0


def add_grain(data: bytes, channels: int, sigma: float, seed: int = 0) -> bytes:
    """Add monochrome Gaussian noise of deviation ``sigma`` (8-bit levels) to
    the colour channels of an interleaved 8-bit buffer (alpha untouched). The
    same offset goes to R, G and B so the grain has no colour."""
    if sigma <= 0 or not data:
        return data
    n = len(data) // channels
    # 256 quantiles of the normal distribution, indexed by a random byte
    offsets = [round(sigma * _normal_quantile((k + 0.5) / 256)) for k in range(256)]
    table = bytes(max(0, min(255, v + offsets[k])) for v in range(256) for k in range(256))
    noise = random.Random(seed).randbytes(n)
    out = bytearray(data)
    colours = min(3, channels)
    for ch in range(colours):
        values = data[ch::channels]
        out[ch::channels] = bytes(map(table.__getitem__, map(int.__add__, map((256).__mul__, values), noise)))
    return bytes(out)


def _normal_quantile(p: float) -> float:
    """Inverse of the standard normal CDF (Acklam's rational approximation)."""
    a = (
        -39.69683028665376,
        220.9460984245205,
        -275.9285104469687,
        138.3577518672690,
        -30.66479806614716,
        2.506628277459239,
    )
    b = (-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572)
    c = (
        -0.007784894002430293,
        -0.3223964580411365,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    low = 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > 1 - low:
        return -_normal_quantile(1 - p)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


# ------------------------------------------------------- seam correction ----

# A generated fill often comes back a few levels lighter or tinted against the
# original right at the selection edge (VAE round trip, colour match over the
# whole context). On textured areas that hides; on a smooth sky or wall the
# layer's outline shows as a faint rectangle. The correction measures the
# difference just outside the selection, where the visible layer should equal
# the original, and spreads it smoothly over the fill: only a low-frequency
# offset, so the generated detail is untouched.

SEAM_SIDE = 256  # analysis resolution (longer side)
SEAM_CELL = 8  # analysis pixels per offset cell
SEAM_MAX_OFFSET = 24  # larger differences are content, not drift: left alone
SEAM_MAX_SPREAD = 10  # cells whose difference varies more than this are texture: ignored
SEAM_MIN_OFFSET = 1.5  # below this nothing is visible: no change


@dataclass(frozen=True)
class SeamField:
    """Per-channel offsets on a coarse grid; ``block`` full-size pixels per grid cell."""

    width: int  # grid size
    height: int
    block: int
    offsets: tuple[tuple[float, ...], ...]  # per grid cell (row-major): one offset per colour channel


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def seam_field(
    width: int,
    height: int,
    original: bytes,
    layer: bytes,
    mask: bytes,
    channels: int = 4,
    band: int = 24,
    side: int = SEAM_SIDE,
    original_rect: tuple[int, int, int, int] | None = None,
) -> SeamField | None:
    """Offsets that make ``layer`` meet ``original`` at the selection edge, or None when nothing needs fixing.

    ``original`` and ``layer`` are 8-bit interleaved buffers of the same context
    (``channels`` each), ``mask`` the selection (one byte per pixel) and
    ``band`` how far the visible layer reaches beyond the selection, in pixels.

    With ``original_rect`` (x0, y0, x1, y1: where ``original`` holds real pixels,
    Generative Expand's old image) ``mask`` is the generated area instead: the
    difference is measured where it overlaps the old image, and the offsets
    spread over the whole generated area.
    """
    step = max(1, math.ceil(max(width, height) / side))
    sw, sh = len(range(0, width, step)), len(range(0, height, step))
    colours = min(3, channels)

    def small(data: bytes, ch: int) -> list[bytes]:
        rows = []
        stride = width * ch
        for y in range(0, height, step):
            row = data[y * stride : (y + 1) * stride]
            rows.append([row[c::ch][::step] for c in range(ch)])
        return rows

    so, sl, sm = small(original, channels), small(layer, channels), small(mask, 1)
    flat_mask = b"".join(r[0] for r in sm)
    if original_rect is None:
        dist = signed_distance(sw, sh, flat_mask)
        reach = max(1, round(band / step))
    else:
        # Measured: generated pixels over the old image (distance 1); shown: anything generated
        # (distance 0); the rest: nothing (a distance beyond reach).
        x0, y0, x1, y1 = original_rect
        reach = 1
        dist = [
            (1 if x0 <= x * step < x1 and y0 <= y * step < y1 and flat_mask[y * sw + x] >= 128 else 0)
            if flat_mask[y * sw + x]
            else 2
            for y in range(sh)
            for x in range(sw)
        ]
    cell = SEAM_CELL
    gw, gh = math.ceil(sw / cell), math.ceil(sh / cell)
    samples: dict[int, list[list[float]]] = {}
    for y in range(sh):
        o_row, l_row = so[y], sl[y]
        for x in range(sw):
            d = dist[y * sw + x]
            if not 1 <= d <= reach:
                continue
            key = (y // cell) * gw + x // cell
            per = samples.setdefault(key, [[] for _ in range(colours)])
            for c in range(colours):
                per[c].append(o_row[c][x] - l_row[c][x])
    known: list[tuple[float, float, tuple[float, ...]]] = []
    for key, per in samples.items():
        if len(per[0]) < 4:
            continue
        meds = tuple(_median(v) for v in per)
        spread = max(_median([abs(a - m) for a in v]) for v, m in zip(per, meds, strict=True))
        if spread > SEAM_MAX_SPREAD or max(abs(m) for m in meds) > SEAM_MAX_OFFSET:
            continue
        known.append((key % gw + 0.5, key // gw + 0.5, meds))
    if not known or max(max(abs(m) for m in k[2]) for k in known) < SEAM_MIN_OFFSET:
        return None
    # Cells the layer shows (inside the selection or within reach of it); the rest stays at zero.
    shown = [False] * (gw * gh)
    for y in range(sh):
        for x in range(sw):
            if dist[y * sw + x] <= reach:
                shown[(y // cell) * gw + x // cell] = True
    offsets = []
    for gy in range(gh):
        for gx in range(gw):
            if not shown[gy * gw + gx]:
                offsets.append((0.0,) * colours)
                continue
            # Shepard interpolation (inverse squared distance) between the measured edge cells.
            acc = [0.0] * colours
            total = 0.0
            exact = None
            for kx, ky, meds in known:
                d2 = (kx - gx - 0.5) ** 2 + (ky - gy - 0.5) ** 2
                if d2 < 1e-6:
                    exact = meds
                    break
                w = 1.0 / d2
                total += w
                for c in range(colours):
                    acc[c] += w * meds[c]
            offsets.append(tuple(exact) if exact else tuple(a / total for a in acc))
    return SeamField(gw, gh, step * cell, tuple(offsets))


def apply_seam_field(data: bytes, width: int, height: int, channels: int, field: SeamField) -> bytes:
    """``data`` with the field's offsets added to its colour channels (bilinear between cells, alpha untouched)."""
    colours = min(3, channels)
    block = field.block
    # Offsets at a resolution of ``fine`` pixels: smooth enough that neighbouring
    # blocks differ by at most a level, cheap enough for pure Python.
    fine = max(1, block // 4)
    fw, fh = math.ceil(width / fine), math.ceil(height / fine)
    span = 2 * SEAM_MAX_OFFSET + 1
    table = bytes(max(0, min(255, v + k - SEAM_MAX_OFFSET)) for v in range(256) for k in range(span))

    def at(gx: float, gy: float, c: int) -> float:
        gx = min(max(gx - 0.5, 0.0), field.width - 1.0)
        gy = min(max(gy - 0.5, 0.0), field.height - 1.0)
        x0, y0 = int(gx), int(gy)
        x1, y1 = min(x0 + 1, field.width - 1), min(y0 + 1, field.height - 1)
        fx, fy = gx - x0, gy - y0
        o = field.offsets
        top = o[y0 * field.width + x0][c] * (1 - fx) + o[y0 * field.width + x1][c] * fx
        bottom = o[y1 * field.width + x0][c] * (1 - fx) + o[y1 * field.width + x1][c] * fx
        return top * (1 - fy) + bottom * fy

    out = bytearray(data)
    stride = width * channels
    for fy in range(fh):
        gy = (fy * fine + fine / 2) / block
        rows = range(fy * fine, min(height, (fy + 1) * fine))
        for c in range(colours):
            cells = [
                max(0, min(span - 1, round(at((fx * fine + fine / 2) / block, gy, c)) + SEAM_MAX_OFFSET))
                for fx in range(fw)
            ]
            if all(k == SEAM_MAX_OFFSET for k in cells):
                continue
            line = b"".join(bytes((k,)) * fine for k in cells)[:width]
            for y in rows:
                start = y * stride + c
                values = data[start : start + stride : channels]
                out[start : start + stride : channels] = bytes(
                    map(table.__getitem__, map(int.__add__, map(span.__mul__, values), line))
                )
    return bytes(out)
