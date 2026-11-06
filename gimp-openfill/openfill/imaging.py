# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Pixel work for the cloud backend, standard library only.

A cloud model gets a whole picture and gives a whole picture back, at a size of
its own choosing. These helpers read the PNG it returns, fit what is sent to the
aspect ratios the service offers, and put the result back through the selection
so that no pixel outside it changes. Pure Python: kept to the pixels that need it.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass

from .png import PngError, encode, read_header

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Largest picture accepted from a cloud service (they return about 1 to 4 megapixels).
MAX_RESULT_PIXELS = 25_000_000
# Largest picture decoded at all (OpenFill's own context crops can be big).
MAX_PIXELS = 100_000_000


@dataclass(frozen=True)
class Rgba:
    """An 8-bit RGBA picture, rows top to bottom, no padding."""

    width: int
    height: int
    data: bytes

    def png(self) -> bytes:
        return encode(self.width, self.height, self.data, 4)


def sniff(data: bytes) -> str:
    """The image format of ``data``: png, jpeg, webp, gif or "" when unknown."""
    if data.startswith(_SIGNATURE):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return ""


# ------------------------------------------------------------------ decode ----


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, stride: int, height: int, bpp: int) -> bytes:
    out = bytearray(stride * height)
    prior = bytearray(stride)
    pos = 0
    for y in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        if len(line) != stride:
            raise PngError("PNG data ends early")
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prior, strict=True))
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prior[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                if i >= bpp:
                    line[i] = (line[i] + _paeth(line[i - bpp], prior[i], prior[i - bpp])) & 0xFF
                else:
                    line[i] = (line[i] + prior[i]) & 0xFF
        elif kind != 0:
            raise PngError(f"unknown PNG filter {kind}")
        out[y * stride : (y + 1) * stride] = line
        prior = line
    return bytes(out)


def _unpack_bits(row: bytes, depth: int, count: int) -> list[int]:
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    values = []
    for byte in row:
        for k in range(per_byte):
            values.append((byte >> (8 - depth * (k + 1))) & mask)
    return values[:count]


def decode_png(data: bytes, max_pixels: int = MAX_PIXELS) -> Rgba:
    """Decode a non-interlaced PNG of any colour type to 8-bit RGBA.

    At most ``max_pixels`` pixels, and the compressed data may not inflate to more than its
    header says: a small file that would unpack to gigabytes is refused before that happens."""
    if not data.startswith(_SIGNATURE):
        raise PngError("not a PNG file")
    pos = len(_SIGNATURE)
    header = None
    palette = b""
    trns = b""
    idat = []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise PngError("PNG has no IHDR chunk")
    width, height, depth, color, _comp, _filter, interlace = header
    if interlace:
        raise PngError("interlaced PNG is not supported")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None or depth not in (1, 2, 4, 8, 16):
        raise PngError(f"unsupported PNG type (colour {color}, depth {depth})")
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise PngError(f"unsupported PNG size {width}x{height}")
    bits = channels * depth
    stride = (width * bits + 7) // 8
    expected = (stride + 1) * height
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(idat), expected + 1)
        more = inflater.unconsumed_tail and inflater.decompress(inflater.unconsumed_tail, 1)
    except zlib.error as err:
        raise PngError(f"damaged PNG data: {err}") from None
    if len(raw) > expected or more:
        raise PngError("PNG data is larger than its size says")
    pixels = _unfilter(raw, stride, height, max(1, bits // 8))
    if depth == 16:  # keep the high byte of each sample
        pixels = pixels[0::2]
        depth = 8
    n = width * height
    out = bytearray(n * 4)
    if color == 6:
        return Rgba(width, height, pixels)
    if color == 2:
        out[0::4], out[1::4], out[2::4] = pixels[0::3], pixels[1::3], pixels[2::3]
        out[3::4] = b"\xff" * n
        if len(trns) == 6:
            key = (trns[1], trns[3], trns[5])
            for i in range(n):
                if (pixels[3 * i], pixels[3 * i + 1], pixels[3 * i + 2]) == key:
                    out[4 * i + 3] = 0
        return Rgba(width, height, bytes(out))
    if color == 4:
        out[0::4] = out[1::4] = out[2::4] = pixels[0::2]
        out[3::4] = pixels[1::2]
        return Rgba(width, height, bytes(out))
    # gray (0) or palette (3), possibly packed below 8 bits
    if depth == 8:
        values = pixels
    else:
        rows = (pixels[y * stride : (y + 1) * stride] for y in range(height))
        values = bytes(v for row in rows for v in _unpack_bits(row, depth, width))
    if color == 0:
        scale = 255 // ((1 << depth) - 1)
        gray = values if scale == 1 else bytes(v * scale for v in values)
        out[0::4] = out[1::4] = out[2::4] = gray
        out[3::4] = b"\xff" * n
        if len(trns) == 2:
            key = trns[1] if depth == 8 else trns[1] & ((1 << depth) - 1)
            for i in range(n):
                if values[i] == key:
                    out[4 * i + 3] = 0
        return Rgba(width, height, bytes(out))
    if not palette:
        raise PngError("palette PNG without a palette")
    table = [
        bytes((palette[3 * i], palette[3 * i + 1], palette[3 * i + 2], trns[i] if i < len(trns) else 255))
        for i in range(len(palette) // 3)
    ]
    try:
        return Rgba(width, height, b"".join(table[v] for v in values))
    except IndexError:
        raise PngError("palette index out of range") from None


def picture_size(data: bytes, kind: str) -> tuple[int, int]:
    """(width, height) from the header of a PNG, JPEG or WebP, read without decoding it."""
    try:
        if kind == "png":
            width, height, _depth, _color = read_header(data)
            return width, height
        if kind == "jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 1 if marker == 0xFF else 2
                    continue
                length = struct.unpack(">H", data[i + 2 : i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                    if width and height:
                        return width, height
                    break
                i += 2 + length
        elif kind == "webp":
            chunk = data[12:16]
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L" and data[20] == 0x2F:
                b0, b1, b2, b3 = data[21:25]
                return 1 + (b0 | (b1 & 0x3F) << 8), 1 + (b1 >> 6 | b2 << 2 | (b3 & 0x0F) << 10)
            if chunk == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    except (struct.error, IndexError, ValueError, PngError):
        pass
    raise PngError(f"the {kind.upper()} picture's size cannot be read")


def to_rgba(width: int, height: int, data: bytes, channels: int) -> Rgba:
    """Raw 8-bit pixels with 1 to 4 channels as RGBA."""
    n = width * height
    if len(data) != n * channels:
        raise PngError(f"pixel buffer has {len(data)} bytes, expected {n * channels}")
    if channels == 4:
        return Rgba(width, height, bytes(data))
    out = bytearray(n * 4)
    if channels == 3:
        out[0::4], out[1::4], out[2::4] = data[0::3], data[1::3], data[2::3]
        out[3::4] = b"\xff" * n
    elif channels == 2:
        out[0::4] = out[1::4] = out[2::4] = data[0::2]
        out[3::4] = data[1::2]
    else:
        out[0::4] = out[1::4] = out[2::4] = data
        out[3::4] = b"\xff" * n
    return Rgba(width, height, bytes(out))


def gray_of(image: Rgba) -> bytes:
    """The red channel of an RGBA picture (a mask sent as RGB or RGBA is gray)."""
    return image.data[0::4]


# ------------------------------------------------------------- geometry ----


def shrink(image: Rgba, factor: int) -> Rgba:
    """Every ``factor``-th pixel in both directions (fast, no filtering)."""
    if factor <= 1:
        return image
    w, h = image.width, image.height
    nw, nh = (w + factor - 1) // factor, (h + factor - 1) // factor
    out = bytearray(nw * nh * 4)
    stride = w * 4
    for ny in range(nh):
        row = image.data[ny * factor * stride : (ny * factor + 1) * stride]
        dest = ny * nw * 4
        for c in range(4):
            out[dest + c : dest + nw * 4 : 4] = row[c :: 4 * factor][:nw]
    return Rgba(nw, nh, bytes(out))


def shrink_gray(data: bytes, width: int, height: int, factor: int) -> tuple[bytes, int, int]:
    if factor <= 1:
        return data, width, height
    nw, nh = (width + factor - 1) // factor, (height + factor - 1) // factor
    rows = (data[y * factor * width : y * factor * width + width][::factor][:nw] for y in range(nh))
    return b"".join(rows), nw, nh


@dataclass(frozen=True)
class Padding:
    """Where a picture sits inside the larger canvas sent to the service."""

    width: int  # padded size
    height: int
    left: int
    top: int
    ratio: str  # "W:H" as the service names it


def ratio_value(ratio: str) -> float:
    w, h = ratio.split(":")
    return float(w) / float(h)


def nearest_ratio(width: int, height: int, ratios: tuple[str, ...]) -> str:
    """The ratio whose shape is closest to ``width`` x ``height`` (compared on a log scale)."""
    want = math.log(width / height)
    return min(ratios, key=lambda r: abs(math.log(ratio_value(r)) - want))


def pad_plan(width: int, height: int, ratios: tuple[str, ...]) -> Padding:
    """Grow ``width`` x ``height`` (centred) to the nearest of ``ratios``, so the
    service has no reason to stretch or crop it."""
    ratio = nearest_ratio(width, height, ratios)
    r = ratio_value(ratio)
    if width / height < r:
        pw, ph = max(width, round(height * r)), height
    else:
        pw, ph = width, max(height, round(width / r))
    return Padding(pw, ph, (pw - width) // 2, (ph - height) // 2, ratio)


def pad_edges(image: Rgba, pad: Padding) -> Rgba:
    """``image`` placed in the padded canvas; the border repeats its edge pixels."""
    w, h = image.width, image.height
    right = pad.width - w - pad.left
    rows = []
    for y in range(pad.height):
        sy = min(h - 1, max(0, y - pad.top))
        row = image.data[sy * w * 4 : (sy + 1) * w * 4]
        rows.append(row[:4] * pad.left + row + row[-4:] * right)
    return Rgba(pad.width, pad.height, b"".join(rows))


def pad_gray(data: bytes, width: int, height: int, pad: Padding, fill: int = 0) -> bytes:
    """A gray mask placed in the padded canvas; the border is ``fill``."""
    right = pad.width - width - pad.left
    blank = bytes([fill]) * pad.width
    rows = [blank] * pad.top
    for y in range(height):
        rows.append(bytes([fill]) * pad.left + data[y * width : (y + 1) * width] + bytes([fill]) * right)
    rows += [blank] * (pad.height - height - pad.top)
    return b"".join(rows)


def mask_rgb(mask: bytes, width: int, height: int) -> bytes:
    """A black and white PNG of ``mask`` (white = change here), for a model that reads pictures."""
    return encode(width, height, bytes(255 if v >= 128 else 0 for v in mask), 1)


def edit_mask_png(mask: bytes, width: int, height: int) -> bytes:
    """The OpenAI images/edits mask: transparent where the image may change, opaque elsewhere."""
    alpha = bytes(0 if v >= 128 else 255 for v in mask)
    out = bytearray(width * height * 4)
    out[3::4] = alpha
    return encode(width, height, bytes(out), 4)


# ------------------------------------------------------------ composite ----


def _inside_distance(mask: bytes, width: int, height: int, box: tuple[int, int, int, int], cap: int) -> list[int]:
    """Chessboard distance (at most ``cap``) from each pixel of ``box`` to the nearest
    unselected pixel; 0 for unselected pixels. Every pixel outside ``box`` but inside the
    picture is unselected. Past the picture's own edge nothing is unselected: a selection
    that reaches the edge (or Expand's new canvas) stays solid there instead of fading out."""
    x0, y0, x1, y1 = box
    gw = x1 - x0 + 2  # one pixel of border all round
    gh = y1 - y0 + 2
    d = [0] * (gw * gh)
    for gy in range(gh):
        for gx in range(gw) if gy in (0, gh - 1) else (0, gw - 1):
            px, py = x0 - 1 + gx, y0 - 1 + gy
            if not (0 <= px < width and 0 <= py < height):
                d[gy * gw + gx] = cap
    for y in range(y0, y1):
        row = y * width
        base = (y - y0 + 1) * gw + 1
        for x in range(x0, x1):
            if mask[row + x] >= 128:
                d[base + x - x0] = cap
    for y in range(1, gh - 1):
        for i in range(y * gw + 1, y * gw + gw - 1):
            if d[i]:
                d[i] = min(d[i], d[i - 1] + 1, d[i - gw - 1] + 1, d[i - gw] + 1, d[i - gw + 1] + 1)
    for y in range(gh - 2, 0, -1):
        for i in range(y * gw + gw - 2, y * gw, -1):
            if d[i]:
                d[i] = min(d[i], d[i + 1] + 1, d[i + gw - 1] + 1, d[i + gw] + 1, d[i + gw + 1] + 1)
    bw = x1 - x0
    return [d[(y + 1) * gw + x + 1] for y in range(y1 - y0) for x in range(bw)]


def mask_box(mask: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of the selected pixels (>= 128), None when none are."""
    ys = [y for y in range(height) if max(mask[y * width : (y + 1) * width], default=0) >= 128]
    if not ys:
        return None
    x0, x1 = width, 0
    for y in ys:
        row = mask[y * width : (y + 1) * width]
        first = next(i for i, v in enumerate(row) if v >= 128)
        last = width - 1 - next(i for i, v in enumerate(reversed(row)) if v >= 128)
        x0, x1 = min(x0, first), max(x1, last + 1)
    return x0, ys[0], x1, ys[-1] + 1


def overlay(
    generated: Rgba,
    mask: bytes,
    width: int,
    height: int,
    source: tuple[float, float, float, float],
    feather: int = 4,
) -> Rgba:
    """A layer of ``width`` x ``height`` that holds ``generated`` inside the selection only.

    Every pixel the mask does not select (below 128) is fully transparent, so the layer can
    change nothing outside the selection, whatever lies under it (transparency, 16-bit or
    float pixels). Within ``feather`` pixels of the selection's edge the layer fades in, so
    the edge does not show as a hard line. ``source`` is as in :func:`composite`.
    """
    return _paint(None, generated, mask, width, height, source, feather)


def composite(
    original: Rgba,
    generated: Rgba,
    mask: bytes,
    source: tuple[float, float, float, float],
    feather: int = 4,
) -> Rgba:
    """``original`` with the selected pixels taken from ``generated`` (the same blend as
    :func:`overlay` placed over ``original``); every unselected pixel is copied unchanged.

    ``source`` is the rectangle (x, y, width, height) of ``generated`` that shows the
    picture: the result is sampled from it with bilinear filtering, whatever size the
    service returned.
    """
    return _paint(original, generated, mask, original.width, original.height, source, feather)


def _paint(
    original: Rgba | None,
    generated: Rgba,
    mask: bytes,
    w: int,
    h: int,
    source: tuple[float, float, float, float],
    feather: int,
) -> Rgba:
    if len(mask) != w * h:
        raise PngError(f"mask has {len(mask)} bytes, expected {w * h}")
    out = bytearray(original.data) if original is not None else bytearray(w * h * 4)
    box = mask_box(mask, w, h)
    if box is None:
        return Rgba(w, h, bytes(out))
    feather = max(1, int(feather))
    dist = _inside_distance(mask, w, h, box, feather)
    gw, gh, gen = generated.width, generated.height, generated.data
    sx0, sy0, sw, sh = source
    fx, fy = sw / w, sh / h
    x0, y0, x1, y1 = box
    bw = x1 - x0
    # Column lookups, the same for every row: two source columns and the weight between them.
    cols = []
    for x in range(x0, x1):
        gx = min(max(sx0 + (x + 0.5) * fx - 0.5, 0.0), gw - 1.0)
        xa = int(gx)
        cols.append((xa * 4, min(xa + 1, gw - 1) * 4, gx - xa))
    lo = cols[0][0]
    hi = cols[-1][1] + 4
    for y in range(y0, y1):
        gy = min(max(sy0 + (y + 0.5) * fy - 0.5, 0.0), gh - 1.0)
        ya = int(gy)
        ty = gy - ya
        ra = gen[ya * gw * 4 + lo : ya * gw * 4 + hi]
        rb = gen[min(ya + 1, gh - 1) * gw * 4 + lo : min(ya + 1, gh - 1) * gw * 4 + hi]
        # the two source rows blended once, at the source's own resolution
        row = [a + (b - a) * ty for a, b in zip(ra, rb, strict=True)] if ty else list(ra)
        drow = (y - y0) * bw - x0
        base = y * w * 4
        for x in range(x0, x1):
            d = dist[drow + x]
            if not d:
                continue
            a, b, tx = cols[x - x0]
            a -= lo
            b -= lo
            i = base + x * 4
            if d >= feather:
                out[i] = int(row[a] + (row[b] - row[a]) * tx + 0.5)
                out[i + 1] = int(row[a + 1] + (row[b + 1] - row[a + 1]) * tx + 0.5)
                out[i + 2] = int(row[a + 2] + (row[b + 2] - row[a + 2]) * tx + 0.5)
                out[i + 3] = 255
                continue
            alpha = d / feather
            if original is None:  # a layer: the colour as generated, fading in by its alpha
                out[i] = int(row[a] + (row[b] - row[a]) * tx + 0.5)
                out[i + 1] = int(row[a + 1] + (row[b + 1] - row[a + 1]) * tx + 0.5)
                out[i + 2] = int(row[a + 2] + (row[b + 2] - row[a + 2]) * tx + 0.5)
                out[i + 3] = int(255 * alpha + 0.5)
                continue
            for k in range(3):
                value = row[a + k] + (row[b + k] - row[a + k]) * tx
                out[i + k] = int(out[i + k] + (value - out[i + k]) * alpha + 0.5)
            out[i + 3] = int(out[i + 3] + (255 - out[i + 3]) * alpha + 0.5)
    return Rgba(w, h, bytes(out))
