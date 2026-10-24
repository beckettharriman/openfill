# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Pure geometry helpers: context regions, working sizes, outpaint canvases.

No GIMP imports. Every function here is unit-tested under system Python.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def intersect(self, other: Rect) -> Rect:
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2, y2 = min(self.right, other.right), min(self.bottom, other.bottom)
        return Rect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))

    def contains(self, other: Rect) -> bool:
        return self.x <= other.x and self.y <= other.y and self.right >= other.right and self.bottom >= other.bottom

    def translate(self, dx: int, dy: int) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    def relative_to(self, origin: Rect) -> Rect:
        """This rectangle expressed in the coordinate system of ``origin``."""
        return Rect(self.x - origin.x, self.y - origin.y, self.width, self.height)


@dataclass(frozen=True)
class ModelProfile:
    """Resolution limits of a model family."""

    name: str
    min_side: int  # the model gets unhappy below this (SD 1.5: 512)
    max_side: int  # longest side we send to the model
    min_context: int  # minimum context region size cropped from the image
    multiple: int = 8  # latent alignment


# min_context is deliberately small: a tiny selection gets a tight crop that is
# then UPSCALED to min_side so the object fills a useful part of the frame
# (A1111 "inpaint at full resolution" behaviour), instead of a 768 px crop in
# which a 40 px object is 5 % of the image and gets painted as a blur.
SD15 = ModelProfile("sd15", min_side=512, max_side=768, min_context=192)
SDXL = ModelProfile("sdxl", min_side=768, max_side=1024, min_context=256)
FLUX = ModelProfile("flux", min_side=768, max_side=1024, min_context=256, multiple=16)
QWEN = ModelProfile("qwen_image", min_side=768, max_side=1024, min_context=256, multiple=16)
# LaMa/MAT work best on native-resolution pixels: a 512 px context at 1:1,
# no upscaling (texture scale would not match the surroundings otherwise).
# The removal node runs MAT on a fixed 512 px square (it pads the crop to a
# square and resizes it with nearest-neighbour sampling), so anything larger
# than 512 px is thrown away there with aliasing; the graph downscales with
# lanczos to 512 instead and the node then works 1:1.
INPAINT_MODEL = ModelProfile("inpaint_model", min_side=256, max_side=512, min_context=512)
PROFILES = {"sd15": SD15, "sdxl": SDXL, "flux": FLUX, "qwen_image": QWEN, "inpaint_model": INPAINT_MODEL}


def round_to_multiple(value: int, multiple: int) -> int:
    """Round ``value`` to the nearest multiple, never below ``multiple``."""
    return max(multiple, round(value / multiple) * multiple)


def clamp_rect(rect: Rect, bounds: Rect) -> Rect:
    """Move ``rect`` inside ``bounds`` if possible, shrink it if it is larger."""
    width = min(rect.width, bounds.width)
    height = min(rect.height, bounds.height)
    x = min(max(rect.x, bounds.x), bounds.right - width)
    y = min(max(rect.y, bounds.y), bounds.bottom - height)
    return Rect(x, y, width, height)


def context_rect(
    selection: Rect, image: Rect, profile: ModelProfile, padding_ratio: float = 1.0, min_padding: int = 32
) -> Rect:
    """Grow the selection into the region the model will see.

    The margin on each side is ``padding_ratio`` times the selection's larger
    side (so the context is about three times the selection), at least
    ``min_padding`` pixels and at least ``profile.min_context`` in total when
    the image allows it; centered on the selection and always inside the
    image. Small selections therefore get a tight crop that ``working_size``
    upscales; large ones get a context that is downscaled.
    """
    if selection.is_empty():
        raise ValueError("selection is empty")
    pad = max(min_padding, round(max(selection.width, selection.height) * padding_ratio))
    width = max(selection.width + 2 * pad, profile.min_context)
    height = max(selection.height + 2 * pad, profile.min_context)
    cx = selection.x + selection.width / 2
    cy = selection.y + selection.height / 2
    grown = Rect(round(cx - width / 2), round(cy - height / 2), width, height)
    return clamp_rect(grown, image)


@dataclass(frozen=True)
class WorkingSize:
    width: int
    height: int
    scale: float  # working / source


def working_size(source_w: int, source_h: int, profile: ModelProfile, max_upscale: float = 8.0) -> WorkingSize:
    """Resolution to generate at for a context region of ``source_w x source_h``.

    Downscale so the longer side fits ``profile.max_side``; upscale (at most
    ``max_upscale``) so the shorter side reaches ``profile.min_side``; snap
    both sides to the model's latent multiple.
    """
    if source_w <= 0 or source_h <= 0:
        raise ValueError("source size must be positive")
    longer = max(source_w, source_h)
    shorter = min(source_w, source_h)
    scale = 1.0
    if longer > profile.max_side:
        scale = profile.max_side / longer
    elif shorter < profile.min_side:
        scale = min(max_upscale, profile.min_side / shorter)
        if longer * scale > profile.max_side:
            scale = profile.max_side / longer
    width = round_to_multiple(round(source_w * scale), profile.multiple)
    height = round_to_multiple(round(source_h * scale), profile.multiple)
    if width == source_w and height == source_h:
        return WorkingSize(width, height, 1.0)
    return WorkingSize(width, height, width / source_w)


