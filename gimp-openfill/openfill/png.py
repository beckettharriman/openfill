# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Minimal PNG encoder and header reader using only the standard library.

GIMP's bundled Python has no Pillow, and round-tripping pixels through
temporary XCF/PNG exports would invoke GIMP's export machinery. Encoding the
few buffers we upload (an RGBA crop and an 8-bit mask) by hand is small and
dependency-free.
"""

from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNG color types for 8-bit samples.
COLOR_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}  # channels -> color type


class PngError(ValueError):
    """Raised for malformed PNG input or unsupported encode parameters."""


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def encode(width: int, height: int, pixels: bytes, channels: int, compress_level: int = 1) -> bytes:
    """Encode interleaved 8-bit ``pixels`` (row-major, no padding) as PNG.

    ``channels`` is 1 (gray), 2 (gray+alpha), 3 (RGB) or 4 (RGBA). The default
    zlib level 1 is chosen for speed: the PNGs only travel to the backend, and on
    a 2048 x 2048 photo crop level 1 encodes about twice as fast as level 6 for a
    file a few percent larger (docs/performance.md).
    """
    if channels not in COLOR_TYPES:
        raise PngError(f"unsupported channel count: {channels}")
    if width <= 0 or height <= 0:
        raise PngError(f"invalid image size: {width}x{height}")
    stride = width * channels
    expected = stride * height
    if len(pixels) != expected:
        raise PngError(f"pixel buffer has {len(pixels)} bytes, expected {expected}")
    raw = bytearray()
    view = memoryview(pixels)
    for y in range(height):
        raw.append(0)  # filter type 0 (None) for every scanline
        raw += view[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, COLOR_TYPES[channels], 0, 0, 0)
    return b"".join(
        (
            _SIGNATURE,
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", zlib.compress(bytes(raw), compress_level)),
            _chunk(b"IEND", b""),
        )
    )


def read_header(data: bytes) -> tuple[int, int, int, int]:
    """Return ``(width, height, bit_depth, color_type)`` from PNG bytes."""
    if len(data) < 33 or not data.startswith(_SIGNATURE):
        raise PngError("not a PNG file")
    length = struct.unpack(">I", data[8:12])[0]
    if data[12:16] != b"IHDR" or length != 13:
        raise PngError("PNG does not start with an IHDR chunk")
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, color_type
