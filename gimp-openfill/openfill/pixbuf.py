# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Reading a cloud service's JPEG or WebP answer with GdkPixbuf (part of GIMP).

Kept apart from ``gimp_io`` (which needs GIMP) so it can be tested with any GdkPixbuf.
Only the loader for the one format the first bytes name is used, and the picture's size
is checked when the loader reports it, before it allocates the pixels.
"""

from __future__ import annotations

from .imaging import MAX_RESULT_PIXELS, picture_size, sniff


class PixbufError(RuntimeError):
    """The picture is refused or cannot be read; the text says why."""


def decode(data: bytes, max_pixels: int | None = None) -> tuple[int, int, bytes]:
    """``data`` (JPEG or WebP) as (width, height, RGBA bytes)."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    limit = MAX_RESULT_PIXELS if max_pixels is None else max_pixels
    kind = sniff(data)
    if kind not in ("jpeg", "webp"):
        raise PixbufError("not a JPEG or WebP picture")
    width, height = picture_size(data, kind)  # OpenFill's own reading of the header, first
    if width * height > limit:
        raise PixbufError(f"{width} x {height} pixels is larger than OpenFill accepts")
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type(kind)
    except GLib.Error as err:
        raise PixbufError(f"this GIMP cannot read {kind.upper()} pictures ({err.message})") from None
    refused: list[tuple[int, int]] = []

    def size_prepared(the_loader, w: int, h: int) -> None:
        # The loader's own size, reported before it allocates the pixels: when it is too big,
        # it is told to decode to 1 x 1 instead, and the picture is refused below.
        if w * h > limit or (w, h) != (width, height):
            refused.append((w, h))
            the_loader.set_size(1, 1)

    loader.connect("size-prepared", size_prepared)
    try:
        loader.write(data)
        loader.close()
    except GLib.Error as err:
        if refused:
            raise PixbufError(f"{refused[0][0]} x {refused[0][1]} pixels is not what the picture says") from None
        raise PixbufError(f"the picture could not be read ({err.message})") from None
    if refused:
        w, h = refused[0]
        raise PixbufError(f"the picture is {w} x {h} pixels, not what its header says, or larger than OpenFill accepts")
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise PixbufError("the picture could not be read")
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    w, h, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
    if (w, h) != (width, height) or w * h > limit:  # checked again after decoding
        raise PixbufError("the picture's size does not match its header")
    raw = pixbuf.read_pixel_bytes()
    pixels = raw.get_data() if isinstance(raw, GLib.Bytes) else bytes(raw)
    return w, h, b"".join(pixels[y * stride : y * stride + w * 4] for y in range(h))