@dataclass(frozen=True)
class ExpandPlan:
    """Result of planning a canvas expansion (outpaint)."""

    image_width: int  # original image size
    image_height: int
    new_width: int
    new_height: int
    offset_x: int  # where the old image lands inside the new canvas
    offset_y: int
    overlap: int  # band of old pixels that is regenerated for blending
    fill_rect: Rect  # bounding box of the generated area, new-canvas coords
    context: Rect  # region sent to the model, new-canvas coords

    @property
    def grows(self) -> tuple[bool, bool, bool, bool]:
        """(left, top, right, bottom)"""
        return (
            self.offset_x > 0,
            self.offset_y > 0,
            self.new_width - self.offset_x - self.image_width > 0,
            self.new_height - self.offset_y - self.image_height > 0,
        )

    @property
    def keep_rect(self) -> Rect:
        """Old pixels that are left untouched (old image minus overlap bands)."""
        left, top, right, bottom = self.grows
        x1 = self.offset_x + (self.overlap if left else 0)
        y1 = self.offset_y + (self.overlap if top else 0)
        x2 = self.offset_x + self.image_width - (self.overlap if right else 0)
        y2 = self.offset_y + self.image_height - (self.overlap if bottom else 0)
        return Rect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))


def plan_expand(
    image_w: int, image_h: int, left: int, top: int, right: int, bottom: int, profile: ModelProfile, overlap: int = 32
) -> ExpandPlan:
    """Plan an outpaint that grows the canvas by the given margins.

    The generated area is everything outside the old image plus an
    ``overlap`` band inside it on each growing side. The context is that
    area's bounding box grown by the usual padding, clamped to the new canvas.
    """
    if min(left, top, right, bottom) < 0:
        raise ValueError("margins must be non-negative")
    if left == top == right == bottom == 0:
        raise ValueError("at least one margin must be positive")
    new_w = image_w + left + right
    new_h = image_h + top + bottom
    canvas = Rect(0, 0, new_w, new_h)
    overlap = max(0, min(overlap, image_w // 2, image_h // 2))
    plan = ExpandPlan(image_w, image_h, new_w, new_h, left, top, overlap, canvas, canvas)
    keep = plan.keep_rect
    sides = sum(1 for m in (left, top, right, bottom) if m > 0)
    if sides >= 2:
        fill = canvas
    elif left:
        fill = Rect(0, 0, keep.x, new_h)
    elif right:
        fill = Rect(keep.right, 0, new_w - keep.right, new_h)
    elif top:
        fill = Rect(0, 0, new_w, keep.y)
    else:
        fill = Rect(0, keep.bottom, new_w, new_h - keep.bottom)
    if sides == 1:
        context = _strip_context(fill, canvas, profile, left, top, right, bottom)
    else:
        context = context_rect(fill, canvas, profile)
    return ExpandPlan(image_w, image_h, new_w, new_h, left, top, overlap, fill, context)


# Old pixels shown next to a one-sided outpaint strip, as a multiple of the
# strip's depth. The generic context (padding by the larger side) turned a
# 256 px strip along a 1440 px edge into the whole canvas, sampled at 0.47x:
# the new area became a thin, flat band in the working image.
EXPAND_CONTEXT_FACTOR = 2.0
# ...but never narrower than half the edge length: a 864x3000 strip was
# sampled at 296x1024, far below what SDXL can compose, and came back as a
# flat dark panel.
EXPAND_MIN_ASPECT = 0.5


def _strip_context(
    fill: Rect, canvas: Rect, profile: ModelProfile, left: int, top: int, right: int, bottom: int
) -> Rect:
    """Context for a single-side expansion: the strip plus a band of the old
    image as deep as ``EXPAND_CONTEXT_FACTOR`` times the strip (at least the
    model's minimum context, and at least ``EXPAND_MIN_ASPECT`` of the edge
    length), over the full length of the edge."""
    if left or right:
        depth = fill.width
        width = depth + max(profile.min_context, round(depth * EXPAND_CONTEXT_FACTOR))
        width = min(canvas.width, max(width, round(canvas.height * EXPAND_MIN_ASPECT)))
        x = 0 if left else canvas.width - width
        return Rect(x, 0, width, canvas.height)
    depth = fill.height
    height = depth + max(profile.min_context, round(depth * EXPAND_CONTEXT_FACTOR))
    height = min(canvas.height, max(height, round(canvas.width * EXPAND_MIN_ASPECT)))
    y = 0 if top else canvas.height - height
    return Rect(0, y, canvas.width, height)


# Largest margin of one expand round, as a fraction of the current canvas
# side it grows from (left and right against the width, top and bottom against
# the height). Every round renders all growing sides at once with the whole
# canvas as context; a margin this size keeps the old image at least two thirds
# of the canvas along each axis, so the model still reads the new area as more
# of the same scene. Larger expansions run in several such rounds.
EXPAND_ROUND_FRACTION = 0.25
EXPAND_MAX_ROUNDS = 8


def expand_rounds(
    image_w: int,
    image_h: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    max_fraction: float = EXPAND_ROUND_FRACTION,
    max_rounds: int = EXPAND_MAX_ROUNDS,
) -> list[tuple[int, int, int, int]]:
    """Margins (left, top, right, bottom) of each expand round.

    The fewest rounds in which no margin exceeds ``max_fraction`` of the canvas
    side it grows from (the canvas grows after each round). Each margin is
    split evenly over the rounds, larger parts first, so every round grows all
    requested sides at once. At most ``max_rounds`` rounds; when even that is
    not enough the margins are split over ``max_rounds``.
    """
    margins = (left, top, right, bottom)
    if min(margins) < 0:
        raise ValueError("margins must be non-negative")
    if not any(margins):
        raise ValueError("at least one margin must be positive")
    rounds: list[tuple[int, int, int, int]] = []
    for count in range(1, max(1, max_rounds) + 1):
        rounds = _split_margins(margins, count)
        if _rounds_fit(image_w, image_h, rounds, max_fraction):
            return rounds
    return rounds


def _split_margins(margins: tuple[int, int, int, int], count: int) -> list[tuple[int, int, int, int]]:
    parts = [[m // count + (1 if i < m % count else 0) for i in range(count)] for m in margins]
    return [(parts[0][i], parts[1][i], parts[2][i], parts[3][i]) for i in range(count)]


def _rounds_fit(width: int, height: int, rounds: list[tuple[int, int, int, int]], max_fraction: float) -> bool:
    for left, top, right, bottom in rounds:
        if max(left, right) > max(1, int(width * max_fraction)) or max(top, bottom) > max(
            1, int(height * max_fraction)
        ):
            return False
        width, height = width + left + right, height + top + bottom
    return True


# Smallest part of the photo that must stay inside the Expand frame (px), and
# the range of one margin: Generative Expand also crops (negative margins).
EXPAND_MIN_KEEP = 64
EXPAND_LIMIT = 4096


def fit_expand_margins(
    margins: tuple[int, int, int, int],
    image_w: int,
    image_h: int,
    previous: tuple[int, int, int, int] | None = None,
    limit: int = EXPAND_LIMIT,
    min_keep: int = EXPAND_MIN_KEEP,
) -> tuple[int, int, int, int]:
    """Margins (left, top, right, bottom; negative = crop) limited to +-``limit``
    and so that at least ``min_keep`` px of the photo (all of it when it is
    smaller) stay in the frame on each axis. Where crops are too deep, the
    sides that differ from ``previous`` give way first."""
    values = [int(max(-limit, min(limit, math.floor(m + 0.5)))) for m in margins]
    for extent, a, b in ((image_w, 0, 2), (image_h, 1, 3)):
        keep = min(min_keep, max(1, extent))
        excess = keep - (extent - max(0, -values[a]) - max(0, -values[b]))
        if excess <= 0:
            continue
        order = sorted((a, b), key=lambda i: 0 if previous is not None and previous[i] != values[i] else 1)
        for i in order:
            cut = min(excess, max(0, -values[i]))
            values[i] += cut
            excess -= cut
    return values[0], values[1], values[2], values[3]


def split_crop_grow(
    margins: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """(crop, grow): the pixels cut off and the pixels added on each side."""
    return tuple(max(0, -m) for m in margins), tuple(max(0, m) for m in margins)  # type: ignore[return-value]


def round_canvases(image_w: int, image_h: int, rounds: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    """Canvas size after each expand round."""
    out = []
    for left, top, right, bottom in rounds:
        image_w += left + right
        image_h += top + bottom
        out.append((image_w, image_h))
    return out


def clip_rect(x: int, y: int, width: int, height: int, canvas_w: int, canvas_h: int) -> Rect | None:
    """The part of a layer rectangle inside the canvas; None when nothing is."""
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(canvas_w, x + width), min(canvas_h, y + height)
    if x2 <= x1 or y2 <= y1:
        return None
    return Rect(x1, y1, x2 - x1, y2 - y1)


def mask_for_expand(plan: ExpandPlan) -> bytes:
    """8-bit mask covering ``plan.context``: 255 where new content goes."""
    ctx = plan.context
    keep = plan.keep_rect
    out = bytearray(b"\xff" * (ctx.width * ctx.height))
    kx1 = max(keep.x, ctx.x) - ctx.x
    kx2 = min(keep.right, ctx.right) - ctx.x
    ky1 = max(keep.y, ctx.y) - ctx.y
    ky2 = min(keep.bottom, ctx.bottom) - ctx.y
    if kx2 > kx1 and ky2 > ky1:
        zero = bytes(kx2 - kx1)
        for y in range(ky1, ky2):
            start = y * ctx.width + kx1
            out[start : start + len(zero)] = zero
    return bytes(out)


@dataclass(frozen=True)
class MaskParams:
    """Mask sizes derived from the selection (image pixels and working pixels)."""

    grow_image: int  # dilation of the selection, image px (layer mask + model mask)
    feather_image: int  # soft edge of the layer mask, image px
    grow_work: int  # dilation sent to the model, working px
    blur_work: int  # model-side blur, working px, never larger than grow_work


def mask_params(
    selection: Rect,
    scale: float,
    grow_percent: float = 15.0,
    feather_percent: float = 6.0,
    grow_max: int = 48,
    feather_max: int = 24,
    grow_min: int = 6,
) -> MaskParams:
    """Scale mask growth and feathering with the selection instead of using fixed
    pixel radii. A fixed 24 px feather on a 38 px selection leaves no fully
    opaque pixel, so the object shows through; a few percent, capped, does not.
    The model-side blur never exceeds the growth so the original selection stays
    at mask value 1.0 (ComfyUI only treats >= 1.0 as "inpaint here")."""
    side = max(1, max(selection.width, selection.height))
    grow_image = int(max(grow_min, min(grow_max, round(side * grow_percent / 100.0))))
    feather_image = int(max(1, min(feather_max, round(side * feather_percent / 100.0))))
    grow_work = max(1, round(grow_image * scale))
    blur_work = max(0, min(grow_work, round(feather_image * scale)))
    return MaskParams(grow_image, feather_image, grow_work, blur_work)


# Disk-to-GPU rate assumed for loading model files (bytes per second); slow
# laptop disks plus dtype conversion and offloading.
MODEL_LOAD_RATE = 60_000_000
# Floor of the timeout when the request uses a model the server has not just
# run (first request after a start, or a different model): FLUX Fill Q4 on an
# 8 GB card timed out at 153 s on its cold first case while warm cases took
# 97 to 144 s.
COLD_TIMEOUT_FLOOR = 600.0


def estimate_timeout(
    steps: int,
    batch: int,
    pixels: int,
    seconds_per_step_mpx: float = 1.5,
    floor: float = 120.0,
    model_load: float = 90.0,
    model_bytes: int = 0,
    cold: bool = False,
) -> float:
    """Rough upper bound for a generation, used as the request timeout.

    ``model_bytes`` (the size of the model files on disk) adds their load
    time at ``MODEL_LOAD_RATE``; a ``cold`` request (the server has not just
    run this model) gets at least ``COLD_TIMEOUT_FLOOR`` seconds."""
    mpx = pixels / 1_000_000
    load = model_load + max(0, int(model_bytes)) / MODEL_LOAD_RATE
    estimate = max(floor, load + steps * batch * max(0.25, mpx) * seconds_per_step_mpx * 2)
    return max(estimate, COLD_TIMEOUT_FLOOR) if cold else estimate


def seed_or_random(seed: int | None, rng: random.Random | None = None) -> int:
    """Return ``seed`` unless negative/None, in which case draw a 32-bit seed."""
    if seed is not None and seed >= 0:
        return int(seed)
    return (rng or random).randrange(0, 2**32 - 1)


# Margin around the selection, as a multiple of its larger side. Removal keeps a
# wide context (texture continuation); adding prompted content works better with a
# tighter crop, where the hole is a larger part of the frame and the surroundings
# do not outvote the prompt (SD 1.5 otherwise continues a flat sky).
REMOVAL_PADDING = 1.0
PROMPT_PADDING = 0.5
# LaMa/MAT see the context at 512 px at most: a 3x context around a 400 px
# selection is shrunk 2.4x and comes back soft. A 2x context keeps the
# selection near 1:1 while still giving the model surroundings to continue.
REMOVAL_MODEL_PADDING = 0.5


def fill_padding(removal: bool, prompt_free_model: bool = False) -> float:
    if prompt_free_model:
        return REMOVAL_MODEL_PADDING
    return REMOVAL_PADDING if removal else PROMPT_PADDING


# Pre-fill of the hole before sampling, as (style, mode) for the workflow's
# INPAINT_MaskedFill. Every architecture gets the border fill (navier-stokes
# plus blur) for a full-strength fill. SD 1.5 used a neutral grey hole so it
# would follow the prompt; on flat or bright contexts the inpainting
# checkpoints (dreamshaper-8 most) read the grey block as a separate picture
# and painted a pasted panel or a pale frame into it (balloon, spoon,
# strawberry, suede shoe). The border fill keeps the prompted object and loses
# the panel (docs/evidence/daytime-2026-10-09/pipeline/README.md).
NEUTRAL_PREFILL_ARCHS: tuple[str, ...] = ()


def prefill_for(removal: bool, strength: float, arch: str) -> tuple[str, str]:
    if removal:
        return "border", "navier-stokes"  # nothing of the removed object survives
    if strength < 1.0:
        return "blur", "navier-stokes"  # partial strength keeps the original
    if arch in NEUTRAL_PREFILL_ARCHS:
        return "border", "neutral"
    return "border", "navier-stokes"


# Longest working side for prompted SD 1.5 fills whose context is downscaled
# anyway. At 768 px (above the model's native 512) a large selection came back
# as a flat, untextured block; at 640 px the same fill has brick texture.
SD15_PROMPT_MAX_SIDE = 640


def fill_profile(profile: ModelProfile, context: Rect, arch: str, removal: bool) -> ModelProfile:
    """Profile for the working size of a fill: for a prompted SD 1.5 fill whose
    context needs no upscaling (shorter side at least ``profile.min_side``) the
    longest side is capped at ``SD15_PROMPT_MAX_SIDE``. Small contexts keep the
    full ``max_side`` so their upscale to ``min_side`` is not limited."""
    if removal or arch != "sd15" or min(context.width, context.height) < profile.min_side:
        return profile
    return replace(profile, max_side=min(profile.max_side, max(profile.min_side, SD15_PROMPT_MAX_SIDE)))


def color_match_useful(selection: Rect, context: Rect, image: Rect, min_reference: float = 0.15) -> bool:
    """Colour matching corrects the generated crop against the untouched pixels
    around the selection. Without enough of them (selection covering most of the
    context, or touching three or more canvas edges) it has no reference and
    shifts the whole result (a whole-image fill came back tinted)."""
    sel = selection.intersect(context)
    if context.area <= 0:
        return False
    if 1.0 - sel.area / context.area < min_reference:
        return False
    edges = sum(
        (
            selection.x <= image.x,
            selection.y <= image.y,
            selection.right >= image.right,
            selection.bottom >= image.bottom,
        )
    )
    return edges < 3


# Strength of the colour match for prompted fills (removal always uses 1.0).
PROMPT_COLOR_MATCH = 1.0


def color_match_strength(selection: Rect, context: Rect, image: Rect, removal: bool) -> float:
    """INPAINT_ColorMatch strength for a fill: 0 when there is no reference
    (``color_match_useful``), otherwise full for removal and
    ``PROMPT_COLOR_MATCH`` for prompted fills."""
    if not color_match_useful(selection, context, image):
        return 0.0
    return 1.0 if removal else PROMPT_COLOR_MATCH


def layer_mask_grow(mask: MaskParams, removal: bool) -> int:
    """Dilation of the selection for the GIMP layer mask.

    Removal grows the full amount so the regenerated rim of the object is
    covered. A prompted fill grows only by the feather radius (GIMP's feather
    is centred on the edge), so the mask is fully opaque over the selection and
    fades outward; growing further exposes the model's repainted context as a
    pale band around the fill. Never more than the removal grow. (Showing the
    whole growth on upscaled fills did not change the clipped watermelon,
    ladybug and earring cases.)
    """
    if removal:
        return mask.grow_image
    return max(1, min(mask.grow_image, mask.feather_image))


def flatten_over_mean(rgba: bytes) -> bytes:
    """Composite RGBA pixels over the alpha-weighted mean colour of the visible
    ones and return opaque RGBA. Models read RGB only, so transparent pixels
    would otherwise reach them as black. Fully opaque input is returned as is."""
    alpha = rgba[3::4]
    n = len(alpha)
    if n == 0 or alpha.count(255) == n:
        return rgba
    total = sum(alpha)
    if total == 0:
        mean = (128, 128, 128)
    else:
        mean = tuple(round(sum(map(int.__mul__, rgba[c::4], alpha)) / total) for c in range(3))
    out = bytearray(len(rgba))
    for c in range(3):
        m = mean[c]
        out[c::4] = bytes(map(lambda v, a, m=m: (v * a + m * (255 - a) + 127) // 255, rgba[c::4], alpha))
    out[3::4] = b"\xff" * n
    return bytes(out)


# ------------------------------------------------------------- fill planning --

# Lowest working scale (working px per image px) of a generation pass. Below it
# a large selection comes back soft: snow, grass and brick at 0.3 to 0.5 were
# visibly calmer than the photo. A context that would need a lower scale is
# cropped to a smaller margin first and split into tiles when even that is not
# enough.
MIN_WORK_SCALE = {"sd15": 0.6, "sdxl": 0.75}
# Smallest margin (image px) kept around the selection when the context is
# shrunk to hold the minimum working scale.
MIN_TILE_MARGIN = 64
# A tile's shorter side, in working pixels, is at least this much. A 1620x89
# strip was worked as a 28 px band inside a 640x408 image; now each tile shows
# the strip with at least 256 working px across it.
MIN_TILE_CONTENT = 256
# Overlap of neighbouring tiles (image px); the later tile blends into the
# earlier one over this band.
TILE_OVERLAP = 128
# Tiles of a generation pass, at most, per architecture; the scale is lowered
# (never below the minimum) until the selection fits in this many.
MAX_GEN_TILES = {"sd15": 4, "sdxl": 2}
# Margin (image px) of the refine region around the selection: covers the mask
# growth and blur of the main pass.
REFINE_MARGIN = 48
# Layout pass of a tiled fill: before the tiles, the whole context is generated
# in one pass so every tile starts from the same picture (without it each tile
# painted its own scene: a white backdrop and a heel instead of a suede shoe, a
# flat slab instead of a roof). Its scale is the single-pass scale, raised to
# at least LAYOUT_MIN_SCALE, with the working long side capped at
# LAYOUT_MAX_SIDE_FACTOR times the model's max side.
LAYOUT_MIN_SCALE = 0.45
LAYOUT_MAX_SIDE_FACTOR = 1.5
# Which tiled fills get the layout pass, by how much of a tile the selection
# fills across (its shorter side at the tile scale over the tiles' shorter
# working side). Under LAYOUT_STRIP_FRACTION the tiles are mostly context and
# each painted its own scene (SDXL roof strip: a flat slab): every architecture
# gets the layout pass. For LAYOUT_ARCHS a selection filling at least
# LAYOUT_AREA_FRACTION of a tile gets it too (the suede shoe split into four
# tiles became four separate shoes). Not for SDXL areas: there the layout pass
# brought back the low-scale failures the tiles fixed (bricks twice their size,
# a grey sleeve instead of red leather), and not for SD 1.5 strips in between,
# whose tiles already drew the roof tiles (realistic-vision-v6).
LAYOUT_ARCHS = ("sd15",)
LAYOUT_STRIP_FRACTION = 0.2
LAYOUT_AREA_FRACTION = 0.5


@dataclass(frozen=True)
class Tile:
    """One window of a tiled pass, in context coordinates."""

    rect: Rect  # pixels the model sees (context coordinates)
    work_width: int  # working size of this window
    work_height: int
    done: tuple[Rect, ...] = ()  # parts finished by earlier tiles (tile coordinates)


@dataclass(frozen=True)
class FillPlan:
    """How a fill is generated: the context crop, the single-pass working size,
    and optional tiled main and refine passes."""

    context: Rect
    work: WorkingSize  # single-pass working size (used when ``tiles`` is empty)
    tiles: tuple[Tile, ...] = ()  # tiled main pass (empty: one pass at ``work``)
    tile_scale: float = 1.0  # working scale of the tiled main pass
    refine_tiles: tuple[Tile, ...] = ()  # full-resolution refine pass (empty: none)
    # True: ``work`` is the layout pass over the whole context, and the tiles
    # only add detail to it at a reduced denoise.
    layout: bool = False

    @property
    def main_scale(self) -> float:
        return self.tile_scale if self.tiles else self.work.scale


def _rect_with_margin(selection: Rect, bounds: Rect, mx: int, my: int) -> Rect:
    grown = Rect(selection.x - mx, selection.y - my, selection.width + 2 * mx, selection.height + 2 * my)
    return grown.intersect(bounds)


def _spans(start: int, length: int, window: int, overlap: int) -> list[tuple[int, int]]:
    """Evenly spaced windows of ``window`` px, overlapping by at least
    ``overlap``, covering [start, start + length)."""
    if length <= window:
        return [(start, length)]
    step_max = max(1, window - overlap)
    count = 1 + -(-(length - window) // step_max)
    step = (length - window) / (count - 1)
    return [(start + round(i * step), window) for i in range(count)]


def _grid(region: Rect, tw: int, th: int, overlap: int) -> list[Rect]:
    xs = _spans(region.x, region.width, tw, overlap)
    ys = _spans(region.y, region.height, th, overlap)
    return [Rect(x, y, w, h) for y, h in ys for x, w in xs]


def _with_done(rects: list[Rect], work: list[tuple[int, int]], overlap: int) -> tuple[Tile, ...]:
    """Tiles in order; each records the parts of earlier tiles it overlaps,
    minus a band of half the overlap on the side facing it, so the later tile
    repaints that band and blends into the earlier one."""
    tiles = []
    half = overlap // 2
    for i, r in enumerate(rects):
        done = []
        for prev in rects[:i]:
            x1, y1, x2, y2 = prev.x, prev.y, prev.right, prev.bottom
            if r.x > prev.x:
                x2 = min(x2, max(r.x + half, x1))
            if r.right < prev.right:
                x1 = max(x1, min(r.right - half, x2))
            if r.y > prev.y:
                y2 = min(y2, max(r.y + half, y1))
            if r.bottom < prev.bottom:
                y1 = max(y1, min(r.bottom - half, y2))
            part = Rect(x1, y1, x2 - x1, y2 - y1).intersect(r)
            if not part.is_empty():
                done.append(part.relative_to(r))
        tiles.append(Tile(r, work[i][0], work[i][1], tuple(done)))
    return tuple(tiles)


def _tile_layout(sel: Rect, bounds: Rect, side: int, min_short: int, margin: int, overlap: int) -> list[Rect]:
    """Windows of at most ``side`` px covering ``sel`` plus ``margin`` inside
    ``bounds``; the margin across a thin selection is raised so every window
    is at least ``min_short`` px across."""
    mx = max(margin, (min_short - sel.width + 1) // 2)
    my = max(margin, (min_short - sel.height + 1) // 2)
    # moved inside the bounds rather than cut, so a strip along the image edge
    # keeps its full width across
    region = clamp_rect(Rect(sel.x - mx, sel.y - my, sel.width + 2 * mx, sel.height + 2 * my), bounds)
    return _grid(region, min(side, region.width), min(side, region.height), overlap)


def layout_work(
    context: Rect, profile: ModelProfile, single: WorkingSize, min_scale: float = LAYOUT_MIN_SCALE
) -> WorkingSize:
    """Working size of the layout pass of a tiled fill: the single-pass scale
    ``single.scale`` raised to ``LAYOUT_MIN_SCALE``, but never with a working
    long side above ``LAYOUT_MAX_SIDE_FACTOR * profile.max_side`` nor above 1:1."""
    long_side = max(context.width, context.height)
    scale = max(single.scale, min_scale)
    scale = min(scale, LAYOUT_MAX_SIDE_FACTOR * profile.max_side / long_side, 1.0)
    scale = max(scale, single.scale)
    m = profile.multiple
    return WorkingSize(
        round_to_multiple(round(context.width * scale), m), round_to_multiple(round(context.height * scale), m), scale
    )


def wants_layout(arch: str, selection: Rect, tiles: tuple[Tile, ...], tile_scale: float) -> bool:
    """Whether a tiled fill gets the layout pass (see ``LAYOUT_STRIP_FRACTION``
    and ``LAYOUT_AREA_FRACTION``)."""
    if not tiles:
        return False
    across = min(selection.width, selection.height) * tile_scale
    fraction = across / min(min(t.work_width, t.work_height) for t in tiles)
    if fraction < LAYOUT_STRIP_FRACTION:
        return True
    return arch in LAYOUT_ARCHS and fraction >= LAYOUT_AREA_FRACTION


def plan_fill(
    selection: Rect,
    image: Rect,
    profile: ModelProfile,
    arch: str,
    removal: bool,
    padding_ratio: float,
    refine: bool = False,
    min_scale: float | None = None,
    max_tiles: int | None = None,
    overlap: int = TILE_OVERLAP,
    layout: bool = False,
    layout_min_scale: float = LAYOUT_MIN_SCALE,
) -> FillPlan:
    """Plan a Generative Fill.

    The context and single-pass working size are the classic ones
    (``context_rect``, ``working_size`` with ``fill_profile``). When that
    working scale is below ``MIN_WORK_SCALE`` for the architecture, the margin
    shrinks (keeping at least ``MIN_TILE_MARGIN``) until the context fits at
    the minimum scale. When the selection itself is too large for that, the
    main pass is split into overlapping tiles at one common scale: the highest
    of 1.0, 0.95, ... down to the minimum that needs no more tiles than
    ``max_tiles`` (default ``MAX_GEN_TILES`` for the architecture) or, if more,
    than the minimum scale needs. Every tile is at least
    ``MIN_TILE_CONTENT`` (or three quarters of the model's ``min_side``)
    working pixels across.

    With ``layout`` and tiles (``wants_layout``), ``work`` becomes the layout
    pass over the whole context (``layout_work``) and the tiles refine it.

    With ``refine`` and a main scale below 1.0, a second pass at full
    resolution covers the selection plus ``REFINE_MARGIN``, tiled at the
    model's ``max_side``.
    """
    ctx = context_rect(selection, image, profile, padding_ratio=padding_ratio)
    prof = fill_profile(profile, ctx, arch, removal)
    work = working_size(ctx.width, ctx.height, prof)
    smin = MIN_WORK_SCALE.get(arch, 0.0) if min_scale is None else min_scale
    tiles: tuple[Tile, ...] = ()
    tile_scale = 1.0
    if work.scale < smin:
        limit = int(prof.max_side / smin)
        mx = (min(limit, ctx.width) - selection.width) // 2
        my = (min(limit, ctx.height) - selection.height) // 2
        if min(mx, my) >= MIN_TILE_MARGIN:
            far = Rect(-(10**9), -(10**9), 2 * 10**9, 2 * 10**9)
            smaller = clamp_rect(_rect_with_margin(selection, far, mx, my), image)
            sprof = fill_profile(profile, smaller, arch, removal)
            swork = working_size(smaller.width, smaller.height, sprof)
            if swork.scale >= smin:
                ctx, work = smaller, swork
        if work.scale < smin:
            sel_c = selection.relative_to(ctx)
            whole = Rect(0, 0, ctx.width, ctx.height)
            layouts = []
            for step in range(20):
                scale = round(max(smin, 1.0 - 0.05 * step), 4)
                side = int(prof.max_side / scale)
                min_short = int(max(MIN_TILE_CONTENT, 0.75 * profile.min_side) / scale) + 1
                margin = max(
                    MIN_TILE_MARGIN, min(side // 8, round(max(selection.width, selection.height) * padding_ratio))
                )
                layouts.append((scale, _tile_layout(sel_c, whole, side, min_short, margin, overlap)))
                if scale <= smin:
                    break
            allowed = max(MAX_GEN_TILES.get(arch, 2) if max_tiles is None else max_tiles, len(layouts[-1][1]))
            tile_scale, rects = next((s_, r_) for s_, r_ in layouts if len(r_) <= allowed)
            sizes = [
                (
                    round_to_multiple(round(r.width * tile_scale), profile.multiple),
                    round_to_multiple(round(r.height * tile_scale), profile.multiple),
                )
                for r in rects
            ]
            tiles = _with_done(rects, sizes, overlap)
    plan = FillPlan(ctx, work, tiles, tile_scale if tiles else 1.0)
    if layout and wants_layout(arch, selection, tiles, tile_scale):
        plan = replace(plan, work=layout_work(ctx, prof, work, layout_min_scale), layout=True)
    if refine and plan.main_scale < 0.999:
        plan = replace(plan, refine_tiles=refine_layout(selection, ctx, profile, overlap=overlap))
    return plan


def refine_layout(
    selection: Rect, ctx: Rect, profile: ModelProfile, margin: int = REFINE_MARGIN, overlap: int = TILE_OVERLAP
) -> tuple[Tile, ...]:
    """Full-resolution windows (scale 1, at most ``profile.max_side`` a side)
    covering the selection plus ``margin`` inside the context. A region
    smaller than the model's ``min_side`` is grown to it where the context
    allows, so the model sees enough around a small selection."""
    whole = Rect(0, 0, ctx.width, ctx.height)
    region = _rect_with_margin(selection.relative_to(ctx), whole, margin, margin)
    need_w = max(region.width, min(profile.min_side, whole.width))
    need_h = max(region.height, min(profile.min_side, whole.height))
    if (need_w, need_h) != (region.width, region.height):
        cx, cy = region.x + region.width / 2, region.y + region.height / 2
        region = clamp_rect(Rect(round(cx - need_w / 2), round(cy - need_h / 2), need_w, need_h), whole)
    m = profile.multiple
    up_w = min(whole.width, -(-region.width // m) * m)
    up_h = min(whole.height, -(-region.height // m) * m)
    region = clamp_rect(Rect(region.x, region.y, up_w, up_h), whole)
    side = profile.max_side // m * m
    tw = min(side, region.width // m * m) or region.width
    th = min(side, region.height // m * m) or region.height
    rects = _grid(region, tw, th, overlap)
    return _with_done(rects, [(r.width, r.height) for r in rects], overlap)


def plan_native_removal(selection: Rect, image: Rect) -> FillPlan:
    """LaMa at the image's own pixels: the classic removal context
    (``REMOVAL_MODEL_PADDING``) at scale 1, never resized. OpenFill's node pads it to
    a multiple of 8 and, when one pass does not fit the graphics memory, works it in
    large overlapping windows at 1:1 (comfy_nodes/plan.py)."""
    ctx = context_rect(selection, image, INPAINT_MODEL, padding_ratio=REMOVAL_MODEL_PADDING)
    return FillPlan(ctx, WorkingSize(ctx.width, ctx.height, 1.0))


# Tiled removal (MAT): the LaMa/MAT node pads its input to a square and resizes it to
# 256 px (LaMa) or 512 px (MAT), so a large context came back smeared. Windows
# of REMOVAL_TILE image px keep the node's scale near 256 / REMOVAL_TILE.
REMOVAL_TILE = 512
REMOVAL_TILE_OVERLAP = 128
REMOVAL_TILE_MARGIN = 64
# Only a band is tiled: a hole whose shorter side exceeds this fraction of the
# tile leaves windows that are nearly all hole, and the node fills those with a
# flat grey (a dark blob in place of the tea cup on a white background).
REMOVAL_BAND = 0.75


def plan_removal(
    selection: Rect,
    image: Rect,
    tile: int = REMOVAL_TILE,
    overlap: int = REMOVAL_TILE_OVERLAP,
    margin: int = REMOVAL_TILE_MARGIN,
) -> FillPlan | None:
    """Tiled plan for a prompt-free removal, or None when the classic context
    (``REMOVAL_MODEL_PADDING``) already fits in one ``tile``.

    Windows of ``tile`` px (or less where the image is smaller) are centred on
    the selection, cover it plus ``margin`` and overlap by at least
    ``overlap``; they run at 1:1 and later ones mark the parts of earlier ones
    they overlap as done. The context is the union of the windows. Only a
    band (shorter side at most ``REMOVAL_BAND * tile``) is tiled, so every
    window keeps known pixels across the hole."""
    if tile <= 0 or min(selection.width, selection.height) > REMOVAL_BAND * tile:
        return None
    ctx = context_rect(selection, image, INPAINT_MODEL, padding_ratio=REMOVAL_MODEL_PADDING)
    if max(ctx.width, ctx.height) <= tile:
        return None
    rects = _tile_layout(selection, image, tile, tile - 1, margin, overlap)
    x1, y1 = min(r.x for r in rects), min(r.y for r in rects)
    x2, y2 = max(r.right for r in rects), max(r.bottom for r in rects)
    union = Rect(x1, y1, x2 - x1, y2 - y1)
    local = [r.relative_to(union) for r in rects]
    tiles = _with_done(local, [(r.width, r.height) for r in local], overlap)
    return FillPlan(union, WorkingSize(union.width, union.height, 1.0), tiles, 1.0)
