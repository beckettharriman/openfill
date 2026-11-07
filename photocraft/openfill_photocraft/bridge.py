# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""PhotoCraft's side of OpenFill: the functions ``openfill.app.App`` calls on ``App.io``.

It mirrors ``openfill.gimp_io`` over PhotoCraft's control channel. Pixels travel as
files through the folder PhotoCraft was given as its automation root.

The user's document is only read until a job's result is ready, and then changed in
one step, so one Undo takes a fill back:

- out: the document is duplicated as one merged layer and saved; the selection is
  saved into that duplicate (not into the document) and saved from there.
- in: results are kept here (``PCImage.pending``) while OpenFill still works on them
  (seam correction, grain, ranking). They are then written as one PSD that already
  holds every layer, its mask, name and visibility and the variations' group, opened
  as a scratch document and copied into the document with one ``layer.copyToDocument``.
  The clipboard is never used.

The user should never see these documents. PhotoCraft has no hidden documents and no
way to read or write pixels without one, and both ``app.open`` and ``app.save`` work on
the active document, so a scratch document is the active one for a moment. OpenFill
keeps that moment as short as it can and makes it look like the user's own document:

- the duplicate holds the document's own pixels and is shown at the document's zoom
  and position; from PhotoCraft 0.18 on, its selection is saved in the same TIFF as an
  alpha channel (older versions save it from a second, black and white document);
- the results' PSD carries the document's own picture as its bottom layer, and the
  document is made active again right after the open, before anything else;
- an expand grows the canvas only once its layers are in, so the new area is never
  shown empty.
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

from PIL import Image, ImageChops, ImageFilter

from openfill import png
from openfill.geometry import Rect, flatten_over_mean

from . import psd
from .control import ControlClient, ControlError

