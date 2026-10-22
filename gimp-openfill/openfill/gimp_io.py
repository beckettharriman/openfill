# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Everything that touches the GIMP API lives here.

Pixel export reads straight from GEGL buffers (no temporary exports); results
come back through ``Gimp.file_load_layer`` so GIMP's own PNG loader handles
color management.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gegl, Gimp, Gio  # noqa: E402

from . import png  # noqa: E402
from .geometry import Rect, flatten_over_mean  # noqa: E402

RGBA = "R'G'B'A u8"
GRAY = "Y u8"


class GimpIoError(RuntimeError):
    pass


# ---------------------------------------------------------------- reading ----


def image_rect(image: Gimp.Image) -> Rect:
    return Rect(0, 0, image.get_width(), image.get_height())


def is_indexed(image: Gimp.Image) -> bool:
    return image.get_base_type() == Gimp.ImageBaseType.INDEXED


def selection_rect(image: Gimp.Image) -> Rect | None:
    """Bounding box of the selection, or None when nothing is selected."""
    # GIMP 3.2 returns (success, non_empty, x1, y1, x2, y2); older builds omit success.
    non_empty, x1, y1, x2, y2 = tuple(Gimp.Selection.bounds(image))[-5:]
    if not non_empty:
        return None
    return Rect(x1, y1, x2 - x1, y2 - y1)


def _read_buffer(buffer: Gegl.Buffer, rect: Rect, fmt: str, abyss: Gegl.AbyssPolicy = Gegl.AbyssPolicy.CLAMP) -> bytes:
    region = Gegl.Rectangle.new(rect.x, rect.y, rect.width, rect.height)
    data = buffer.get(region, 1.0, fmt, abyss)
    return bytes(data)


def _visible_without_channels(image: Gimp.Image) -> Gimp.Layer | None:
    """The visible composite without channel overlays (a tinted user channel,
    the red quick mask), which GIMP's projection would otherwise bake into the
    pixels sent to the model. With visible channels the composite is taken from
    a throw-away duplicate, so the user's image and its undo history are never
    touched (freezing undo around visibility toggles crashed GIMP 3.2.6)."""
    if not any(ch.get_visible() for ch in image.get_channels()):
        return Gimp.Layer.new_from_visible(image, image, "openfill export")
    dup = image.duplicate()
    try:
        dup.undo_disable()
        for channel in dup.get_channels():
            channel.set_visible(False)
        return Gimp.Layer.new_from_visible(dup, image, "openfill export")
    finally:
        dup.delete()


def export_region(image: Gimp.Image, rect: Rect, transparent_outside: bool = False) -> tuple[bytes, bytes]:
    """Composite of all visible layers inside ``rect``: (opaque RGBA PNG, its
    raw RGBA pixels).

    ``rect`` may extend past the image; with ``transparent_outside`` those
    pixels are transparent (used by expand before the canvas grows),
    otherwise the edge pixels are repeated. Visible channels are not part of
    the composite. Transparent pixels are flattened over the mean visible
    colour: models read RGB only and would see them as black.
    """
    layer = _visible_without_channels(image)
    if layer is None:
        raise GimpIoError("GIMP could not render the visible image")
    try:
        abyss = Gegl.AbyssPolicy.NONE if transparent_outside else Gegl.AbyssPolicy.CLAMP
        pixels = _read_buffer(layer.get_buffer(), rect, RGBA, abyss)
    finally:
        layer.delete()
    expected = rect.width * rect.height * 4
    if len(pixels) != expected:
        raise GimpIoError(f"read {len(pixels)} bytes, expected {expected}")
    flat = flatten_over_mean(pixels)
    return png.encode(rect.width, rect.height, flat, 4), flat


def export_region_png(image: Gimp.Image, rect: Rect, transparent_outside: bool = False) -> bytes:
    """The PNG of ``export_region``."""
    return export_region(image, rect, transparent_outside)[0]


def export_selection_mask(image: Gimp.Image, rect: Rect) -> tuple[bytes, bytes]:
    """The selection channel inside ``rect``: (8-bit gray PNG, raw bytes); 255 = selected."""
    selection = image.get_selection()
    pixels = _read_buffer(selection.get_buffer(), rect, GRAY)
    expected = rect.width * rect.height
    if len(pixels) != expected:
        raise GimpIoError(f"read {len(pixels)} mask bytes, expected {expected}")
    return png.encode(rect.width, rect.height, pixels, 1), pixels


def export_selection_mask_png(image: Gimp.Image, rect: Rect) -> bytes:
    """The selection channel inside ``rect`` as an 8-bit gray PNG (255 = selected)."""
    return export_selection_mask(image, rect)[0]


