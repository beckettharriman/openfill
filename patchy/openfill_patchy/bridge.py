# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Patchy's side of OpenFill: the functions ``openfill.app.App`` calls on ``App.io``.

It mirrors ``openfill.gimp_io`` over Patchy's connector. Pixels travel as PNG files in the
profile's exchange folder; the scripts Patchy runs hold their paths, never the pixels.

The user's document is only read until a job's result is ready, and then changed in one
script run, which is one step in Patchy's history, so one Undo takes a fill (or a whole
expand) back:

- out: one read-only run writes the visible image (``doc.getCompositePixels``) and the
  selection's coverage (``doc.selection.getMask``) to files.
- in: results are kept here (``PatchyImage.ops``) while OpenFill still works on them (seam
  correction, grain, ranking), together with an expand's canvas changes. At the end of the
  job one run applies them all: the canvas (``resizeCanvas`` with an offset, as Image >
  Canvas Size), then each layer (``addLayer`` and ``setPixels``), its mask made from the
  job's own saved selection or mask (``setLayerMask``, never the live selection) and, for
  variations, their group. A fresh
  ``get_state`` comes right before it, so the run never meets a token the artist's work
  made stale.
"""

from __future__ import annotations

import io as _io
import itertools
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageStat

from openfill import png
from openfill.geometry import Rect, flatten_over_mean

from .client import PatchyClient, PatchyError, WorkspaceLost


class BridgeError(RuntimeError):
    """Patchy did not do what OpenFill asked."""


@dataclass(eq=False)
class PatchyLayer:
    """A layer OpenFill made: its pixels, and its id in Patchy once it is in the document."""

    id: str | None
    name: str
    origin: Rect
    rgba: bytes  # origin-sized RGBA
    group: str | None = None
    visible: bool = True
    # How its mask is made: "" none, "selection" (the job's selection grown by ``grow``),
    # "pixels" (``mask``, origin-sized); both feathered by ``feather`` when Patchy renders it.
    mask_kind: str = ""
    grow: int = 0
    feather: float = 0.0
    mask: bytes | None = None
    selection: bytes | None = None  # the job's selection (``selection_size``), for a "selection" mask
    selection_size: tuple[int, int] = (0, 0)

    def get_name(self) -> str:
        return self.name

    def get_width(self) -> int:
        return self.origin.width

    def get_height(self) -> int:
        return self.origin.height


@dataclass
class CanvasOp:
    """A canvas change still to be made: the new size, where the old (0, 0) lands, and the
    colour a Background layer gets in the new area."""

    width: int
    height: int
    dx: int
    dy: int
    fill: str = "#ffffff"


@dataclass
class LayersOp:
    """Layers still to be added (top first), in a group when ``group`` is set."""

    layers: list[PatchyLayer]
    group: str = ""


@dataclass(eq=False)
class PatchyImage:
    """The Patchy document a job works on, by its document id."""

    bridge: PatchyBridge
    id: str
    name: str = ""
    layers: list[PatchyLayer] = field(default_factory=list)
    ops: list[CanvasOp | LayersOp] = field(default_factory=list)  # made, not yet in the document
    pending_group: str = ""  # the group the last pending variations go into
    selection: bytes | None = None  # the job's selection, canvas-sized 8-bit
    selection_size: tuple[int, int] = (0, 0)
    shift: tuple[int, int] = (0, 0)  # where the job's original (0, 0) is now (an expand grows the canvas)
    committed: list[CanvasOp | LayersOp] = field(default_factory=list)  # what the last commit applied
    result_state: str | None = None  # the history state OpenFill's last step left
    steps: int = 0  # OpenFill's steps since that state's job began (the result, then picks)
    _snapshot: tuple[str, Image.Image, Image.Image | None] | None = None  # (state token, visible, selection)

    @property
    def pending(self) -> list[PatchyLayer]:
        """The layers that have not reached the document yet."""
        return [layer for op in self.ops if isinstance(op, LayersOp) for layer in op.layers]

    def size(self) -> tuple[int, int]:
        """The canvas as the job sees it: the document's, changed by pending canvas changes."""
        for op in reversed(self.ops):
            if isinstance(op, CanvasOp):
                return op.width, op.height
        d = self.bridge.document(self)
        return int(d["width"]), int(d["height"])

    def get_width(self) -> int:
        return self.size()[0]

    def get_height(self) -> int:
        return self.size()[1]

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


