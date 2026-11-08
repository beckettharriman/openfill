# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""A small Photoshop (PSD) writer: RGB, 8 bits, layers with masks, names, visibility and
one level of groups. Nothing else.

OpenFill brings its results into PhotoCraft as one PSD that already holds every layer,
mask and group, so PhotoCraft opens one scratch document per job and OpenFill never has
to work inside it (see ``bridge``). Channel data is written uncompressed: the file only
lives for a moment in the exchange folder.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO

# Section divider kinds (the "lsct" block).
_OPEN_FOLDER, _BOUNDING_DIVIDER = 1, 3


@dataclass
class PsdLayer:
    """One pixel layer, or a group when ``children`` is not None (then ``rgba`` is unused)."""

    name: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    rgba: bytes = b""  # width * height * 4
    visible: bool = True
    mask: tuple[int, int, int, int, bytes] | None = None  # (x, y, width, height, 8-bit pixels)
    children: list[PsdLayer] | None = None  # top first, like the Layers panel


@dataclass
class _Record:
    name: str
    rect: tuple[int, int, int, int]  # top, left, bottom, right
    # (channel id, plane length, the plane): planes are cut from the layer only while written
    channels: list[tuple[int, int, Callable[[], bytes]]] = field(default_factory=list)
    visible: bool = True
    mask: tuple[int, int, int, int] | None = None  # top, left, bottom, right
    divider: int = 0


def _pascal(name: str) -> bytes:
    raw = name.encode("latin-1", "replace")[:255]
    data = bytes([len(raw)]) + raw
    return data + b"\0" * (-len(data) % 4)


def _unicode(name: str) -> bytes:
    text = name.encode("utf-16-be")
    body = struct.pack(">I", len(text) // 2) + text
    body += b"\0" * (-len(body) % 4)
    return b"8BIMluni" + struct.pack(">I", len(body)) + body


def _planes(layer: PsdLayer) -> list[tuple[int, int, Callable[[], bytes]]]:
    rgba = layer.rgba
    n = layer.width * layer.height
    if len(rgba) != n * 4:
        raise ValueError(f"layer '{layer.name}': {len(rgba)} bytes for {layer.width} x {layer.height}")
    return [(cid, n, lambda k=k: rgba[k::4]) for cid, k in ((-1, 3), (0, 0), (1, 1), (2, 2))]


def _empty(cid: int) -> tuple[int, int, Callable[[], bytes]]:
    return cid, 0, bytes


def _records(layers: list[PsdLayer]) -> list[_Record]:
    """The records for ``layers`` (top first) in file order: bottom first, a group's closing
    divider before its children and the group itself after them."""
    out: list[_Record] = []
    for layer in reversed(layers):
        if layer.children is not None:
            out.append(
                _Record("</Layer group>", (0, 0, 0, 0), [_empty(c) for c in (-1, 0, 1, 2)], divider=_BOUNDING_DIVIDER)
            )
            out.extend(_records(layer.children))
            out.append(
                _Record(
                    layer.name, (0, 0, 0, 0), [_empty(c) for c in (-1, 0, 1, 2)], layer.visible, divider=_OPEN_FOLDER
                )
            )
            continue
        record = _Record(layer.name, (layer.y, layer.x, layer.y + layer.height, layer.x + layer.width))
        record.channels, record.visible = _planes(layer), layer.visible
        if layer.mask is not None:
            mx, my, mw, mh, pixels = layer.mask
            if len(pixels) != mw * mh:
                raise ValueError(f"layer '{layer.name}': mask of {len(pixels)} bytes for {mw} x {mh}")
            record.mask = (my, mx, my + mh, mx + mw)
            record.channels.append((-2, len(pixels), lambda pixels=pixels: pixels))
        out.append(record)
    return out


def _record_bytes(record: _Record) -> bytes:
    out = bytearray(struct.pack(">4i", *record.rect))
    out += struct.pack(">H", len(record.channels))
    for channel, length, _plane in record.channels:
        out += struct.pack(">hI", channel, length + 2)  # + the compression word
    flags = 0x08 | (0 if record.visible else 0x02)
    if record.divider:
        flags |= 0x10  # no pixel data of its own
    out += b"8BIMnorm" + bytes([255, 0, flags, 0])
    extra = bytearray()
    if record.mask is not None:
        extra += struct.pack(">I4iBB2x", 20, *record.mask, 0, 0)
    else:
        extra += struct.pack(">I", 0)
    extra += struct.pack(">I", 0)  # blending ranges
    extra += _pascal(record.name)
    extra += _unicode(record.name)
    if record.divider:
        extra += b"8BIMlsct" + struct.pack(">II", 4, record.divider)
    out += struct.pack(">I", len(extra)) + extra
    return bytes(out)


def write(f: BinaryIO, width: int, height: int, layers: list[PsdLayer]) -> None:
    """Write a PSD of ``width`` x ``height`` holding ``layers`` (top first) to ``f``, one channel
    at a time (a big canvas is never held twice in memory)."""
    if not layers:
        raise ValueError("a PSD needs a layer")
    records = _records(layers)
    head = bytearray(struct.pack(">h", -len(records)))  # negative: the merged image has alpha
    for record in records:
        head += _record_bytes(record)
    data = sum(2 + length for record in records for _c, length, _p in record.channels)
    info_len = len(head) + data
    pad = info_len % 2
    f.write(b"8BPS" + struct.pack(">H6xHIIHH", 1, 4, height, width, 8, 3))
    f.write(struct.pack(">II", 0, 0))  # colour mode data, image resources
    f.write(struct.pack(">II", 4 + info_len + pad + 4, info_len + pad))  # the section, its layer info
    f.write(head)
    for record in records:
        for _channel, length, plane in record.channels:
            f.write(b"\0\0")
            if length:
                f.write(plane())
    f.write(b"\0" * pad)
    f.write(struct.pack(">I", 0))  # no global mask info
    f.write(_blank_merged(width, height, 4))  # PhotoCraft reads the layers


def encode(width: int, height: int, layers: list[PsdLayer]) -> bytes:
    """The PSD ``write`` writes, as bytes."""
    out = io.BytesIO()
    write(out, width, height, layers)
    return out.getvalue()


def _blank_merged(width: int, height: int, channels: int) -> bytes:
    """The merged image, all zeros, run-length encoded (PackBits): a few bytes a row."""
    row = bytearray()
    left = width
    while left > 0:
        n = min(left, 128)
        row += bytes([0, 0]) if n == 1 else bytes([257 - n, 0])
        left -= n
    rows = channels * height
    return struct.pack(">H", 1) + struct.pack(">H", len(row)) * rows + bytes(row) * rows
