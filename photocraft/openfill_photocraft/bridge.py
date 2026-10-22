# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""PhotoCraft's side of OpenFill: the functions ``openfill.app.App`` calls on ``App.io``.

It mirrors ``openfill.gimp_io`` over PhotoCraft's control channel. Pixels travel as
PNG files through the folder PhotoCraft was given as its automation root.

The user's document is only read until a job's result is ready, and then changed in
one step, so one Undo takes a fill back:

- out: the document is duplicated as one merged layer and saved; the selection is
  saved into that duplicate (not into the document) and saved from there.
- in: results are kept here (``PCImage.pending``) while OpenFill still works on them
  (seam correction, grain, ranking). They are then built in scratch documents (a
  canvas-sized PNG per layer, so each lands pixel-exact, its mask and group) and
  copied into the document with one ``layer.copyToDocument``. The clipboard is never
  used.
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


class BridgeError(RuntimeError):
    """PhotoCraft did not do what OpenFill asked."""


@dataclass(eq=False)
class PCLayer:
    """A layer OpenFill made: its pixels, and its id in PhotoCraft once it is in the document."""

    id: int | None
    name: str
    origin: Rect
    rgba: bytes  # origin-sized RGBA
    group: int | None = None
    visible: bool = True
    # How its mask is made: "" none, "selection" (the job's selection grown by ``grow``),
    # "pixels" (``mask``, origin-sized); both feathered by ``feather``.
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