def layer_rgba(layer: Gimp.Layer) -> bytes:
    """The layer's own pixels as RGBA bytes (layer size, mask not applied)."""
    rect = Rect(0, 0, layer.get_width(), layer.get_height())
    return _read_buffer(layer.get_buffer(), rect, RGBA, Gegl.AbyssPolicy.NONE)


def set_layer_rgba(layer: Gimp.Layer, data: bytes) -> None:
    """Replace the layer's pixels with RGBA bytes of the layer's size."""
    rect = Gegl.Rectangle.new(0, 0, layer.get_width(), layer.get_height())
    buffer = layer.get_buffer()
    buffer.set(rect, RGBA, data)
    buffer.flush()
    layer.update(0, 0, layer.get_width(), layer.get_height())


def reorder_variations(
    image: Gimp.Image, layers: list[Gimp.Layer], order: list[int], base_name: str
) -> list[Gimp.Layer]:
    """Put the variation layers of one group in ``order`` (indices into
    ``layers``, best first): the first becomes the top, visible, selected
    layer and the names are renumbered "(1)", "(2)", ... in the new order.
    One undo step; returns the layers in the new order."""
    if len(layers) < 2 or order == list(range(len(layers))):
        return layers
    ordered = [layers[i] for i in order]
    image.undo_group_start()
    try:
        parent = ordered[0].get_parent()
        for position, layer in enumerate(ordered):
            image.reorder_item(layer, parent, position)
            layer.set_name(f"{base_name} ({position + 1})")
            layer.set_visible(position == 0)
        image.set_selected_layers([ordered[0]])
    finally:
        image.undo_group_end()
    Gimp.displays_flush()
    return ordered


def mask_png_from_bytes(rect: Rect, pixels: bytes) -> bytes:
    return png.encode(rect.width, rect.height, pixels, 1)


# ---------------------------------------------------------------- writing ----


def write_temp_png(data: bytes, directory: Path | None = None, prefix: str = "openfill-") -> Path:
    directory = Path(directory) if directory else Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".png", dir=str(directory))
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return Path(name)


def load_layer_from_png(image: Gimp.Image, data: bytes, name: str, temp_dir: Path | None = None) -> Gimp.Layer:
    path = write_temp_png(data, temp_dir)
    try:
        layer = Gimp.file_load_layer(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(str(path)))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if layer is None:
        raise GimpIoError("GIMP could not load the generated image")
    layer.set_name(name)
    if not layer.has_alpha():
        layer.add_alpha()
    return layer


class SavedSelection:
    """Context manager that snapshots the selection and restores it on exit."""

    def __init__(self, image: Gimp.Image) -> None:
        self.image = image
        self.channel: Gimp.Channel | None = None
        self.was_empty = Gimp.Selection.is_empty(image)

    def __enter__(self) -> SavedSelection:
        if not self.was_empty:
            self.channel = Gimp.Selection.save(self.image)
        return self

    def __exit__(self, *exc) -> None:
        if self.channel is not None:
            self.image.select_item(Gimp.ChannelOps.REPLACE, self.channel)
            self.image.remove_channel(self.channel)
        else:
            Gimp.Selection.none(self.image)


def select_from_mask(image: Gimp.Image, rect: Rect, pixels: bytes) -> None:
    """Replace the selection with an 8-bit mask covering ``rect``."""
    channel = Gimp.Channel.new(
        image, "openfill mask", image.get_width(), image.get_height(), 100.0, Gegl.Color.new("black")
    )
    if channel is None:
        raise GimpIoError("GIMP could not create a temporary channel")
    buffer = channel.get_buffer()
    buffer.set(Gegl.Rectangle.new(rect.x, rect.y, rect.width, rect.height), GRAY, pixels)
    buffer.flush()
    image.insert_channel(channel, None, 0)
    try:
        image.select_item(Gimp.ChannelOps.REPLACE, channel)
    finally:
        image.remove_channel(channel)


def add_selection_mask(
    image: Gimp.Image,
    layer: Gimp.Layer,
    selection: Gimp.Channel | None,
    feather: float,
    mask_rect: Rect | None = None,
    mask_pixels: bytes | None = None,
    grow: int = 0,
) -> None:
    """Attach a layer mask (selection grown by ``grow`` then feathered, or an
    explicit mask) to ``layer``. Growing first keeps the regenerated rim of a
    removed object inside the opaque part of the mask."""
    if mask_pixels is not None and mask_rect is not None:
        select_from_mask(image, mask_rect, mask_pixels)
    elif selection is not None:
        image.select_item(Gimp.ChannelOps.REPLACE, selection)
    else:
        Gimp.Selection.all(image)
    if grow > 0 and selection is not None and mask_pixels is None:
        Gimp.Selection.grow(image, int(grow))
    if feather > 0:
        Gimp.Selection.feather(image, feather)
    mask = layer.create_mask(Gimp.AddMaskType.SELECTION)
    layer.add_mask(mask)


