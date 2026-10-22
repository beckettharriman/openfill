# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Scene description of the context around a selection, without a model.

ComfyUI has no captioning node without extra node packs, so the description
comes from cheap statistics of the context crop (the pixels outside the
selection): brightness, contrast, colour temperature, saturation, sharpness
and grain, plus a few daylight and indoor heuristics. It produces short
photographic phrases such as "soft overcast daylight, muted colours, slight
film grain, photo" that are added to a prompt so a generated object matches
the light and look of the photo around it.

Pure Python on small pixel buffers (the caller passes a downsampled crop);
no GIMP imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Longest side of the analysis buffer; ``sample`` downsamples to it.
ANALYSIS_SIDE = 160


@dataclass(frozen=True)
class SceneStats:
    luminance: float  # mean, 0..1
    contrast: float  # standard deviation of luminance, 0..1
    saturation: float  # mean HSV saturation, 0..1
    warmth: float  # mean (R - B), -1..1
    green: float  # mean (G - (R + B) / 2), -1..1
    sharpness: float  # mean absolute Laplacian of luminance (0..1 scale)
    grain: float  # median absolute high-pass in flat areas (0..1 scale)
    top_luminance: float  # mean luminance of the top fifth
    top_blue: float  # mean (B - R) of the top fifth
    pixels: int


def sample(width: int, height: int, data: bytes, channels: int, side: int = ANALYSIS_SIDE) -> tuple[int, int, bytes]:
    """Nearest-neighbour downsample of an 8-bit interleaved buffer so its longer
    side is at most ``side``. Returns (width, height, data) with the same channel count."""
    step = max(1, math.ceil(max(width, height) / side))
    if step == 1:
        return width, height, bytes(data)
    out = bytearray()
    row_len = width * channels
    for y in range(0, height, step):
        row = data[y * row_len : (y + 1) * row_len]
        # pick every step-th pixel: interleave the channel slices
        picked = [row[c::channels][::step] for c in range(channels)]
        n = len(picked[0])
        block = bytearray(n * channels)
        for c in range(channels):
            block[c::channels] = picked[c]
        out += block
    new_w = len(range(0, width, step))
    new_h = len(range(0, height, step))
    return new_w, new_h, bytes(out)