# The scripts Patchy runs. ``A`` is the JSON the bridge passes; everything else is fixed.
SNAPSHOT_JS = """
var d = app.getDocument(A.doc);
patchy.io.writeImage(A.visible, d.getCompositePixels());
var s = d.selection.exists ? d.selection.getMask({x: 0, y: 0, width: d.width, height: d.height}) : null;
if (s) patchy.io.writeImage(A.mask, s);
patchy.setResult({width: d.width, height: d.height, selection: !!s});
"""

# A canvas change is Image > Canvas Size: the content and the selection move, and a
# Background layer is filled out to the new canvas. Then the layers go on top, bottom first,
# so the first of each op ends up the top one.
INSERT_JS = """
var d = app.getDocument(A.doc);
function block(path, x, y, gray) {
  var b = gray ? patchy.io.readImage(path, {grayscale: true}) : patchy.io.readImage(path);
  b.x = x; b.y = y;
  return b;
}
// Every file is read before the first change, so a missing one changes nothing.
A.ops.forEach(function (op) {
  (op.layers || []).forEach(function (s) {
    s.block = block(s.pixels, s.x, s.y, false);
    if (s.mask) s.maskBlock = block(s.mask, s.x, s.y, true);
  });
});
// Keep: the job's step goes first (an undo must come before the run's first change).
for (var u = 0; u < A.undo; u++) if (!d.undo()) throw new Error("OpenFill's result is no longer the last step");
var made = [];
A.ops.forEach(function (op) {
  if (op.kind === "canvas") {
    d.resizeCanvas(op.width, op.height, {x: op.x, y: op.y, fill: op.fill});
    return;
  }
  var group = op.group ? d.addGroup(op.group) : null;
  var ids = [];
  for (var i = op.layers.length - 1; i >= 0; i--) {
    var s = op.layers[i];
    var layer = d.addLayer(s.name);
    layer.setPixels(s.block);
    if (s.maskBlock) {
      var m = s.maskBlock;
      layer.setLayerMask({x: m.x, y: m.y, width: m.width, height: m.height, data: m.data, feather: s.feather});
    }
    layer.visible = s.visible;
    if (group) d.moveLayers([layer], {parentId: group.id});
    ids.unshift(layer.id);
  }
  made.push({group: group ? group.id : null, layers: ids});
});
if (made.length) d.activeLayer = d.getLayer(made[made.length - 1].layers[0]);
patchy.setResult({ops: made});
"""

SHOW_JS = """
var d = app.getDocument(A.doc);
A.layers.forEach(function (row) { var l = d.getLayer(row.id); l.visible = row.visible; if (row.name) l.name = row.name; });
if (A.active) d.activeLayer = d.getLayer(A.active);
patchy.setResult(true);
"""

REORDER_JS = """
var d = app.getDocument(A.doc);
A.order.forEach(function (row) {
  var l = d.getLayer(row.id);
  d.moveLayers([l], {parentId: row.parent});
  l.name = row.name; l.visible = row.visible;
});
d.activeLayer = d.getLayer(A.order[A.order.length - 1].id);
patchy.setResult(true);
"""

REMOVE_JS = """
var d = app.getDocument(A.doc);
A.ids.forEach(function (id) { d.getLayer(id).remove(); });
patchy.setResult(true);
"""

SET_PIXELS_JS = """
var d = app.getDocument(A.doc);
var b = patchy.io.readImage(A.pixels);
b.x = A.x; b.y = A.y;
d.getLayer(A.layer).setPixels(b);
patchy.setResult(true);
"""