def insert_results(
    image: Gimp.Image,
    images: list[bytes],
    origin: Rect,
    base_name: str,
    feather: float,
    temp_dir: Path | None,
    use_selection_mask: bool = True,
    group_variations: bool = True,
    mask_pixels: bytes | None = None,
    grow: int = 0,
    scale_to_origin: bool = False,
) -> list[Gimp.Layer]:
    """Insert generated PNGs as layers at ``origin`` with a feathered mask.

    The mask comes from the user's selection, or from ``mask_pixels`` (an
    8-bit mask the size of ``origin``) when given. Several variations go into
    a layer group; only the first is visible. Runs inside one undo group and
    restores the user's selection. With ``scale_to_origin`` a result whose
    size differs from ``origin`` (generated at the model's working size) is
    scaled to it.
    """
    layers: list[Gimp.Layer] = []
    image.undo_group_start()
    try:
        with SavedSelection(image) as saved:
            parent = None
            if group_variations and len(images) > 1:
                parent = Gimp.GroupLayer.new(image, base_name)
                image.insert_layer(parent, None, 0)
            for index, data in enumerate(images):
                name = base_name if len(images) == 1 else f"{base_name} ({index + 1})"
                layer = load_layer_from_png(image, data, name, temp_dir)
                image.insert_layer(layer, parent, index)
                # scaling needs the layer attached to the image (silently ignored before)
                if scale_to_origin and (layer.get_width(), layer.get_height()) != (origin.width, origin.height):
                    layer.scale(origin.width, origin.height, False)
                layer.set_offsets(origin.x, origin.y)
                if use_selection_mask:
                    add_selection_mask(image, layer, saved.channel, feather, origin, mask_pixels, grow)
                layer.set_visible(index == 0)
                layers.append(layer)
            if layers:
                image.set_selected_layers([layers[0]])
    finally:
        image.undo_group_end()
    Gimp.displays_flush()
    return layers


def resize_canvas(image: Gimp.Image, new_w: int, new_h: int, off_x: int, off_y: int) -> None:
    image.resize(new_w, new_h, off_x, off_y)


def clip_layers_to_canvas(image: Gimp.Image) -> None:
    """Cut every layer to the canvas (after a crop), so nothing outside it is kept."""
    from .geometry import clip_rect

    width, height = image.get_width(), image.get_height()

    def clip(layers: list) -> None:
        for layer in layers:
            if isinstance(layer, Gimp.GroupLayer):
                clip(layer.get_children())
                continue
            offsets = layer.get_offsets()
            x, y = int(offsets[-2]), int(offsets[-1])
            lw, lh = layer.get_width(), layer.get_height()
            rect = clip_rect(x, y, lw, lh, width, height)
            if rect is None or (rect.x, rect.y, rect.width, rect.height) == (x, y, lw, lh):
                continue
            layer.resize(rect.width, rect.height, x - rect.x, y - rect.y)

    image.undo_group_start()
    try:
        clip(image.get_layers())
    finally:
        image.undo_group_end()
    Gimp.displays_flush()


def new_image_from_pngs(images: list[bytes], base_name: str, temp_dir: Path | None) -> Gimp.Image:
    w, h, _, _ = png.read_header(images[0])
    image = Gimp.Image.new(w, h, Gimp.ImageBaseType.RGB)
    image.undo_disable()
    for index, data in enumerate(images):
        name = base_name if len(images) == 1 else f"{base_name} ({index + 1})"
        layer = load_layer_from_png(image, data, name, temp_dir)
        image.insert_layer(layer, None, index)
        layer.set_visible(index == 0)
    image.undo_enable()
    try:
        Gimp.Display.new(image)
    except Exception:  # noqa: BLE001 - no display in batch/console mode; the image still exists
        pass
    Gimp.displays_flush()
    return image


def profile_dir() -> str:
    return Gimp.directory()


def message(text: str) -> None:
    Gimp.message(text)


class GimpProgress:
    """Adapter from the backend progress callback to GIMP's progress bar."""

    def __init__(self, title: str) -> None:
        self.title = title
        Gimp.progress_init(title)

    def restart(self) -> None:
        Gimp.progress_init(self.title)

    def __call__(self, fraction: float, text: str) -> None:
        if text:
            Gimp.progress_set_text(text)
        Gimp.progress_update(max(0.0, min(1.0, float(fraction))))

    def done(self) -> None:
        Gimp.progress_update(1.0)
        Gimp.progress_end()