def analyse(width: int, height: int, data: bytes, channels: int, mask: bytes | None = None) -> SceneStats | None:
    """Statistics of the pixels where ``mask`` (one byte per pixel) is below
    128, or of every pixel without a mask. None when fewer than 16 pixels count."""
    if width <= 0 or height <= 0 or channels < 3:
        return None
    n = width * height
    lum = [0.0] * n
    use = [True] * n if mask is None else [m < 128 for m in mask[:n]]
    r_s = g_s = b_s = sat_s = l_s = l2_s = 0.0
    count = 0
    top_rows = max(1, height // 5)
    top_l = top_b = 0.0
    top_n = 0
    for i in range(n):
        o = i * channels
        r, g, b = data[o] / 255.0, data[o + 1] / 255.0, data[o + 2] / 255.0
        lv = 0.299 * r + 0.587 * g + 0.114 * b
        lum[i] = lv
        if not use[i]:
            continue
        mx, mn = max(r, g, b), min(r, g, b)
        sat_s += (mx - mn) / mx if mx > 0 else 0.0
        r_s += r
        g_s += g
        b_s += b
        l_s += lv
        l2_s += lv * lv
        count += 1
        if i // width < top_rows:
            top_l += lv
            top_b += b - r
            top_n += 1
    if count < 16:
        return None
    mean_l = l_s / count
    contrast = math.sqrt(max(0.0, l2_s / count - mean_l * mean_l))
    lap_vals: list[float] = []
    flat_vals: list[float] = []
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            i = row + x
            if not use[i]:
                continue
            c = lum[i]
            up, dn, lf, rt = lum[i - width], lum[i + width], lum[i - 1], lum[i + 1]
            lap = abs(up + dn + lf + rt - 4 * c)
            lap_vals.append(lap)
            if max(up, dn, lf, rt, c) - min(up, dn, lf, rt, c) < 0.08:
                flat_vals.append(abs(c - (up + dn + lf + rt) / 4))
    sharp = sum(lap_vals) / len(lap_vals) if lap_vals else 0.0
    flat_vals.sort()
    grain = flat_vals[len(flat_vals) // 2] if flat_vals else 0.0
    return SceneStats(
        luminance=mean_l,
        contrast=contrast,
        saturation=sat_s / count,
        warmth=(r_s - b_s) / count,
        green=(g_s - (r_s + b_s) / 2) / count,
        sharpness=sharp,
        grain=grain,
        top_luminance=top_l / top_n if top_n else mean_l,
        top_blue=top_b / top_n if top_n else 0.0,
        pixels=count,
    )


def phrases(stats: SceneStats) -> list[str]:
    """Photographic phrases for the statistics, most important first."""
    out: list[str] = []
    lum, warm, sat = stats.luminance, stats.warmth, stats.saturation
    sky = stats.top_luminance > 0.55 and stats.top_luminance >= lum and (stats.top_blue > 0.02 or sat < 0.12)
    vegetation = stats.green > 0.04
    if lum > 0.75 and sat < 0.12:
        out.append("bright even studio light" if stats.contrast < 0.06 else "soft overcast daylight")
    elif lum < 0.18:
        out.append("night scene, dim light" if warm < 0.05 else "dim warm low light")
    elif sky or vegetation:
        if lum < 0.32 and not vegetation:
            out.append("dusk light")
        elif stats.contrast < 0.13 or (sat < 0.22 and not sky):
            out.append("soft overcast daylight")
        else:
            out.append("bright natural daylight")
    elif warm > 0.08 and lum < 0.5:
        out.append("warm indoor light")
    elif lum < 0.32:
        out.append("low light")
    else:
        out.append("soft natural light")
    if sat < 0.08:
        out.append("almost monochrome colours")
    elif sat < 0.2:
        out.append("muted colours")
    elif sat > 0.45:
        out.append("vivid colours")
    if warm > 0.12 and lum >= 0.18:
        out.append("warm tones")
    elif warm < -0.06:
        out.append("cool tones")
    if stats.contrast > 0.28:
        out.append("high contrast")
    elif stats.contrast < 0.07:
        out.append("low contrast")
    if stats.sharpness < 0.012:
        out.append("soft focus")
    elif stats.sharpness > 0.06:
        out.append("sharp detail")
    # grain is only told apart from texture on smooth surfaces
    if stats.sharpness < 0.12:
        if stats.grain > 0.012:
            out.append("visible film grain")
        elif stats.grain > 0.006:
            out.append("slight film grain")
    out.append("photo")
    return out


def describe(width: int, height: int, data: bytes, channels: int, mask: bytes | None = None) -> str:
    """Scene description of the (masked-out) context, or "" when there is too
    little to analyse. ``data`` should already be small (``sample``)."""
    stats = analyse(width, height, data, channels, mask)
    if stats is None:
        return ""
    return ", ".join(phrases(stats))


def augment(prompt: str, description: str, prepend: bool = False) -> str:
    """The user's prompt with the scene description after it (default: the
    prompt stays first, where the text encoder weighs it most) or before it.
    Phrases the prompt already contains are not repeated; an empty prompt is
    returned unchanged."""
    prompt = (prompt or "").strip()
    if not prompt or not description:
        return prompt
    lower = prompt.lower()
    extra = [p for p in description.split(", ") if p and p.lower() not in lower]
    if "photo" in lower:
        extra = [p for p in extra if p != "photo"]
    if not extra:
        return prompt
    return f"{', '.join(extra)}, {prompt}" if prepend else f"{prompt}, {', '.join(extra)}"