NEW_IMAGE_JS = """
var first = patchy.io.readImage(A.images[0].pixels);
var d = app.newDocument(first.width, first.height, {name: A.name});
var base = d.layers[0];
base.setPixels(first);
base.name = A.images[0].name;
for (var i = 1; i < A.images.length; i++) {
  var l = d.addLayer(A.images[i].name);
  l.setPixels(patchy.io.readImage(A.images[i].pixels));
  l.visible = false;
}
d.activeLayer = base;
patchy.setResult({doc: d.id});
"""


def script(body: str, args: dict[str, Any]) -> str:
    """``body`` with its arguments as ``A`` (JSON is valid JavaScript)."""
    return "var A = " + json.dumps(args) + ";\n" + body


def grow_mask(mask: Image.Image, grow: int) -> Image.Image:
    """``mask`` grown by ``grow`` pixels (a square dilation, in steps of two)."""
    while grow >= 2:
        mask, grow = mask.filter(ImageFilter.MaxFilter(5)), grow - 2
    if grow:
        mask = mask.filter(ImageFilter.MaxFilter(3))
    return mask


def mean_colour(im: Image.Image) -> str:
    """The alpha-weighted mean colour of ``im`` as ``#rrggbb`` (grey when nothing shows), the
    colour ``flatten_over_mean`` puts behind transparent pixels."""
    alpha = im.getchannel("A")
    total = ImageStat.Stat(alpha).sum[0]
    if total <= 0:
        return "#808080"
    rgb = [ImageStat.Stat(ImageChops.multiply(im.getchannel(c), alpha)).sum[0] for c in "RGB"]
    return "#" + "".join(f"{min(255, round(v * 255 / total)):02x}" for v in rgb)


