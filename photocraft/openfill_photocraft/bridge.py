# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""PhotoCraft's side of OpenFill: the functions ``openfill.app.App`` calls on ``App.io``.

It mirrors ``openfill.gimp_io`` over PhotoCraft's control channel. Pixels travel as
PNG files through the folder PhotoCraft was given as its automation root:

- out: the visible image is duplicated as one merged layer and saved; the selection
  is saved as a channel, duplicated into its own document and saved.
- in: a result is written into a transparent PNG the size of the canvas at its
  exact position, opened, copied and pasted in place, so it lands pixel-exact; its
  mask is the saved selection (grown and feathered) or an explicit mask, loaded as a
  selection and turned into a layer mask.
"""

from __future__ import annotations

import io as _io
import itertools
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFilter

from openfill import png
from openfill.geometry import Rect, flatten_over_mean

from .control import ControlClient, ControlError

SELECTION_CHANNEL = "OpenFill selection"


class BridgeError(RuntimeError):
    """PhotoCraft did not do what OpenFill asked."""


@dataclass
class PCLayer:
    """A layer OpenFill added: its id in PhotoCraft and the pixels it was given."""

    id: int
    name: str
    origin: Rect
    rgba: bytes  # origin-sized RGBA, as inserted
    group: int | None = None
    # How its mask was made, to rebuild it when the pixels are replaced (set_layer_rgba):
    # "" none, "selection" (the saved selection grown by ``grow``), "pixels" (``mask``);
    # both feathered by ``feather``.
    mask_kind: str = ""
    grow: int = 0
    feather: float = 0.0
    mask: bytes | None = None

    def get_name(self) -> str:
        return self.name

    def get_width(self) -> int:
        return self.origin.width

    def get_height(self) -> int:
        return self.origin.height


@dataclass
class PCImage:
    """The PhotoCraft document a job works on, found again by name before each step."""

    bridge: PhotoCraftBridge
    name: str
    saved_selection: bool = False
    layers: list[PCLayer] = field(default_factory=list)

    def _inspect(self) -> dict:
        self.bridge.activate(self)
        return self.bridge.client.run("document.inspect")

    def get_width(self) -> int:
        return int(self._inspect()["width"])

    def get_height(self) -> int:
        return int(self._inspect()["height"])

    def undo_group_start(self) -> None:
        """PhotoCraft records each command as its own history step."""

    def undo_group_end(self) -> None:
        pass


class _Progress:
    """Stand-in for GIMP's status-bar progress (``App.check_backend`` uses it)."""

    def __init__(self, title: str, sink: Callable[[float, str], None] | None = None) -> None:
        self.title, self.sink = title, sink

    def restart(self) -> None:
        pass

    def __call__(self, fraction: float, text: str) -> None:
        if self.sink:
            self.sink(fraction, text)

    def done(self) -> None:
        pass