@dataclass(eq=False)
class PCImage:
    """The PhotoCraft document a job works on, found again by its tab position and name."""

    bridge: PhotoCraftBridge
    name: str
    index: int | None = None
    layers: list[PCLayer] = field(default_factory=list)
    pending: list[PCLayer] = field(default_factory=list)  # made, not yet in the document
    pending_group: str = ""  # the group several pending variations go into
    selection: bytes | None = None  # the job's selection, canvas-sized 8-bit
    _snapshot: tuple[int, Image.Image, Image.Image | None] | None = None  # (revision, visible, selection)

    def _inspect(self) -> dict:
        self.bridge.activate(self)
        return self.bridge.client.run("document.inspect")

    def get_width(self) -> int:
        return int(self._inspect()["width"])

    def get_height(self) -> int:
        return int(self._inspect()["height"])

    def undo_group_start(self) -> None:
        """Results reach the document in one step anyway (see the module docstring)."""

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
        read_root: Path | None = None,
    ) -> None:
        self.client = client
        self.exchange = Path(exchange_dir)  # PhotoCraft's automation write root
        # Its read root: the exchange folder, or a folder that holds it (the home folder, so the
        # launcher can open images there too). Paths for app.open are relative to it.
        self.read_root = Path(read_root) if read_root else self.exchange
        self.profile = Path(profile)
        self.notify = notify or (lambda _text: None)
        self._names = itertools.count(1)
        self._known_images: list[PCImage] = []
        self.exchange.mkdir(parents=True, exist_ok=True)

    # session ------------------------------------------------------------------

    def session(self) -> dict:
        return self.client.call("ui.inspect").get("session") or {}

    def documents(self) -> list[dict]:
        return list(self.session().get("documents") or [])

    def active_image(self) -> PCImage | None:
        session = self.session()
        active = session.get("active")
        for d in session.get("documents") or []:
            if d.get("index") == active:
                return PCImage(self, str(d.get("name")), int(d["index"]))
        return None

    def _find(self, image: PCImage, session: dict | None = None) -> dict:
        docs = (session or self.session()).get("documents") or []
        for d in docs:  # where it was, if it is still there (two tabs may share a name)
            if d.get("index") == image.index and d.get("name") == image.name:
                return d
        for d in docs:
            if d.get("name") == image.name:
                image.index = int(d["index"])
                return d
        raise BridgeError(f"the document '{image.name}' is no longer open in PhotoCraft")

    def activate(self, image: PCImage) -> None:
        session = self.session()
        d = self._find(image, session)
        if d.get("index") != session.get("active"):
            self.client.run("document.activate", document=d["index"])

    def _revision(self, image: PCImage) -> int:
        return int(self._find(image).get("revision") or 0)

    def _file(self, stem: str) -> str:
        """A fresh name in the exchange folder (relative, as PhotoCraft wants it)."""
        return f"OpenFill {stem} {os.getpid()}-{next(self._names)}.png"

    def _read(self, rel: str) -> Image.Image:
        path = self.exchange / rel
        for _ in range(50):  # the save returns when written; allow a slow disk a moment
            if path.is_file():
                break
            time.sleep(0.05)
        with Image.open(path) as im:
            im.load()
            out = im.copy()
        self._forget(rel)
        return out

    def _write(self, image: Image.Image, stem: str) -> str:
        rel = self._file(stem)
        image.save(self.exchange / rel, compress_level=1)
        return rel

    def _open_path(self, rel: str) -> str:
        """``rel`` (in the exchange folder) as app.open wants it: relative to the read root."""
        return os.path.relpath(self.exchange / rel, self.read_root)

    def open_file(self, path: str) -> dict:
        """Open an image the user picked (inside the read root) in PhotoCraft."""
        rel = os.path.relpath(Path(path).resolve(), self.read_root.resolve())
        if rel.startswith(".."):
            raise BridgeError(f"PhotoCraft may only open files in {self.read_root} for OpenFill")
        return self.client.call("app.open", path=rel) or {}

    def _forget(self, rel: str) -> None:
        try:
            (self.exchange / rel).unlink()
        except OSError:
            pass

    def _open(self, image: Image.Image, stem: str) -> tuple[int, int]:
        """Open ``image`` as a scratch document; (its index, its one layer's id)."""
        rel = self._write(image, stem)
        try:
            self.client.call("app.open", path=self._open_path(rel))
        finally:
            self._forget(rel)
        index = int(self.session().get("active"))
        layers = (self.client.run("layer.list") or {}).get("layers") or []
        if not layers:
            raise BridgeError("PhotoCraft opened an empty document")
        return index, int(layers[0]["id"])

    def _close(self, index: int) -> None:
        try:
            self.client.run("file.close", document=index)
        except ControlError:
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

    def _snapshot(self, image: PCImage) -> tuple[Image.Image, Image.Image | None]:
        """The visible image (RGBA) and the selection (L, or None), both canvas-sized, read
        through one merged duplicate; the document itself is not changed. Kept until the
        document changes."""
        self.commit(image)
        revision = self._revision(image)
        if image._snapshot and image._snapshot[0] == revision:
            return image._snapshot[1], image._snapshot[2]
        self.activate(image)
        has_selection = bool(self.client.run("document.inspect").get("hasSelection"))
        self.client.run("image.duplicate", name="OpenFill export", mergedOnly=True)
        duplicate = int(self.session().get("active"))
        visible_rel, mask_rel = self._file("visible"), self._file("mask")
        mask_doc = None
        try:
            self.client.call("app.save", path=visible_rel)
            if has_selection:
                self.activate(image)
                self.client.run("select.saveSelection", name="OpenFill selection", document=duplicate)
                self.client.run("document.activate", document=duplicate)
                self.client.run("channel.duplicate", channel="OpenFill selection", document="new", name="OpenFill mask")
                mask_doc = int(self.session().get("active"))
                self.client.call("app.save", path=mask_rel)
        finally:
            for index in sorted((i for i in (duplicate, mask_doc) if i is not None), reverse=True):
                self._close(index)
            self.activate(image)
        visible = self._read(visible_rel).convert("RGBA")
        selection = self._read(mask_rel).convert("L") if mask_doc is not None else None
        image._snapshot = (revision, visible, selection)
        return visible, selection

    def export_region(self, image: PCImage, rect: Rect, transparent_outside: bool = False) -> tuple[bytes, bytes]:
        """(opaque RGBA PNG, raw RGBA) of ``rect``; outside the canvas is transparent before
        flattening over the mean colour, as GIMP does for an expand."""
        full, _selection = self._snapshot(image)
        crop = full.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        flat = flatten_over_mean(crop.tobytes())
        return png.encode(rect.width, rect.height, flat, 4), flat

    def export_region_png(self, image: PCImage, rect: Rect, transparent_outside: bool = False) -> bytes:
        return self.export_region(image, rect, transparent_outside)[0]

    def export_selection_mask(self, image: PCImage, rect: Rect) -> tuple[bytes, bytes]:
        """(8-bit PNG, raw bytes) of the selection inside ``rect``; 255 = selected. The whole
        selection is kept for the result's mask, so it is exactly this one even if the user
        changes the selection while the job runs."""
        full, selection = self._snapshot(image)
        if selection is None:
            selection = Image.new("L", full.size, 255)
        image.selection = selection.tobytes()
        crop = selection.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        raw = crop.tobytes()
        return png.encode(rect.width, rect.height, raw, 1), raw

    def export_selection_mask_png(self, image: PCImage, rect: Rect) -> bytes:
        return self.export_selection_mask(image, rect)[0]

    def mask_png_from_bytes(self, rect: Rect, pixels: bytes) -> bytes:
        return png.encode(rect.width, rect.height, pixels, 1)

    # writing ------------------------------------------------------------------------

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
        Several variations go into a group; only the first is visible. They reach the document
        at the end of the job (``commit``)."""
        self.commit(image)  # an earlier round's results come first
        layers: list[PCLayer] = []
        for index, data in enumerate(images):
            name = base_name if len(images) == 1 else f"{base_name} ({index + 1})"
            with Image.open(_io.BytesIO(data)) as im:
                rgba = im.convert("RGBA")
            if scale_to_origin and rgba.size != (origin.width, origin.height):
                rgba = rgba.resize((origin.width, origin.height), Image.LANCZOS)
            kind = "" if not use_selection_mask else ("pixels" if mask_pixels is not None else "selection")
            layers.append(
                PCLayer(None, name, origin, rgba.tobytes(), None, index == 0, kind, grow, feather, mask_pixels)
            )
        image.pending = layers
        image.pending_group = base_name if group_variations and len(images) > 1 else ""
        image.layers.extend(layers)
        return layers

    def layer_rgba(self, layer: PCLayer) -> bytes:
        return layer.rgba

    def set_layer_rgba(self, layer: PCLayer, data: bytes) -> None:
        """Replace a layer's pixels: here while it is pending; in the document, a copy with the
        new pixels, its mask, name and visibility goes above it and the old one is deleted."""
        layer.rgba = data
        if layer.id is None:
            return
        image = self._image_of(layer)
        scratch, _ids, top = self._build(image, [layer], "")
        try:
            self.activate(image)
            self.client.run("layer.select", layer=layer.id)
            copied = self._copy_in(image, scratch, top)
        finally:
            self._close(scratch)
            self.activate(image)
        self.client.run("layer.delete", layer=layer.id)
        layer.id = copied
        self.client.run("layer.select", layer=copied)

    def _mask_image(self, image: PCImage, layer: PCLayer, size: tuple[int, int]) -> Image.Image | None:
        """The canvas-sized 8-bit mask ``layer`` gets before PhotoCraft grows or feathers it."""
        if layer.mask_kind == "pixels" and layer.mask is not None:
            mask = Image.frombytes("L", (layer.origin.width, layer.origin.height), layer.mask)
            if layer.feather > 0:
                # Feathered here, not by PhotoCraft: a Gaussian blur that clamps at the canvas
                # edges keeps the generated area fully opaque (the canvas an expand extended is
                # filled with the background colour, which a soft edge would let through).
                mask = mask.filter(ImageFilter.GaussianBlur(float(layer.feather) / 2))
            full = Image.new("L", size, 0)
            full.paste(mask, (layer.origin.x, layer.origin.y))
            return full
        if layer.mask_kind == "selection":
            if image.selection is not None and len(image.selection) == size[0] * size[1]:
                return Image.frombytes("L", size, image.selection)
            return Image.new("L", size, 255)
        return None

    def _build(self, image: PCImage, layers: list[PCLayer], group_name: str) -> tuple[int, list[int], int]:
        """Build ``layers`` (top first) with masks, names and visibility in a scratch document
        the size of the canvas; (its index, the layer ids there, the id to copy)."""
        d = image._inspect()
        size = (int(d["width"]), int(d["height"]))
        scratch, ids = None, []
        try:
            for layer in reversed(layers):  # each copy lands above the last, so bottom first
                full = Image.new("RGBA", size, (0, 0, 0, 0))
                full.paste(
                    Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), layer.rgba),
                    (layer.origin.x, layer.origin.y),
                )
                index, layer_id = self._open(full, "layer")
                if scratch is None:
                    scratch = index
                    ids.insert(0, layer_id)
                else:
                    copied = self.client.run("layer.copyToDocument", document=scratch, source=index, layers=[layer_id])
                    self._close(index)
                    ids.insert(0, int((copied or {}).get("layers", [0])[0]))
            assert scratch is not None
            self.client.run("document.activate", document=scratch)
            masks: dict[tuple, list[int]] = {}
            for layer, layer_id in zip(layers, ids, strict=True):
                key = (layer.mask_kind, layer.grow, layer.feather, id(layer.mask))
                if layer.mask_kind:
                    masks.setdefault(key, []).append(layer_id)
            for (kind, grow, feather, _), targets in masks.items():
                sample = next(layer for layer, i in zip(layers, ids, strict=True) if i == targets[0])
                mask = self._mask_image(image, sample, size)
                if mask is None:
                    continue
                rgba = Image.new("RGBA", size, (255, 255, 255, 0))
                rgba.putalpha(mask)
                mask_doc, mask_layer = self._open(rgba, "mask")
                try:
                    self.client.run("document.activate", document=scratch)
                    for layer_id in targets:  # adding a mask uses the selection up, so load it for each
                        self.client.run(
                            "select.loadSelection", channel="transparency", layer=mask_layer, document=mask_doc
                        )
                        if kind == "selection":
                            if grow > 0:
                                self.client.run("select.modify.expand", radius=int(grow), applyAtCanvasBounds=False)
                            if feather > 0:
                                self.client.run(
                                    "select.modify.feather", radius=float(feather), applyAtCanvasBounds=False
                                )
                        self.client.run("layer.layerMask.revealSelection", layer=layer_id)
                finally:
                    self._close(mask_doc)
                    self.client.run("document.activate", document=scratch)
            for layer, layer_id in zip(layers, ids, strict=True):
                self.client.run("layer.setProps", layer=layer_id, name=layer.name, visible=layer.visible)
            top = ids[0]
            if group_name:
                top = int((self.client.run("layer.new.group", name=group_name) or {}).get("layer"))
                for layer_id in reversed(ids):  # "into" puts it at the top of the group
                    self.client.run("layer.moveTo", layer=layer_id, target=top, position="into")
            return scratch, ids, top
        except BaseException:
            if scratch is not None:
                self._close(scratch)
            raise

    def _copy_in(self, image: PCImage, scratch: int, layer_id: int) -> int:
        """Copy ``layer_id`` from the scratch document into ``image``, above its active layer:
        one step in the document's history. The copy's id."""
        target = self._find(image)
        copied = self.client.run(
            "layer.copyToDocument", document=int(target["index"]), source=scratch, layers=[layer_id]
        )
        ids = (copied or {}).get("layers") or []
        if not ids:
            raise BridgeError("PhotoCraft did not report the copied layer")
        return int(ids[0])

    def commit(self, image: PCImage) -> None:
        """Put the pending results into the document in one step, on top of the stack."""
        layers, group_name = image.pending, image.pending_group
        if not layers:
            return
        image.pending, image.pending_group = [], ""
        scratch, _ids, top = self._build(image, layers, group_name)
        try:
            self.activate(image)
            top_layer = self._top_layer(image)
            if top_layer is not None:
                self.client.run("layer.select", layer=top_layer)  # not a history step
            copied = self._copy_in(image, scratch, top)
        finally:
            self._close(scratch)
            self.activate(image)
        if group_name:
            rows = [row for row in self._flat_layers(image) if row.get("parent") == copied]
            if len(rows) != len(layers):
                raise BridgeError("the copied group does not hold the variations")
            for layer, row in zip(layers, rows, strict=True):
                layer.id, layer.group = int(row["id"]), copied
        else:
            layers[0].id = copied
        self.client.run("layer.select", layer=layers[0].id)
        image._snapshot = None

    def _top_layer(self, image: PCImage) -> int | None:
        """The topmost layer outside any group, so a result lands on top of the stack."""
        for row in self._flat_layers(image):
            if not row.get("depth"):
                return int(row["id"]) if row.get("id") is not None else None
        return None

    def _image_of(self, layer: PCLayer) -> PCImage:
        for image in self._known_images:
            if layer in image.layers:
                return image
        raise BridgeError("layer of an unknown document")

    def track(self, image: PCImage) -> PCImage:
        """Remember ``image`` so its layers can be found again (``set_layer_rgba``)."""
        if image not in self._known_images:
            self._known_images.append(image)
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
        for position, layer in enumerate(ordered):
            layer.name, layer.visible = f"{base_name} ({position + 1})", position == 0
        if all(layer.id is None for layer in ordered):
            image.pending = [layer for layer in ordered if layer in image.pending] or image.pending
            return ordered
        self.activate(image)
        for layer in reversed(ordered):
            if layer.group is not None:
                self.client.run("layer.moveTo", layer=layer.id, target=int(layer.group), position="into")
            else:
                self.client.run("layer.arrange.bringToFront", layer=layer.id)
        for layer in ordered:
            self.client.run("layer.setProps", layer=layer.id, name=layer.name, visible=layer.visible)
        self.client.run("layer.select", layer=ordered[0].id)
        return ordered

    def show_variation(self, image: PCImage, layers: list[PCLayer], index: int) -> None:
        """The variations picker: only ``layers[index]`` visible (two steps, not one per layer)."""
        self.commit(image)
        self.activate(image)
        for i, layer in enumerate(layers):
            want = i == index
            if layer.visible != want and layer.id is not None:
                self.client.run("layer.setProps", layer=layer.id, visible=want)
                layer.visible = want
        if layers[index].id is not None:
            self.client.run("layer.select", layer=layers[index].id)

    def resize_canvas(self, image: PCImage, new_w: int, new_h: int, off_x: int, off_y: int) -> None:
        """Grow or crop the canvas; ``off_x``/``off_y`` is where the old (0, 0) lands."""
        self.commit(image)
        self.activate(image)
        self.client.run("image.crop", x=-int(off_x), y=-int(off_y), width=int(new_w), height=int(new_h))
        image._snapshot = None
        image.selection = None  # the job's selection was for the old canvas

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
            opened = self.client.call("app.open", path=self._open_path(rel)) or {}
        finally:
            self._forget(rel)
        index = int(self.session().get("active"))
        image = self.track(PCImage(self, str(opened.get("name") or rel), index))
        base = (self.client.run("document.inspect") or {}).get("activeLayer")
        if base is not None:
            self.client.run("layer.setProps", layer=base, name=base_name if len(images) == 1 else f"{base_name} (1)")
        for number, data in enumerate(images[1:], start=2):
            with Image.open(_io.BytesIO(data)) as im:
                rgba = im.convert("RGBA")
            extra = PCLayer(
                None, f"{base_name} ({number})", Rect(0, 0, rgba.width, rgba.height), rgba.tobytes(), visible=False
            )
            scratch, _ids, top = self._build(image, [extra], "")
            try:
                self.activate(image)
                self._copy_in(image, scratch, top)
            finally:
                self._close(scratch)
                self.activate(image)
        if base is not None:
            self.client.run("layer.select", layer=base)
        return image

    def finish(self, image: PCImage) -> None:
        """End of a job: the results go into the document (a failed job leaves it untouched)."""
        try:
            self.commit(image)
        finally:
            image.selection = None
            image._snapshot = None

    def discard(self, image: PCImage) -> None:
        """A failed or cancelled job: forget results that never reached the document."""
        image.pending, image.pending_group = [], ""
        image.selection = None
        image._snapshot = None


def default_profile() -> Path:
    """Where the companion keeps settings, logs and the exchange folder."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "OpenFill for PhotoCraft"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenFill for PhotoCraft"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openfill-photocraft"