# The first PhotoCraft that saves an alpha channel into a flat TIFF (``_selection_tiff``).
TIFF_CHANNELS_FROM = (0, 18)
# The scratch document's bottom layer: the document's own picture, never copied.
BACKDROP = "OpenFill backdrop"


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
    selection: bytes | None = None  # the job's selection (canvas-sized), for a "selection" mask built later

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
    shift: tuple[int, int] = (0, 0)  # where the job's original (0, 0) is now (an expand grows the canvas)
    # An expand's canvas change that waits for its layers (``resize_canvas``): the new width and
    # height, and where the old (0, 0) lands.
    pending_crop: tuple[int, int, int, int] | None = None

    def _inspect(self) -> dict:
        self.bridge.activate(self)
        return self.bridge.client.run("document.inspect")

    def size(self) -> tuple[int, int]:
        """The canvas size, as it is once the pending canvas change is made."""
        if self.pending_crop is not None:
            return self.pending_crop[0], self.pending_crop[1]
        d = self._inspect()
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
        self.on_region: Callable[[tuple[int, int, int, int]], None] | None = None
        self._names = itertools.count(1)
        self._known_images: list[PCImage] = []
        self._version: tuple[int, ...] | None = None
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

    def _file(self, stem: str, ext: str = ".png") -> str:
        """A fresh name in the exchange folder (relative, as PhotoCraft wants it)."""
        return f"OpenFill {stem} {os.getpid()}-{next(self._names)}{ext}"

    def photocraft_version(self) -> tuple[int, ...]:
        """PhotoCraft's version, (0,) when it does not say (before 0.16)."""
        if self._version is None:
            text = str((self.client.call("ui.inspect").get("updates") or {}).get("currentVersion") or "")
            found = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
            self._version = tuple(int(n) for n in found.groups(default="0")) if found else (0,)
        return self._version

    def _view(self, session: dict | None = None) -> dict | None:
        """The active document's zoom and centre, as ``ui.set`` takes them (None if unknown)."""
        try:
            inspected = self.client.call("ui.inspect")
            active = int((session or inspected.get("session") or {}).get("active"))
            view = (inspected.get("views") or [])[active]
            return {"zoom": float(view["zoom"]), "center": [float(v) for v in view["center"]]}
        except (ControlError, LookupError, TypeError, ValueError):
            return None

    def _show_like(self, view: dict | None) -> None:
        """Show the active document at ``view`` (a scratch copy then looks like the original).
        A new document fits itself to the window on its first frames: after that, or the fit
        would undo it."""
        if not view:
            return
        try:
            for _ in range(10):
                inspected = self.client.call("ui.inspect")
                active = (inspected.get("views") or [])[int((inspected.get("session") or {}).get("active"))]
                if not active.get("fit_pending") and not active.get("fill_pending"):
                    break
            self.client.call("ui.set", **view)
        except (ControlError, LookupError, TypeError, ValueError):
            pass

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

    def _open_path(self, rel: str) -> str:
        """``rel`` (in the exchange folder) as app.open wants it: relative to the read root."""
        return os.path.relpath(self.exchange / rel, self.read_root).replace(os.sep, "/")  # PhotoCraft wants /

    def open_file(self, path: str) -> dict:
        """Open an image the user picked (inside the read root) in PhotoCraft."""
        try:
            rel = os.path.relpath(Path(path).resolve(), self.read_root.resolve())
        except ValueError:  # another drive (Windows)
            rel = ".."
        if rel.startswith(".."):
            raise BridgeError(f"PhotoCraft may only open files in {self.read_root} for OpenFill")
        return self.client.call("app.open", path=rel.replace(os.sep, "/")) or {}

    def _forget(self, rel: str) -> None:
        try:
            (self.exchange / rel).unlink()
        except OSError:
            pass

    def _write_bytes(self, data: bytes, stem: str, ext: str) -> str:
        rel = self._file(stem, ext)
        (self.exchange / rel).write_bytes(data)
        return rel

    def _open_scratch(self, image: PCImage, data: bytes) -> tuple[int, int]:
        """Open the PSD ``data`` as a scratch document and make ``image`` the active one again
        at once (``app.open`` shows what it opens): (the scratch document's index, ``image``'s)."""
        back = int(self._find(image)["index"])
        rel = self._write_bytes(data, "layers", ".psd")
        try:
            opened = self.client.call("app.open", path=self._open_path(rel)) or {}
        finally:
            self._forget(rel)
        # The very next request: PhotoCraft opens a document after the others, so the user's
        # keeps its index.
        try:
            self.client.run("document.activate", document=back)
        except ControlError:
            pass
        session = self.session()
        docs = session.get("documents") or []
        name = str(opened.get("name") or Path(rel).name)
        scratch = [int(d["index"]) for d in docs if d.get("name") == name and d.get("index") != back]
        if not scratch:
            raise BridgeError("PhotoCraft did not open OpenFill's layers")
        if session.get("active") != back:
            self.activate(image)
            back = int(self._find(image)["index"])
        return scratch[-1], back

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
        width, height = image.size()
        return Rect(0, 0, width, height)

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
        document changes.

        The duplicate has to be the active document while it is saved. It holds the
        document's pixels and is shown at the document's zoom and position, so the canvas
        looks the same; only its tab shows, for as long as the saves take."""
        self.commit(image)
        revision = self._revision(image)
        if image._snapshot and image._snapshot[0] == revision:
            return image._snapshot[1], image._snapshot[2]
        self.activate(image)
        back = int(self._find(image)["index"])
        d = self.client.run("document.inspect")
        bounds = d.get("selectionBounds") if d.get("hasSelection") else None
        view = self._view()
        self.client.run("image.duplicate", name="OpenFill export", mergedOnly=True)
        duplicate = int(self.session().get("active"))
        self._show_like(view)
        selection, opened = None, [duplicate]
        try:
            visible = self._visible()
            if d.get("hasSelection"):
                selection = self._selection_tiff(bounds)
                if selection is None:
                    selection = self._selection_document(image, duplicate, opened)
        finally:
            try:
                self.client.run("document.activate", document=back)  # first, then close the copies
            except ControlError:
                pass
            for index in sorted(opened, reverse=True):
                self._close(index)
            self.activate(image)
        image._snapshot = (revision, visible, selection)
        return visible, selection

    def _visible(self) -> Image.Image:
        """The active duplicate's pixels (RGBA). From PhotoCraft 0.18 on an uncompressed TIFF,
        which saves several times faster than a PNG (the duplicate shows for less time)."""
        if self.photocraft_version() >= TIFF_CHANNELS_FROM:
            rel = self._file("visible", ".tif")
            try:
                self.client.call("app.save", path=rel, tiffCompression="none", tiffTransparency=True)
                path = self.exchange / rel
                for _ in range(50):  # the save returns when written; allow a slow disk a moment
                    if path.is_file():
                        break
                    time.sleep(0.05)
                with Image.open(path, formats=["TIFF"]) as picture:
                    extra = tuple(picture.tag_v2.get(338) or ())
                    # colours, plus at most their transparency: an alpha channel the copy
                    # carried would read as transparency, so then the PNG
                    if picture.mode in ("RGB", "RGBA") and extra in ((), (1,), (2,)):
                        visible = picture.convert("RGBA")
                        self._forget(rel)
                        return visible
            except (ControlError, OSError, ValueError, SyntaxError):
                pass
            self._forget(rel)
        rel = self._file("visible")
        self.client.call("app.save", path=rel)
        return self._read(rel).convert("RGBA")

    def _selection_tiff(self, bounds) -> Image.Image | None:
        """The selection of the active duplicate (it keeps the document's), saved into it as an
        alpha channel and read from a flat TIFF that carries that channel (PhotoCraft 0.18 on).
        None when this PhotoCraft can't, or the file does not hold what it should."""
        if self.photocraft_version() < TIFF_CHANNELS_FROM:
            return None
        rel = self._file("selection", ".tif")
        try:
            saved = self.client.run("select.saveSelection", name="OpenFill selection") or {}
            self.client.call("app.save", path=rel, tiffCompression="none", tiffTransparency=False)
            selection = _tiff_channel(self.exchange / rel, saved.get("channel"))
        except ControlError:
            selection = None
        finally:
            self._forget(rel)
        if selection is None or not bounds:
            return None
        x, y, w, h = (int(v) for v in bounds)
        if selection.getbbox() != (x, y, x + w, y + h):
            return None  # not the selection: read it the long way
        return selection

    def _selection_document(self, image: PCImage, duplicate: int, opened: list[int]) -> Image.Image:
        """The selection the way every PhotoCraft can: saved into the duplicate, copied into a
        document of its own (black and white) and saved from there."""
        self.activate(image)
        self.client.run("select.saveSelection", name="OpenFill selection", document=duplicate)
        self.client.run("document.activate", document=duplicate)
        self.client.run("channel.duplicate", channel="OpenFill selection", document="new", name="OpenFill mask")
        opened.append(int(self.session().get("active")))
        rel = self._file("mask")
        self.client.call("app.save", path=rel)
        return self._read(rel).convert("L")

    def export_region(self, image: PCImage, rect: Rect, transparent_outside: bool = False) -> tuple[bytes, bytes]:
        """(opaque RGBA PNG, raw RGBA) of ``rect``; outside the canvas is transparent before
        flattening over the mean colour, as GIMP does for an expand."""
        full, _selection = self._snapshot(image)
        if self.on_region:  # where this round's preview frames belong, in the job's first canvas
            self.on_region((rect.x - image.shift[0], rect.y - image.shift[1], rect.width, rect.height))
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

    def decode_image(self, data: bytes) -> tuple[int, int, bytes]:
        """A JPEG or WebP picture from the cloud service as (width, height, RGBA bytes).

        Only those two formats, recognised by their first bytes and opened with only Pillow's
        reader for that format: an answer is never handed to any other reader (Ghostscript for
        EPS, ...). Its size is checked before the pixels are decoded."""
        from openfill import imaging

        kind = imaging.sniff(data)
        if kind not in ("jpeg", "webp"):
            raise ValueError("not a JPEG or WebP picture")
        with Image.open(_io.BytesIO(data), formats=["JPEG" if kind == "jpeg" else "WEBP"]) as im:
            if im.width * im.height > imaging.MAX_RESULT_PIXELS:
                raise ValueError(f"{im.width} x {im.height} pixels is larger than OpenFill accepts")
            rgba = im.convert("RGBA")
            return rgba.width, rgba.height, rgba.tobytes()

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
        self.commit(image, canvas=False)  # an earlier round's results come first; the canvas waits
        layers: list[PCLayer] = []
        for index, data in enumerate(images):
            name = base_name if len(images) == 1 else f"{base_name} ({index + 1})"
            with Image.open(_io.BytesIO(data), formats=["PNG"]) as im:  # results are always PNGs OpenFill made
                rgba = im.convert("RGBA")
            if scale_to_origin and rgba.size != (origin.width, origin.height):
                rgba = rgba.resize((origin.width, origin.height), Image.LANCZOS)
            kind = "" if not use_selection_mask else ("pixels" if mask_pixels is not None else "selection")
            layers.append(
                PCLayer(
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
                )
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
        self.activate(image)
        self.client.run("layer.select", layer=layer.id)
        copied = self._bring_in(image, [layer], "")
        self.client.run("layer.delete", layer=layer.id)
        layer.id = copied[0]
        self.client.run("layer.select", layer=copied[0])

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
            selection = layer.selection if layer.selection is not None else image.selection
            if selection is not None and len(selection) == size[0] * size[1]:
                return Image.frombytes("L", size, selection)
            return Image.new("L", size, 255)
        return None

    def _layer_mask(self, image: PCImage, layer: PCLayer, size: tuple[int, int]) -> Image.Image | None:
        """The canvas-sized 8-bit mask ``layer`` gets: a "selection" mask grown and feathered
        here as PhotoCraft's Select > Modify would (grown round, feather sigma = radius)."""
        mask = self._mask_image(image, layer, size)
        if mask is None or layer.mask_kind != "selection":
            return mask
        if layer.grow > 0:
            mask = _grow(mask, int(layer.grow))
        if layer.feather > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(float(layer.feather)))
        return mask

    def _backdrop(self, image: PCImage, size: tuple[int, int]) -> Image.Image | None:
        """The document as the job last read it, on a canvas of ``size``: the scratch document's
        bottom layer, so the moment PhotoCraft shows it looks like the document."""
        if not image._snapshot:
            return None
        visible = image._snapshot[1]
        if visible.size == size and image.pending_crop is None:
            return visible
        back = Image.new("RGBA", size, (0, 0, 0, 0))
        off = image.pending_crop[2:] if image.pending_crop is not None else (0, 0)
        back.paste(visible, off)
        return back

    def _psd(self, image: PCImage, layers: list[PCLayer], group_name: str, size: tuple[int, int]) -> bytes:
        """``layers`` (top first) with masks, names and visibility, in their group, as a PSD the
        size of the canvas, with the backdrop at the bottom."""
        out = []
        for layer in layers:
            mask, box = self._layer_mask(image, layer, size), None
            if mask is not None:
                box = mask.getbbox() or (0, 0, 1, 1)  # an empty mask: one hidden pixel
                box = (box[0], box[1], box[2] - box[0], box[3] - box[1], mask.crop(box).tobytes())
            o = layer.origin
            out.append(psd.PsdLayer(layer.name, o.x, o.y, o.width, o.height, layer.rgba, layer.visible, box))
        if group_name:
            out = [psd.PsdLayer(group_name, children=out)]
        back = self._backdrop(image, size)
        if back is not None:
            out.append(psd.PsdLayer(BACKDROP, 0, 0, size[0], size[1], back.tobytes()))
        return psd.encode(size[0], size[1], out)

    def _bring_in(
        self, image: PCImage, layers: list[PCLayer], group_name: str, before_copy: Callable[[], None] | None = None
    ) -> list[int]:
        """Copy ``layers`` (in their group) into ``image`` above its selected layer: one
        ``layer.copyToDocument``, one step in its history. The copies' top-level ids.

        One scratch document holds them all (a PSD OpenFill wrote); ``image`` is active again
        right after it opens, and it is closed right after the copy. While an expand's canvas
        change is pending the copies go where they belong on the new canvas (``commit``)."""
        size = image.size()
        scratch, target = self._open_scratch(image, self._psd(image, layers, group_name, size))
        try:
            rows = (self.client.run("layer.list", document=scratch) or {}).get("layers") or []
            tops = [int(r["id"]) for r in rows if not r.get("depth") and r.get("name") != BACKDROP]
            if not tops:
                raise BridgeError("PhotoCraft opened OpenFill's layers without them")
            if before_copy:
                before_copy()
            crop = image.pending_crop
            copied = self.client.run(
                "layer.copyToDocument",
                document=target,
                source=scratch,
                layers=tops,
                offset=[-crop[2], -crop[3]] if crop else [0, 0],
            )
        finally:
            self._close(scratch)
        ids = [int(i) for i in (copied or {}).get("layers") or []]
        if len(ids) != len(tops):
            raise BridgeError("PhotoCraft did not report the copied layer")
        return ids

    def commit(self, image: PCImage, canvas: bool = True) -> None:
        """Put the pending results into the document in one step, on top of the stack, then
        make a pending canvas change (an expand grows the canvas only now, under its layers;
        ``canvas`` False leaves it waiting for the layers still to come)."""
        layers, group_name = image.pending, image.pending_group
        if layers:
            # The results stay pending until they are in the document, so a failure here can be
            # tried again and never loses them.
            self._select_top(image)
            copied = self._bring_in(image, layers, group_name)
            image.pending, image.pending_group = [], ""
            self._take_ids(image, layers, group_name, copied)
            image._snapshot = None
        if canvas and image.pending_crop is not None:
            new_w, new_h, off_x, off_y = image.pending_crop
            self.activate(image)
            self.client.run("image.crop", x=-off_x, y=-off_y, width=new_w, height=new_h)
            image.pending_crop = None
            image._snapshot = None

    def _select_top(self, image: PCImage) -> None:
        self.activate(image)
        top_layer = self._top_layer(image)
        if top_layer is not None:
            self.client.run("layer.select", layer=top_layer)  # not a history step

    def _take_ids(self, image: PCImage, layers: list[PCLayer], group_name: str, copied: list[int]) -> None:
        """Note the ids the copied ``layers`` have in the document; the first is selected."""
        if group_name:
            rows = [row for row in self._flat_layers(image) if row.get("parent") == copied[0]]
            if len(rows) != len(layers):
                raise BridgeError("the copied group does not hold the variations")
            for layer, row in zip(layers, rows, strict=True):
                layer.id, layer.group = int(row["id"]), copied[0]
        elif len(layers) == 1:
            layers[0].id = copied[0]
        else:  # several layers without a group: found by their names
            names = {int(r["id"]): r.get("name") for r in self._flat_layers(image) if int(r["id"]) in copied}
            for layer in layers:
                layer.id = next((i for i, n in names.items() if n == layer.name), None)
                names.pop(layer.id, None)
        self.client.run("layer.select", layer=layers[0].id)

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

    def history(self, image: PCImage) -> list[str]:
        """The document's history (its step labels, oldest first)."""
        self.activate(image)
        return list((self.client.run("document.inspect") or {}).get("history") or [])

    def rewind(self, image: PCImage, state: int) -> None:
        """Take the document back to before history step ``state`` (it and every later step go)."""
        self.activate(image)
        self.client.run("history.deleteState", state=int(state))
        image._snapshot = None

    def collapse(self, image: PCImage, since: int, name: str) -> None:
        """Make history steps ``since``.. one step named ``name``: a snapshot of the document as
        it is, the steps taken back, and the snapshot selected (PhotoCraft adds that as one
        step). The snapshot itself is deleted afterwards (the document does not change)."""
        self.activate(image)
        snap = (self.client.run("history.newSnapshot", name=name, **{"from": "fullDocument"}) or {})["snapshot"]
        self.client.run("history.deleteState", state=int(since))
        try:
            self.client.run("history.selectSnapshot", snapshot=snap)
        except ControlError:
            # Once more; if that fails too the snapshot stays in the History panel, so the
            # result can still be brought back by hand.
            self.client.run("history.selectSnapshot", snapshot=snap)
        self.client.run("history.deleteSnapshot", snapshot=snap)
        image._snapshot = None

    def keep_one(self, image: PCImage, since: int, layers: list[PCLayer], chosen: PCLayer, name: str) -> None:
        """Keep: the job's result step (history step ``since``) becomes ``chosen`` alone, still
        one step. The kept layer is built first, from the job's stored pixels and selection (the
        live selection does not matter), and only then is the job's step taken back, so a
        failure while building leaves the variations as they were. Should the copy fail after
        that, the variations are put back (one step again) before the error is raised."""
        kept = PCLayer(
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
        )
        rewound = []

        def rewind() -> None:
            self.rewind(image, since)
            rewound.append(True)
            self._select_top(image)

        try:
            copied = self._bring_in(image, [kept], "", before_copy=rewind)
        except BaseException:
            if rewound:
                self._restore(image, layers)
            raise
        chosen.id, chosen.group, chosen.visible, chosen.name = copied[0], None, True, name
        self.client.run("layer.select", layer=copied[0])
        image._snapshot = None

    def _restore(self, image: PCImage, layers: list[PCLayer]) -> None:
        """Put variations that were taken back into the document again (one step)."""
        saved = [(layer.id, layer.group, layer.visible) for layer in layers]
        try:
            for layer in layers:
                layer.id, layer.group = None, None
            group = re.sub(r" \(\d+\)$", "", layers[0].name) if len(layers) > 1 else ""
            image.pending, image.pending_group = list(layers), group
            self.commit(image)
        except BaseException:
            image.pending, image.pending_group = [], ""
            for layer, (layer_id, group_id, visible) in zip(layers, saved, strict=True):
                layer.id, layer.group, layer.visible = layer_id, group_id, visible
            raise

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
        """Grow or crop the canvas; ``off_x``/``off_y`` is where the old (0, 0) lands. The
        change waits for the results that fill the new area (``commit``), so PhotoCraft never
        shows the canvas grown and empty."""
        self.commit(image)
        image.pending_crop = (int(new_w), int(new_h), int(off_x), int(off_y))
        image.shift = (image.shift[0] + int(off_x), image.shift[1] + int(off_y))
        # the document is unchanged until then: its snapshot stays (the scratch document's backdrop)
        image.selection = None  # the job's selection was for the old canvas

    def clip_layers_to_canvas(self, image: PCImage) -> None:
        """PhotoCraft's crop already cuts the layers (deleteCroppedPixels)."""

    def new_image_from_pngs(self, images: list[bytes], base_name: str, temp_dir: Path | None) -> PCImage:
        """A new document from the first image; further variations become hidden layers."""
        with Image.open(_io.BytesIO(images[0]), formats=["PNG"]) as im:  # results are PNGs OpenFill made
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
        extras = []
        for number, data in enumerate(images[1:], start=2):
            with Image.open(_io.BytesIO(data), formats=["PNG"]) as im:
                rgba = im.convert("RGBA")
            extras.insert(  # (2) right above the first, the others above it
                0,
                PCLayer(
                    None,
                    f"{base_name} ({number})",
                    Rect(0, 0, rgba.width, rgba.height),
                    rgba.tobytes(),
                    visible=False,
                ),
            )
        if extras:
            if base is not None:
                self.client.run("layer.select", layer=base)
            self._bring_in(image, extras, "")
        if base is not None:
            self.client.run("layer.select", layer=base)
        return image

    def finish(self, image: PCImage) -> None:
        """End of a job: the results go into the document (a failed job leaves it untouched)."""
        try:
            try:
                self.commit(image)
            except ControlError:
                self.commit(image)  # still pending: once more (a click may have got in the way)
        finally:
            image.selection = None
            image._snapshot = None

    def discard(self, image: PCImage) -> None:
        """A failed or cancelled job: forget results that never reached the document."""
        image.pending, image.pending_group = [], ""
        image.pending_crop = None  # the canvas stays as it was
        image.selection = None
        image._snapshot = None


def _grow(mask: Image.Image, radius: int) -> Image.Image:
    """``mask`` grown by ``radius`` pixels, round as Select > Modify > Expand grows it (square
    and cross steps in turn: an octagon), worked only around what is selected."""
    box = mask.getbbox()
    if box is None or radius <= 0:
        return mask
    w, h = mask.size
    area = (max(0, box[0] - radius), max(0, box[1] - radius), min(w, box[2] + radius), min(h, box[3] + radius))
    part = mask.crop(area)
    for step in range(radius):
        if step % 2 == 0:
            part = part.filter(ImageFilter.MaxFilter(3))
        else:
            pw, ph = part.size
            grown = part
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                shifted = Image.new("L", part.size, 0)
                shifted.paste(
                    part.crop((max(0, -dx), max(0, -dy), pw - max(0, dx), ph - max(0, dy))), (max(0, dx), max(0, dy))
                )
                grown = ImageChops.lighter(grown, shifted)
            part = grown
    out = mask.copy()
    out.paste(part, area[:2])
    return out


def _tiff_channel(path: Path, channel: object) -> Image.Image | None:
    """Alpha channel ``channel`` (its index among the document's alpha channels) of the flat,
    uncompressed 8-bit TIFF PhotoCraft saved at ``path``: the samples after red, green and
    blue. None when the file is not that."""
    for _ in range(50):  # the save returns when written; allow a slow disk a moment
        if path.is_file():
            break
        time.sleep(0.05)
    try:
        with Image.open(path, formats=["TIFF"]) as im:
            tags, (width, height) = im.tag_v2, im.size
            samples = int(tags.get(277, 1))
            bits = tags.get(258) or (8,)
            if tags.get(259, 1) != 1 or tags.get(284, 1) != 1 or any(int(b) != 8 for b in bits) or samples < 4:
                return None
            offsets, counts = tags.get(273) or (), tags.get(279) or ()
        with open(path, "rb") as f:
            data = bytearray()
            for offset, count in zip(offsets, counts, strict=True):
                f.seek(int(offset))
                data += f.read(int(count))
    except (OSError, ValueError, TypeError, SyntaxError):
        return None
    if len(data) != width * height * samples:
        return None
    index = channel if isinstance(channel, int) and 0 <= channel < samples - 3 else samples - 4
    return Image.frombytes("L", (width, height), bytes(data[3 + index :: samples]))


def default_profile() -> Path:
    """Where the companion keeps settings, logs and the exchange folder."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "OpenFill for PhotoCraft"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenFill for PhotoCraft"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openfill-photocraft"