class PhotoCraftBridge:
    """``App.io`` for PhotoCraft (same function names as ``openfill.gimp_io``)."""

    def __init__(
        self,
        client: ControlClient,
        exchange_dir: Path,
        profile: Path,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.exchange = Path(exchange_dir)  # PhotoCraft's automation read and write root
        self.profile = Path(profile)
        self.notify = notify or (lambda _text: None)
        self._names = itertools.count(1)
        self.exchange.mkdir(parents=True, exist_ok=True)

    # session ------------------------------------------------------------------

    def documents(self) -> list[dict]:
        return list((self.client.call("ui.inspect").get("session") or {}).get("documents") or [])

    def active_image(self) -> PCImage | None:
        session = self.client.call("ui.inspect").get("session") or {}
        docs = session.get("documents") or []
        active = session.get("active")
        for d in docs:
            if d.get("index") == active:
                return PCImage(self, str(d.get("name")))
        return None

    def activate(self, image: PCImage) -> None:
        session = self.client.call("ui.inspect").get("session") or {}
        for d in session.get("documents") or []:
            if d.get("name") == image.name:
                if d.get("index") != session.get("active"):
                    self.client.run("document.activate", document=d["index"])
                return
        raise BridgeError(f"the document '{image.name}' is no longer open in PhotoCraft")

    def _file(self, stem: str) -> str:
        """A fresh name in the exchange folder (relative, as PhotoCraft wants it)."""
        return f"openfill-{os.getpid()}-{next(self._names)}-{stem}.png"

    def _read(self, rel: str) -> Image.Image:
        path = self.exchange / rel
        for _ in range(50):  # the save returns when written; allow a slow disk a moment
            if path.is_file():
                break
            time.sleep(0.05)
        with Image.open(path) as im:
            im.load()
            out = im.copy()
        try:
            path.unlink()
        except OSError:
            pass
        return out

    def _write(self, image: Image.Image, stem: str) -> str:
        rel = self._file(stem)
        image.save(self.exchange / rel, compress_level=1)
        return rel

    def _forget(self, rel: str) -> None:
        try:
            (self.exchange / rel).unlink()
        except OSError:
            pass

    # the functions App calls ----------------------------------------------------

    def profile_dir(self) -> str:
        return str(self.profile)

    def message(self, text: str) -> None:
        self.notify(text)

    def GimpProgress(self, title: str) -> _Progress:  # noqa: N802 - the name App calls
        return _Progress(title)

    def is_indexed(self, image: PCImage) -> bool:
        return False  # PhotoCraft documents are RGB, Gray, CMYK or Lab; the export is RGB(A)

    def image_rect(self, image: PCImage) -> Rect:
        d = image._inspect()
        return Rect(0, 0, int(d["width"]), int(d["height"]))

    def selection_rect(self, image: PCImage) -> Rect | None:
        d = image._inspect()
        bounds = d.get("selectionBounds") if d.get("hasSelection") else None
        if not bounds:
            return None
        x, y, w, h = (int(v) for v in bounds)
        return Rect(x, y, w, h) if w > 0 and h > 0 else None

    def _visible(self, image: PCImage) -> Image.Image:
        """The visible image (all layers merged) as RGBA, the size of the canvas."""
        self.activate(image)
        rel = self._file("visible")
        self.client.run("image.duplicate", name="OpenFill export", mergedOnly=True)
        try:
            self.client.call("app.save", path=rel)
        finally:
            self.client.run("file.close")
            self.activate(image)
        return self._read(rel).convert("RGBA")

    def export_region(self, image: PCImage, rect: Rect, transparent_outside: bool = False) -> tuple[bytes, bytes]:
        """(opaque RGBA PNG, raw RGBA) of ``rect``; outside the canvas is transparent before
        flattening over the mean colour, as GIMP does for an expand."""
        full = self._visible(image)
        crop = full.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        flat = flatten_over_mean(crop.tobytes())
        return png.encode(rect.width, rect.height, flat, 4), flat

    def export_region_png(self, image: PCImage, rect: Rect, transparent_outside: bool = False) -> bytes:
        return self.export_region(image, rect, transparent_outside)[0]

    def export_selection_mask(self, image: PCImage, rect: Rect) -> tuple[bytes, bytes]:
        """(8-bit PNG, raw bytes) of the selection inside ``rect``; 255 = selected. The selection
        is also kept as a channel so the result's mask uses exactly it, even if the user changes
        the selection while the job runs."""
        self.activate(image)
        self._drop_selection_channel()
        self.client.run("select.saveSelection", name=SELECTION_CHANNEL)
        image.saved_selection = True
        rel = self._file("mask")
        self.client.run("channel.duplicate", channel=SELECTION_CHANNEL, document="new", name="OpenFill mask")
        try:
            self.client.call("app.save", path=rel)
        finally:
            self.client.run("file.close")
            self.activate(image)
        mask = self._read(rel).convert("L")
        crop = mask.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        raw = crop.tobytes()
        return png.encode(rect.width, rect.height, raw, 1), raw

    def export_selection_mask_png(self, image: PCImage, rect: Rect) -> bytes:
        return self.export_selection_mask(image, rect)[0]

    def mask_png_from_bytes(self, rect: Rect, pixels: bytes) -> bytes:
        return png.encode(rect.width, rect.height, pixels, 1)

    def _drop_selection_channel(self) -> None:
        try:
            names = [
                c.get("name") for c in (self.client.run("document.inspect").get("channels") or {}).get("alpha") or []
            ]
            while SELECTION_CHANNEL in names:
                self.client.run("channel.delete", channel=SELECTION_CHANNEL)
                names.remove(SELECTION_CHANNEL)
        except ControlError:
            pass

    # writing ------------------------------------------------------------------------

    def _paste(self, image: PCImage, rgba: Image.Image, origin: Rect, above: int | None = None) -> int:
        """Paste ``rgba`` at ``origin`` as a new layer (above layer ``above`` when given); its id."""
        canvas = image._inspect()
        full = Image.new("RGBA", (int(canvas["width"]), int(canvas["height"])), (0, 0, 0, 0))
        full.paste(rgba, (origin.x, origin.y))
        rel = self._write(full, "paste")
        try:
            target = above if above is not None else self._top_layer(image)
            if target is not None:
                self.client.run("layer.select", layer=target)
            self.client.call("app.open", path=rel)
            self.client.run("select.all")
            self.client.run("edit.copy")
            self.client.run("file.close")
            self.activate(image)
            if target is not None:
                self.client.run("layer.select", layer=target)
            result = self.client.run("edit.pasteSpecial.pasteInPlace") or {}
        finally:
            self._forget(rel)
        layer = result.get("layer")
        if layer is None:
            raise BridgeError("PhotoCraft did not report the pasted layer")
        return int(layer)

    def _top_layer(self, image: PCImage) -> int | None:
        """The topmost layer outside any group, so a paste lands on top of the stack."""
        for row in self._flat_layers(image):
            if not row.get("depth"):
                return int(row["id"]) if row.get("id") is not None else None
        return None

    def _select_mask(self, image: PCImage, origin: Rect, mask: bytes) -> None:
        """Make an explicit 8-bit mask (``origin``-sized) the selection: paste it as the
        transparency of a temporary layer and load that."""
        alpha = Image.frombytes("L", (origin.width, origin.height), mask)
        rgba = Image.new("RGBA", alpha.size, (255, 255, 255, 0))
        rgba.putalpha(alpha)
        temp = self._paste(image, rgba, origin)
        try:
            self.client.run("select.loadSelection", channel="transparency", layer=temp)
        finally:
            self.client.run("layer.delete", layer=temp)

    def _mask_layer(
        self, image: PCImage, layer: int, origin: Rect, feather: float, grow: int, mask_pixels: bytes | None
    ) -> None:
        if mask_pixels is not None:
            # Feathered here, not by PhotoCraft: a Gaussian blur that clamps at the canvas edges
            # keeps the generated area fully opaque (the canvas PhotoCraft extended for an expand
            # is filled with the background colour, which a soft edge would let through).
            if feather > 0:
                soft = Image.frombytes("L", (origin.width, origin.height), mask_pixels).filter(
                    ImageFilter.GaussianBlur(float(feather) / 2)
                )
                mask_pixels = soft.tobytes()
            self._select_mask(image, origin, mask_pixels)
        else:
            if image.saved_selection:
                self.client.run("select.loadSelection", channel=SELECTION_CHANNEL)
                if grow > 0:
                    self.client.run("select.modify.expand", radius=int(grow), applyAtCanvasBounds=False)
            else:
                self.client.run("select.all")
            if feather > 0:
                self.client.run("select.modify.feather", radius=float(feather), applyAtCanvasBounds=False)
        self.client.run("layer.select", layer=layer)
        self.client.run("layer.layerMask.revealSelection", layer=layer)

    def insert_results(
        self,
        image: PCImage,
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
    ) -> list[PCLayer]:
        """Generated PNGs as layers at ``origin`` with a mask (see ``gimp_io.insert_results``).
        Several variations go into a group; only the first is visible. The user's selection is
        put back afterwards."""
        self.activate(image)
        group = None
        if group_variations and len(images) > 1:
            group = (self.client.run("layer.new.group", name=base_name) or {}).get("layer")
            if group is None:
                group = self.client.run("document.inspect").get("activeLayer")
        layers: list[PCLayer] = []
        for index, data in enumerate(images):
            name = base_name if len(images) == 1 else f"{base_name} ({index + 1})"
            with Image.open(_io.BytesIO(data)) as im:
                rgba = im.convert("RGBA")
            if scale_to_origin and rgba.size != (origin.width, origin.height):
                rgba = rgba.resize((origin.width, origin.height), Image.LANCZOS)
            layer_id = self._paste(image, rgba, origin)
            self.client.run("layer.setProps", layer=layer_id, name=name, visible=index == 0)
            if use_selection_mask:
                self._mask_layer(image, layer_id, origin, feather, grow, mask_pixels)
            if group is not None:
                self.client.run("layer.moveTo", layer=layer_id, target=int(group), position="into")
            kind = "" if not use_selection_mask else ("pixels" if mask_pixels is not None else "selection")
            layers.append(PCLayer(layer_id, name, origin, rgba.tobytes(), group, kind, grow, feather, mask_pixels))
        self._restore_selection(image)
        if layers:
            self.client.run("layer.select", layer=layers[0].id)
        image.layers.extend(layers)
        return layers

    def _restore_selection(self, image: PCImage) -> None:
        try:
            if image.saved_selection:
                self.client.run("select.loadSelection", channel=SELECTION_CHANNEL)
            else:
                self.client.run("select.deselect")
        except ControlError:
            pass

    def layer_rgba(self, layer: PCLayer) -> bytes:
        return layer.rgba

    def set_layer_rgba(self, layer: PCLayer, data: bytes) -> None:
        """Replace a layer's pixels: paste the new ones above it, give them its mask, name,
        visibility and group, then delete the old layer."""
        image = self._image_of(layer)
        rgba = Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), data)
        info = next((row for row in self._flat_layers(image) if row.get("id") == layer.id), {})
        new_id = self._paste(image, rgba, layer.origin, above=layer.id)
        self.client.run("layer.setProps", layer=new_id, name=layer.name, visible=bool(info.get("visible", True)))
        if layer.mask_kind:
            self._mask_layer(
                image,
                new_id,
                layer.origin,
                layer.feather,
                layer.grow,
                layer.mask if layer.mask_kind == "pixels" else None,
            )
        self.client.run("layer.delete", layer=layer.id)
        self._restore_selection(image)
        self.client.run("layer.select", layer=new_id)
        layer.id, layer.rgba = new_id, data

    def _image_of(self, layer: PCLayer) -> PCImage:
        for image in self._images:
            if layer in image.layers:
                return image
        raise BridgeError("layer of an unknown document")

    @property
    def _images(self) -> list[PCImage]:
        return getattr(self, "_known_images", [])

    def track(self, image: PCImage) -> PCImage:
        """Remember ``image`` so its layers can be found again (``set_layer_rgba``)."""
        known = getattr(self, "_known_images", [])
        if image not in known:
            known.append(image)
        self._known_images = known
        return image

    def _flat_layers(self, image: PCImage) -> list[dict]:
        self.activate(image)
        reply = self.client.run("layer.list", flat=True) or {}
        return list(reply.get("layers") or [] if isinstance(reply, dict) else reply)

    def reorder_variations(
        self, image: PCImage, layers: list[PCLayer], order: list[int], base_name: str
    ) -> list[PCLayer]:
        """Best first: the first in ``order`` becomes the top, visible, selected layer."""
        if len(layers) < 2 or order == list(range(len(layers))):
            return layers
        ordered = [layers[i] for i in order]
        self.activate(image)
        for layer in reversed(ordered):
            if layer.group is not None:
                self.client.run("layer.moveTo", layer=layer.id, target=int(layer.group), position="into")
            else:
                self.client.run("layer.arrange.bringToFront", layer=layer.id)
        for position, layer in enumerate(ordered):
            layer.name = f"{base_name} ({position + 1})"
            self.client.run("layer.setProps", layer=layer.id, name=layer.name, visible=position == 0)
        self.client.run("layer.select", layer=ordered[0].id)
        return ordered

    def show_variation(self, image: PCImage, layers: list[PCLayer], index: int) -> None:
        """The variations picker: only ``layers[index]`` visible."""
        self.activate(image)
        for i, layer in enumerate(layers):
            self.client.run("layer.setProps", layer=layer.id, visible=i == index)
        self.client.run("layer.select", layer=layers[index].id)

    def resize_canvas(self, image: PCImage, new_w: int, new_h: int, off_x: int, off_y: int) -> None:
        """Grow or crop the canvas; ``off_x``/``off_y`` is where the old (0, 0) lands."""
        self.activate(image)
        self.client.run("image.crop", x=-int(off_x), y=-int(off_y), width=int(new_w), height=int(new_h))

    def clip_layers_to_canvas(self, image: PCImage) -> None:
        """PhotoCraft's crop already cuts the layers (deleteCroppedPixels)."""

    def new_image_from_pngs(self, images: list[bytes], base_name: str, temp_dir: Path | None) -> PCImage:
        """A new document from the first image; further variations become hidden layers."""
        with Image.open(_io.BytesIO(images[0])) as im:
            first = im.convert("RGBA")
        # The document takes the file's name: make it the prompt's, not a temporary one.
        stem = re.sub(r"[^A-Za-z0-9 ,._-]+", "", base_name).strip()[:60] or "Text to Image"
        rel = f"{stem}.png"
        if (self.exchange / rel).exists():
            rel = f"{stem} {next(self._names)}.png"
        first.save(self.exchange / rel, compress_level=1)
        try:
            opened = self.client.call("app.open", path=rel) or {}
        finally:
            self._forget(rel)
        image = self.track(PCImage(self, str(opened.get("name") or rel)))
        try:
            base = self.client.run("document.inspect").get("activeLayer")
            if base is not None:
                self.client.run(
                    "layer.setProps", layer=base, name=base_name if len(images) == 1 else f"{base_name} (1)"
                )
        except ControlError:
            pass
        for index, data in enumerate(images[1:], start=2):
            with Image.open(_io.BytesIO(data)) as im:
                rgba = im.convert("RGBA")
            layer = self._paste(image, rgba, Rect(0, 0, rgba.width, rgba.height))
            self.client.run("layer.setProps", layer=layer, name=f"{base_name} ({index})", visible=False)
        return image

    def finish(self, image: PCImage) -> None:
        """End of a job: drop the saved selection channel (the selection itself stays)."""
        try:
            self.activate(image)
            self._drop_selection_channel()
            image.saved_selection = False
        except (ControlError, BridgeError):
            pass


def default_profile() -> Path:
    """Where the companion keeps settings, logs and the exchange folder."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "OpenFill for PhotoCraft"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenFill for PhotoCraft"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openfill-photocraft"