class PatchyBridge:
    """``App.io`` for Patchy (same function names as ``openfill.gimp_io``)."""

    def __init__(self, client: PatchyClient, exchange_dir: Path, profile: Path, notify=None) -> None:
        self.client = client
        self.exchange = Path(exchange_dir)
        self.profile = Path(profile)
        self.notify = notify or (lambda _text: None)
        self.on_region: Callable[[tuple[int, int, int, int]], None] | None = None
        self._names = itertools.count(1)
        self._known_images: list[PatchyImage] = []
        self.exchange.mkdir(parents=True, exist_ok=True)

    # documents -------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return self.client.get_state()

    def document(self, image: PatchyImage, state: dict[str, Any] | None = None) -> dict[str, Any]:
        for d in (state or self.state()).get("documents") or []:
            if str(d.get("id")) == image.id:
                image.name = str(d.get("name") or image.name)
                return d
        raise BridgeError(f"the document '{image.name or image.id}' is no longer open in Patchy")

    def active_image(self) -> PatchyImage | None:
        state = self.state()
        active = state.get("activeDocumentId")
        for d in state.get("documents") or []:
            if active is not None and str(d.get("id")) == str(active):
                return PatchyImage(self, str(d["id"]), str(d.get("name") or ""))
        return None

    def history(self, image: PatchyImage) -> str:
        """Where the document's history is (Patchy's history state id)."""
        return str(self.document(image).get("historyStateId") or "")

    def _file(self, stem: str) -> Path:
        return self.exchange / f"OpenFill {stem} {os.getpid()}-{next(self._names)}.png"

    def _write(self, image: Image.Image, stem: str) -> Path:
        path = self._file(stem)
        image.save(path, compress_level=1)
        return path

    @staticmethod
    def _forget(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass

    def _run(self, body: str, args: dict[str, Any], name: str = "OpenFill") -> Any:
        try:
            return self.client.run_script(script(body, args), name=name)
        except PatchyError as err:
            logs = err.data.get("logs") if isinstance(err.data, dict) else None
            detail = "; ".join(str(entry.get("message", entry)) for entry in logs or [] if isinstance(entry, dict))
            if detail:
                raise PatchyError(f"{err} ({detail})", err.code, err.data) from err
            raise

    # the functions App calls --------------------------------------------------------

    def profile_dir(self) -> str:
        return str(self.profile)

    def message(self, text: str) -> None:
        self.notify(text)

    def GimpProgress(self, title: str) -> _Progress:  # noqa: N802 - the name App calls
        return _Progress(title)

    def is_indexed(self, image: PatchyImage) -> bool:
        return False  # palette-mode documents take RGBA too; Patchy snaps the pixels to the palette

    def image_rect(self, image: PatchyImage) -> Rect:
        width, height = image.size()
        return Rect(0, 0, width, height)

    def selection_rect(self, image: PatchyImage) -> Rect | None:
        selection = self.document(image).get("selection") or {}
        bounds = selection.get("bounds") if selection.get("exists") else None
        if not bounds:
            return None
        x, y, w, h = (int(bounds[k]) for k in ("x", "y", "width", "height"))
        return Rect(x, y, w, h) if w > 0 and h > 0 else None

    def _snapshot(self, image: PatchyImage) -> tuple[Image.Image, Image.Image | None]:
        """The document's visible image (RGBA) and selection (L, or None), both canvas-sized,
        from one read-only run. Kept while the state token stays the same."""
        self.document(image, self.state())  # also refreshes the token
        token = self.client.state_token
        if image._snapshot and image._snapshot[0] == token:
            return image._snapshot[1], image._snapshot[2]
        visible_path, mask_path = self._file("visible"), self._file("mask")
        try:
            facts = self._run(
                SNAPSHOT_JS, {"doc": image.id, "visible": str(visible_path), "mask": str(mask_path)}, "OpenFill: read"
            )
            with Image.open(visible_path) as im:
                visible = im.convert("RGBA")
            selection = None
            if (facts or {}).get("selection"):
                with Image.open(mask_path) as im:
                    selection = im.convert("L")
        finally:
            self._forget([visible_path, mask_path])
        image._snapshot = (self.client.state_token, visible, selection)
        return visible, selection

    def visible(self, image: PatchyImage) -> Image.Image:
        """What the canvas will look like once the pending results are in: the document's
        visible image, moved by pending canvas changes, under the pending layers. An expand's
        later rounds read their context from it."""
        canvas, _selection = self._snapshot(image)
        for op in image.ops:
            if isinstance(op, CanvasOp):
                grown = Image.new("RGBA", (op.width, op.height), (0, 0, 0, 0))
                grown.paste(canvas, (op.dx, op.dy))
                canvas = grown
                continue
            for layer in reversed(op.layers):
                if not layer.visible:
                    continue
                pixels = Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), layer.rgba)
                mask = self._mask(layer)
                if mask is not None:
                    if layer.feather > 0:
                        mask = mask.filter(ImageFilter.GaussianBlur(layer.feather / 2))
                    pixels.putalpha(ImageChops.multiply(pixels.getchannel("A"), mask))
                full = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                full.paste(pixels, (layer.origin.x, layer.origin.y))
                canvas = Image.alpha_composite(canvas, full)
        return canvas

    def export_region(
        self, image: PatchyImage, rect: Rect, transparent_outside: bool = False, keep_alpha: bool = False
    ) -> tuple[bytes, bytes]:
        """(opaque RGBA PNG, raw RGBA) of ``rect``; outside the canvas is transparent before
        flattening over the mean colour, as GIMP does for an expand."""
        full = self.visible(image)
        if self.on_region:  # where this round's preview frames belong, in the job's first canvas
            self.on_region((rect.x - image.shift[0], rect.y - image.shift[1], rect.width, rect.height))
        crop = full.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        flat = crop.tobytes() if keep_alpha else flatten_over_mean(crop.tobytes())
        return png.encode(rect.width, rect.height, flat, 4), flat

    def export_region_png(self, image: PatchyImage, rect: Rect, transparent_outside: bool = False) -> bytes:
        return self.export_region(image, rect, transparent_outside)[0]

    def export_selection_mask(self, image: PatchyImage, rect: Rect) -> tuple[bytes, bytes]:
        """(8-bit PNG, raw bytes) of the selection inside ``rect``; 255 = selected. The whole
        selection is kept for the result's mask, so it is exactly this one even if the artist
        selects something else while the job runs."""
        full, selection = self._snapshot(image)
        if selection is None:
            selection = Image.new("L", full.size, 255)
        image.selection, image.selection_size = selection.tobytes(), selection.size
        crop = selection.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        raw = crop.tobytes()
        return png.encode(rect.width, rect.height, raw, 1), raw

    def export_selection_mask_png(self, image: PatchyImage, rect: Rect) -> bytes:
        return self.export_selection_mask(image, rect)[0]

    def mask_png_from_bytes(self, rect: Rect, pixels: bytes) -> bytes:
        return png.encode(rect.width, rect.height, pixels, 1)

    # writing ------------------------------------------------------------------------

    def insert_results(
        self,
        image: PatchyImage,
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
    ) -> list[PatchyLayer]:
        """Generated PNGs as layers at ``origin`` with a mask (see ``gimp_io.insert_results``).
        Several variations go into a group; only the first is visible. They reach the document
        at the end of the job (``commit``)."""
        layers: list[PatchyLayer] = []
        for index, data in enumerate(images):
            name = base_name if len(images) == 1 else f"{base_name} ({index + 1})"
            with Image.open(_io.BytesIO(data), formats=["PNG"]) as im:  # results are PNGs OpenFill made
                rgba = im.convert("RGBA")
            if scale_to_origin and rgba.size != (origin.width, origin.height):
                rgba = rgba.resize((origin.width, origin.height), Image.LANCZOS)
            kind = "" if not use_selection_mask else ("pixels" if mask_pixels is not None else "selection")
            layers.append(
                PatchyLayer(
                    None,
                    name,
                    origin,
                    rgba.tobytes(),
                    None,
                    index == 0,
                    kind,
                    grow,
                    feather,
                    mask_pixels,
                    image.selection,
                    image.selection_size,
                )
            )
        image.pending_group = base_name if group_variations and len(images) > 1 else ""
        image.ops.append(LayersOp(layers, image.pending_group))
        image.layers.extend(layers)
        return layers

    def layer_rgba(self, layer: PatchyLayer) -> bytes:
        return layer.rgba

    def set_layer_rgba(self, layer: PatchyLayer, data: bytes) -> None:
        """Replace a layer's pixels: here while it is pending, else in the document (one step)."""
        layer.rgba = data
        if layer.id is None:
            return
        image = self._image_of(layer)
        path = self._write(Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), data), "layer")
        try:
            self._run(
                SET_PIXELS_JS,
                {"doc": image.id, "layer": layer.id, "pixels": str(path), "x": layer.origin.x, "y": layer.origin.y},
            )
        finally:
            self._forget([path])

    def _mask(self, layer: PatchyLayer) -> Image.Image | None:
        """The origin-sized 8-bit mask ``layer`` gets (before Patchy feathers it), or None."""
        size = (layer.origin.width, layer.origin.height)
        if layer.mask_kind == "pixels" and layer.mask is not None:
            return Image.frombytes("L", size, layer.mask)
        if layer.mask_kind != "selection":
            return None
        width, height = layer.selection_size
        if layer.selection is None or len(layer.selection) != width * height:
            return Image.new("L", size, 255)
        selection = Image.frombytes("L", (width, height), layer.selection)
        margin = max(0, int(layer.grow))
        o = layer.origin
        around = selection.crop((o.x - margin, o.y - margin, o.x + o.width + margin, o.y + o.height + margin))
        return grow_mask(around, margin).crop((margin, margin, margin + o.width, margin + o.height))

    def _layer_args(self, layer: PatchyLayer, files: list[Path]) -> dict[str, Any]:
        pixels = self._write(Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), layer.rgba), "layer")
        files.append(pixels)
        row: dict[str, Any] = {
            "name": layer.name,
            "pixels": str(pixels),
            "x": layer.origin.x,
            "y": layer.origin.y,
            "visible": bool(layer.visible),
            # Patchy's mask feather is a Gaussian's sigma; GIMP's feather is about twice that.
            "feather": round(max(0.0, float(layer.feather)) / 2, 2),
        }
        mask = self._mask(layer)
        if mask is not None:
            files.append(self._write(mask, "mask"))
            row["mask"] = str(files[-1])
        return row

    def _apply(self, image: PatchyImage, ops: list[CanvasOp | LayersOp], undo: int = 0) -> list[dict]:
        """One run: ``undo`` Undos, then ``ops`` in order; the ids Patchy gave each layers op,
        ``{"group": id or None, "layers": [top first]}``."""
        files: list[Path] = []
        try:
            rows: list[dict[str, Any]] = []
            for op in ops:
                if isinstance(op, CanvasOp):
                    rows.append(
                        {
                            "kind": "canvas",
                            "width": op.width,
                            "height": op.height,
                            "x": op.dx,
                            "y": op.dy,
                            "fill": op.fill,
                        }
                    )
                else:
                    layers = [self._layer_args(layer, files) for layer in op.layers]
                    rows.append({"kind": "layers", "group": op.group, "layers": layers})
            names = [op.layers[0].name for op in ops if isinstance(op, LayersOp) and op.layers]
            label = op_label(names[-1] if names else "Generative Expand")
            before = str(self.document(image, self.state()).get("historyStateId") or "")  # a fresh token
            try:
                result = self._run(INSERT_JS, {"doc": image.id, "undo": int(undo), "ops": rows}, label)
            except WorkspaceLost:
                raise
            except PatchyError as err:
                err.restored = self._take_back_partial(image, before, undo > 0)
                raise
        finally:
            self._forget(files)
        made = list((result or {}).get("ops") or [])
        if len(made) != sum(isinstance(op, LayersOp) for op in ops):
            raise BridgeError("Patchy did not report the layers it added")
        return made

    def _take_back_partial(self, image: PatchyImage, before: str, undid: bool) -> bool:
        """A run that failed half way leaves its changes as one step: undo it. True when the
        document is as it was before the run; False when a Keep's Undos went through and
        cannot be brought back with Redo (``keep_one`` adds the variations again then)."""
        try:
            d = self.document(image, self.state())
            if str(d.get("historyStateId") or "") == before:
                return True  # nothing changed
            if undid and d.get("canRedo"):
                # Only Undos went through (a change would have emptied Redo): redo them. Redo
                # holds nothing older, since OpenFill's step came last.
                while d.get("canRedo") and self.client.redo(image.id):
                    d = self.document(image, self.state())
                return str(d.get("historyStateId") or "") == before
            self.client.undo(image.id)  # the run's own step
            return not undid
        except PatchyError:
            return not undid

    def _note_step(self, image: PatchyImage, before: str | None) -> None:
        """After one of OpenFill's own runs: count it while the history was still where
        OpenFill left it (``before``), so Keep and Discard can take all of them back."""
        if before is not None and before == image.result_state:
            image.steps += 1
            image.result_state = self.history(image)
        else:
            image.steps, image.result_state = 0, None

    def unchanged(self, image: PatchyImage) -> bool:
        """True while the document's history is where OpenFill's last step left it."""
        try:
            return image.result_state is not None and self.history(image) == image.result_state
        except PatchyError:
            return False

    def commit(self, image: PatchyImage) -> None:
        """Put the pending results (and canvas changes) into the document in one step."""
        ops = list(image.ops)
        if not ops:
            return
        if image.pending_group:  # the job may have renamed it (Object Removal)
            for op in reversed(ops):
                if isinstance(op, LayersOp):
                    op.group = image.pending_group if len(op.layers) > 1 else ""
                    break
        # The results stay pending until they are in the document, so a failure here can be
        # tried again and never loses them.
        made = self._apply(image, ops)
        self._take_ids([op for op in ops if isinstance(op, LayersOp)], made)
        image.ops, image.pending_group, image.committed = [], "", ops
        image.steps, image.result_state = 1, self.history(image)
        image._snapshot = None

    @staticmethod
    def _take_ids(ops: list[LayersOp], made: list[dict]) -> None:
        for op, ids in zip(ops, made, strict=True):
            layer_ids = [str(i) for i in ids.get("layers") or []]
            if len(layer_ids) != len(op.layers):
                raise BridgeError("Patchy did not report the layers it added")
            for layer, layer_id in zip(op.layers, layer_ids, strict=True):
                layer.id, layer.group = layer_id, (str(ids["group"]) if ids.get("group") else None)

    def _image_of(self, layer: PatchyLayer) -> PatchyImage:
        for image in self._known_images:
            if layer in image.layers:
                return image
        raise BridgeError("layer of an unknown document")

    def track(self, image: PatchyImage) -> PatchyImage:
        """Remember ``image`` so its layers can be found again (``set_layer_rgba``)."""
        if image not in self._known_images:
            self._known_images.append(image)
        return image

    def reorder_variations(
        self, image: PatchyImage, layers: list[PatchyLayer], order: list[int], base_name: str
    ) -> list[PatchyLayer]:
        """Best first: the first in ``order`` becomes the top, visible, selected layer."""
        if len(layers) < 2 or order == list(range(len(layers))):
            return layers
        ordered = [layers[i] for i in order]
        for position, layer in enumerate(ordered):
            layer.name, layer.visible = f"{base_name} ({position + 1})", position == 0
        if all(layer.id is None for layer in ordered):
            for op in image.ops:
                if isinstance(op, LayersOp) and set(map(id, op.layers)) == set(map(id, ordered)):
                    op.layers = ordered
            return ordered
        rows = [
            {"id": layer.id, "parent": layer.group, "name": layer.name, "visible": layer.visible}
            for layer in reversed(ordered)  # each move puts it on top of its folder
        ]
        before = self.history(image)
        self._run(REORDER_JS, {"doc": image.id, "order": rows})
        self._note_step(image, before)
        return ordered

    def show_variation(self, image: PatchyImage, layers: list[PatchyLayer], index: int) -> None:
        """The variations picker: only ``layers[index]`` visible (one step)."""
        self.commit(image)
        rows = []
        for i, layer in enumerate(layers):
            layer.visible = i == index
            if layer.id is not None:
                rows.append({"id": layer.id, "visible": layer.visible})
        if rows:
            before = self.history(image)
            self._run(SHOW_JS, {"doc": image.id, "layers": rows, "active": layers[index].id})
            self._note_step(image, before)

    def remove_layers(self, image: PatchyImage, layer_ids: list[str]) -> None:
        """Delete layers (or a group) from the document, in one step."""
        if layer_ids:
            self._run(REMOVE_JS, {"doc": image.id, "ids": [str(i) for i in layer_ids]})
            image.steps, image.result_state = 0, None

    def rewind(self, image: PatchyImage) -> None:
        """Discard: take OpenFill's steps back (the result and any picks), only while the
        history is still where OpenFill left it, so an artist's later edit is never undone."""
        if not self.unchanged(image):
            raise BridgeError("the document changed since OpenFill's result was added")
        for _ in range(image.steps):
            self.state()  # the token the Undo needs
            if not self.client.undo(image.id):
                raise BridgeError("Patchy could not undo OpenFill's result")
        image.steps, image.result_state = 0, None
        image._snapshot = None

    def keep_one(self, image: PatchyImage, chosen: PatchyLayer, name: str) -> None:
        """Keep: the job's step becomes ``chosen`` alone, still one step. One run undoes
        OpenFill's steps and adds what the job added again (an expand's canvas, earlier
        rounds) with ``chosen`` as the only variation, from the job's stored pixels and mask.
        Only while the history is still where OpenFill left it."""
        if not self.unchanged(image):
            raise BridgeError("the document changed since OpenFill's result was added")
        kept = PatchyLayer(
            None,
            name,
            chosen.origin,
            chosen.rgba,
            None,
            True,
            chosen.mask_kind,
            chosen.grow,
            chosen.feather,
            chosen.mask,
            chosen.selection,
            chosen.selection_size,
        )
        ops: list[CanvasOp | LayersOp] = []
        for op in image.committed:
            if isinstance(op, LayersOp) and chosen in op.layers:
                ops.append(LayersOp([kept]))
            else:
                ops.append(op)
        try:
            made = self._apply(image, ops, undo=image.steps)
        except WorkspaceLost:
            raise
        except PatchyError as err:
            if not getattr(err, "restored", True):
                # The job's step is gone with nothing in its place: add the variations again.
                restored = self._apply(image, image.committed)
                self._take_ids([op for op in image.committed if isinstance(op, LayersOp)], restored)
                image.steps, image.result_state = 1, self.history(image)
            raise
        self._take_ids([op for op in ops if isinstance(op, LayersOp)], made)
        image.steps, image.result_state = 1, self.history(image)
        chosen.id, chosen.group, chosen.visible, chosen.name = kept.id, None, True, name
        image.committed = ops
        image._snapshot = None

    def resize_canvas(self, image: PatchyImage, new_w: int, new_h: int, off_x: int, off_y: int) -> None:
        """Grow or crop the canvas; ``off_x``/``off_y`` is where the old (0, 0) lands. Made with
        the results, in the same step. A Background layer is filled out to the new canvas
        with the image's mean colour (the kind of colour ``export_region`` flattens the new
        area over for the generator), so a gap the result leaves, such as its feathered edge,
        is close to its surroundings instead of white. Every round of an expand uses the
        first round's colour, from the image alone, so the new area is one colour."""
        earlier = [op.fill for op in image.ops if isinstance(op, CanvasOp)]
        fill = earlier[0] if earlier else mean_colour(self.visible(image))
        image.ops.append(CanvasOp(int(new_w), int(new_h), int(off_x), int(off_y), fill))
        image.shift = (image.shift[0] + int(off_x), image.shift[1] + int(off_y))
        image.selection = None  # the job's selection was for the old canvas

    def clip_layers_to_canvas(self, image: PatchyImage) -> None:
        """Patchy keeps pixels beyond the canvas, as Photoshop does; nothing to cut."""

    def new_image_from_pngs(self, images: list[bytes], base_name: str, temp_dir: Path | None) -> PatchyImage:
        """A new document from the first image; further variations become hidden layers."""
        files: list[Path] = []
        rows = []
        try:
            for number, data in enumerate(images, start=1):
                with Image.open(_io.BytesIO(data), formats=["PNG"]) as im:  # results are PNGs OpenFill made
                    files.append(self._write(im.convert("RGBA"), "image"))
                name = base_name if len(images) == 1 else f"{base_name} ({number})"
                rows.append({"name": name, "pixels": str(files[-1])})
            self.state()
            result = self._run(NEW_IMAGE_JS, {"name": base_name[:60] or "Text to Image", "images": rows}, base_name)
        finally:
            self._forget(files)
        return self.track(PatchyImage(self, str((result or {}).get("doc")), base_name))

    def finish(self, image: PatchyImage) -> None:
        """End of a job: the results go into the document (a failed job leaves it untouched)."""
        try:
            self.commit(image)
        finally:
            image.selection = None
            image._snapshot = None

    def discard(self, image: PatchyImage) -> None:
        """A failed or cancelled job: forget results that never reached the document."""
        image.ops, image.pending_group = [], ""
        image.selection = None
        image._snapshot = None


def op_label(name: str) -> str:
    """The history step's name: the result's layer name (variations without their number)."""
    return re.sub(r" \(\d+\)$", "", name)[:80] or "OpenFill"


def default_profile() -> Path:
    """Where the companion keeps settings, logs and the exchange folder."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "OpenFill for Patchy"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenFill for Patchy"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openfill-patchy"
