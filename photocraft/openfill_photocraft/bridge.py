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

OpenFill's own documents have unique names and are found by them before every save and
close: the user may click another tab at any moment, and "the active document" is never
taken to be OpenFill's.
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
    path: str | None = None  # the document's file, when PhotoCraft says (tells same-named tabs apart)
    known_ids: set[int] = field(default_factory=set)  # layer ids seen in it: its identity among same-named tabs
    # What this job has put into the document so far (a multi-round expand commits round 1 before
    # round 2 runs): how many history steps, and the history right after the last one. A failed
    # or cancelled job takes them back (``discard``) if nothing else happened since.
    steps_added: int = 0
    history_after: list[str] | None = None
    revision_after: int | None = None  # the document's revision then (any edit, undo or redo changes it)
    rollback_ok: bool = True  # no step of the user's came between the job's

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
        self._left_open: set[str] = set()  # OpenFill's documents PhotoCraft could not close yet
        self._actions: bool | None = None  # whether PhotoCraft plays actions (see _run_at)
        self._selection_colours: Image.Image | None = None  # the colours the selection's TIFF held
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
                return PCImage(self, str(d.get("name")), int(d["index"]), path=d.get("path"))
        return None

    def _layer_ids(self, index: int) -> set[int]:
        try:
            rows = (self.client.run("layer.list", document=int(index), flat=True) or {}).get("layers") or []
        except ControlError:
            return set()
        return {int(r["id"]) for r in rows if r.get("id") is not None}

    def _find(self, image: PCImage, session: dict | None = None) -> dict:
        """``image``'s tab. PhotoCraft has no document ids and tabs move when one closes, so a
        tab is the job's when its name (and file, when known) match; when several tabs match,
        the one holding the layers seen in the job's document is it."""
        docs = (session or self.session()).get("documents") or []
        same = [d for d in docs if d.get("name") == image.name]
        if image.path:
            same = [d for d in same if d.get("path") == image.path] or same
        if not same:
            raise BridgeError(f"the document '{image.name}' is no longer open in PhotoCraft")
        found = same[0]
        if len(same) > 1:
            overlap = [(len(self._layer_ids(d["index"]) & image.known_ids), d) for d in same]
            best = max(n for n, _d in overlap)
            if best:
                found = next(d for n, d in overlap if n == best)
            else:  # nothing to tell them apart by: where it was
                found = next((d for d in same if d.get("index") == image.index), same[0])
        image.index = int(found["index"])
        return found

    def _own(self, name: str, session: dict | None = None) -> int | None:
        """The tab of OpenFill's own document ``name`` (its names are unique), None when it is
        gone. Two tabs with that name (a user document named like it): BridgeError, so OpenFill
        never saves, reads or closes one of them by mistake."""
        found = [int(d["index"]) for d in (session or self.session()).get("documents") or [] if d.get("name") == name]
        if len(found) > 1:
            raise BridgeError(f"two documents are named '{name}': OpenFill leaves them alone")
        return found[0] if found else None

    def _index_of(self, name: str) -> int | None:
        return self._own(name)

    def _make_active(self, name: str) -> int:
        """Make OpenFill's own document ``name`` the active one (the user may have clicked
        another tab) and check that it is: its index. Nothing is saved or read otherwise."""
        for _ in range(3):
            session = self.session()
            index = self._own(name, session)
            if index is None:
                raise BridgeError(f"OpenFill's document '{name}' is gone")
            if session.get("active") == index:
                return index
            try:
                self.client.run("document.activate", document=index)
            except ControlError:
                pass
        raise BridgeError(f"PhotoCraft would not show OpenFill's document '{name}'")

    def _close_own(self, name: str) -> None:
        """Close OpenFill's own document ``name``, found by its name at that moment, never
        whatever happens to be active, and only when exactly one tab has that name. When
        PhotoCraft can't close it now (a dialog is open), it is closed at the next chance."""
        try:
            index = self._own(name)
        except BridgeError:
            self._left_open.add(name)
            return
        if index is None:
            self._left_open.discard(name)
            return
        try:
            self.client.run("file.close", document=index)
            self._left_open.discard(name)
        except ControlError:
            if name not in self._left_open:
                self.notify(
                    f"OpenFill could not close its temporary document '{name}' (a dialog was open). "
                    "It closes it with the next job, or close it yourself: it holds nothing of yours."
                )
            self._left_open.add(name)

    def _close_left_open(self) -> None:
        for name in sorted(self._left_open):
            self._close_own(name)

    def _job_document_active(self, image: PCImage) -> tuple[dict, int]:
        """Make the job's document the active one and check that it is: (that session, its
        index). BridgeError when another document stays in front."""
        for _ in range(3):
            session = self.session()
            index = int(self._find(image, session)["index"])
            if session.get("active") == index:
                return session, index
            try:
                self.client.run("document.activate", document=index)
            except ControlError:
                pass
        raise BridgeError(f"another image stayed in front of '{image.name}' in PhotoCraft")

    def _states(self) -> dict[tuple, tuple[int, int]]:
        """Every open document's (revision, number of history states before the redo ones),
        by (tab index, name)."""
        out = {}
        for d in self.documents():
            try:
                n = len((self.client.run("document.inspect", document=int(d["index"])) or {}).get("history") or [])
            except ControlError:
                n = -1
            out[(d.get("index"), d.get("name"))] = (d.get("revision"), n)
        return out

    def _run_at(self, index: int, command: str, atomic: bool = True, **params) -> dict:
        """Run ``command`` (one that acts on the document in front) on the document at tab
        ``index``, in one go: a throwaway action holding "show that document" and the command
        is played in a single request, so no tab click can come between them. The action is
        deleted right after. Without actions (or while the user records one): show, check, run;
        a click in that moment can still land it elsewhere, which ``_on_document`` notices."""
        if atomic and self._actions is not False:
            try:
                listed = self.client.run("actions.list") or {}
                if listed.get("recording"):
                    raise BridgeError("the user is recording an action")  # theirs: not ours to touch
                taken = {a.get("name") for a in listed.get("actions") or []}
                label = {"image.crop": "Crop", "history.selectState": "History"}.get(command, command)
                name = f"OpenFill {label}"  # the history shows "Action: OpenFill Crop"
                while name in taken:
                    name = f"OpenFill {label} {next(self._names)}"
                self.client.run("actions.record", name=name)
                stopped = self.client.run("actions.stop") or {}
            except ControlError:
                self._actions = False
            except BridgeError:
                pass
            else:
                try:
                    for step in reversed(range(int(stopped.get("steps") or 0))):
                        # what the user did in that moment (it happened already): never replayed
                        self.client.run("actions.delete", action=name, step=step)
                    self.client.run("actions.insert", action=name, id="document.activate", params={"document": index})
                    self.client.run("actions.insert", action=name, id=command, params=params)
                    played = self.client.run("actions.play", action=name) or {}
                finally:
                    try:
                        self.client.run("actions.delete", action=name)
                    except ControlError:
                        pass
                self._actions = True
                failed = played.get("failed")
                if failed:
                    raise ControlError(f"{command}: {failed.get('error')}")
                return played
        self.client.run("document.activate", document=index)
        if self.session().get("active") != index:
            raise BridgeError("another image came to the front")
        return self.client.run(command, **params) or {}

    def _put_back(self, key: tuple, states: int) -> bool:
        """Take the user's document ``key`` back to the history state it was at (``states``
        states before the redo ones) after an OpenFill command landed on it. Only ever moves
        through its history (``history.selectState``): nothing of it is deleted."""
        for _ in range(3):
            docs = self.documents()
            index = next((int(d["index"]) for d in docs if (d.get("index"), d.get("name")) == key), None)
            if index is None or states < 1:
                return False
            try:
                self._run_at(index, "history.selectState", state=states - 1)
                now = (self.client.run("document.inspect", document=index) or {}).get("history") or []
                if len(now) == states:
                    return True
            except (ControlError, BridgeError):
                pass
        return False

    def _on_document(self, image: PCImage, command: str, edits: bool = True, atomic: bool = True, **params) -> dict:
        """Run ``command``, which acts on whatever document is in front, on the job's document
        only (``_run_at``: in one go where PhotoCraft has actions). Afterwards every document's
        revision is compared: if it still landed elsewhere, that document is put back where it
        was in its history (a move, never a deletion: Edit > Redo would still reach anything)
        and the command is tried again. ``edits`` False: the command changes no revision
        (snapshots), so only the tab in front before and after can be checked."""
        for _ in range(5):
            before = self._states() if edits else {}
            session = self.session()
            index = int(self._find(image, session)["index"])
            job = (index, image.name)
            if edits and job not in before:
                continue
            try:
                out = self._run_at(index, command, atomic=atomic, **params)
            except BridgeError:
                continue
            after = self.session()
            if not edits:
                if after.get("active") == index:
                    return out
                raise BridgeError(
                    f"another image came to the front while OpenFill changed '{image.name}', so it stopped"
                )
            revisions = {(d.get("index"), d.get("name")): d.get("revision") for d in after.get("documents") or []}
            if revisions.get(job) != before[job][0]:
                return out  # it changed the job's document (anything else that changed is the user's own)
            landed = [key for key, rev in revisions.items() if key != job and key in before and rev != before[key][0]]
            if not landed:
                return out  # it changed nothing (already so)
            for key in landed:
                if not self._put_back(key, before[key][1]):
                    raise BridgeError(
                        f"OpenFill's change for '{image.name}' landed on '{key[1]}' when another tab came to "
                        "the front. Edit > Undo or Redo there takes it back."
                    )
        raise BridgeError(f"another image kept coming to the front while OpenFill changed '{image.name}'")

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

    def _open_scratch(self, image: PCImage, rel: str) -> tuple[str, int, int]:
        """Open the PSD ``rel`` (in the exchange folder) as a scratch document and make ``image`` the active one again
        at once (``app.open`` shows what it opens): (the scratch document's name and index,
        ``image``'s index). Its file name is unique, so it is found by name."""
        try:
            back = int(self._find(image)["index"])
        except BaseException:
            self._forget(rel)
            raise
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
        scratch = [int(d["index"]) for d in docs if d.get("name") == name]
        if not scratch:
            raise BridgeError("PhotoCraft did not open OpenFill's layers")
        target = int(self._find(image, session)["index"])
        if session.get("active") != target:
            self.client.run("document.activate", document=target)
        return name, scratch[-1], target

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
        self._close_left_open()
        revision = self._revision(image)
        if image._snapshot and image._snapshot[0] == revision:
            return image._snapshot[1], image._snapshot[2]
        self.activate(image)
        back = int(self._find(image)["index"])
        d = self.client.run("document.inspect")
        bounds = d.get("selectionBounds") if d.get("hasSelection") else None
        eight_bit = int(d.get("depth") or 8) == 8  # the TIFF reads below are 8-bit only
        view = self._view()
        # OpenFill's documents get names no user document has: they are found by name, never
        # taken to be "the active one" (the user may click another tab at any moment).
        copy = f"OpenFill export {os.getpid()}-{next(self._names)}"
        self.client.run("image.duplicate", name=copy, mergedOnly=True)
        opened = [copy]
        selection = None
        try:
            self._make_active(copy)
            self._show_like(view)
            visible = self._visible(copy, eight_bit)
            if d.get("hasSelection"):
                self._selection_colours = None
                selection = self._selection_tiff(copy, bounds) if eight_bit else None
                if selection is not None and not _same_colours(visible, self._selection_colours):
                    # The selection's file is the copy's (it holds the copy's own channel); the
                    # picture read before it is not: another tab was in front then. Once more.
                    visible = self._visible(copy, eight_bit)
                    if not _same_colours(visible, self._selection_colours):
                        raise BridgeError("another image came to the front while OpenFill read yours; try again")
                if selection is None:
                    selection = self._selection_document(image, copy, opened)
        finally:
            try:
                self.client.run("document.activate", document=back)  # first, then close the copies
            except ControlError:
                pass
            for name in reversed(opened):
                self._close_own(name)
            self.activate(image)
        image._snapshot = (revision, visible, selection)
        return visible, selection

    def _save_own(self, name: str, rel: str, **options) -> None:
        """Save OpenFill's own document ``name`` (app.save saves the active one): checked to be
        the active one before, and again after, so a file never holds the user's document."""
        for _ in range(3):
            index = self._make_active(name)
            size = next(((d.get("width"), d.get("height")) for d in self.documents() if d.get("index") == index), None)
            self.client.call("app.save", path=rel, **options)
            known = size if size and None not in size else None  # PhotoCraft 0.16 on reports sizes
            if self.session().get("active") == self._own(name) and (
                known is None or self._saved_size(rel) in (known, None)
            ):
                return
            self._forget(rel)  # another document came to the front meanwhile: once more
        raise BridgeError("PhotoCraft kept showing another document while OpenFill read the image")

    def _saved_size(self, rel: str) -> tuple[int, int] | None:
        """The size of the picture saved at ``rel`` (None if it can't be read: then the reader
        reports it)."""
        path = self.exchange / rel
        for _ in range(50):
            if path.is_file():
                break
            time.sleep(0.05)
        try:
            with Image.open(path) as im:
                return im.size
        except (OSError, ValueError, SyntaxError):
            return None

    def _visible(self, copy: str, eight_bit: bool = True) -> Image.Image:
        """The duplicate's pixels (RGBA). From PhotoCraft 0.18 on, for 8-bit documents, an
        uncompressed TIFF, which saves several times faster than a PNG (the duplicate shows for
        less time)."""
        if eight_bit and self.photocraft_version() >= TIFF_CHANNELS_FROM:
            rel = self._file("visible", ".tif")
            try:
                self._save_own(copy, rel, tiffCompression="none", tiffTransparency=True)
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
        self._save_own(copy, rel)
        return self._read(rel).convert("RGBA")

    def _selection_tiff(self, copy: str, bounds) -> Image.Image | None:
        """The selection of the active duplicate (it keeps the document's), saved into it as an
        alpha channel and read from a flat TIFF that carries that channel (PhotoCraft 0.18 on).
        None when this PhotoCraft can't, or the file does not hold what it should."""
        if self.photocraft_version() < TIFF_CHANNELS_FROM:
            return None
        rel = self._file("selection", ".tif")
        try:
            duplicate = self._make_active(copy)
            saved = self.client.run("select.saveSelection", name="OpenFill selection", document=duplicate) or {}
            self._save_own(copy, rel, tiffCompression="none", tiffTransparency=False)
            read = _tiff_channel(self.exchange / rel, saved.get("channel"))
        except ControlError:
            read = None
        finally:
            self._forget(rel)
        if read is None or not bounds:
            return None
        selection, self._selection_colours = read
        x, y, w, h = (int(v) for v in bounds)
        if selection.getbbox() != (x, y, x + w, y + h):
            return None  # not the selection: read it the long way
        return selection

    def _selection_document(self, image: PCImage, copy: str, opened: list[str]) -> Image.Image:
        """The selection the way every PhotoCraft can: saved into the duplicate, copied into a
        document of its own (black and white) and saved from there."""
        self.activate(image)
        self.client.run("select.saveSelection", name="OpenFill selection", document=self._index_of(copy))
        self._make_active(copy)
        mask = f"OpenFill mask {os.getpid()}-{next(self._names)}"
        before = [d.get("name") for d in self.documents()]
        self.client.run("channel.duplicate", channel="OpenFill selection", document="new", name=mask)
        names = [d.get("name") for d in self.documents()]
        if mask not in names:  # a PhotoCraft that names the new document itself: the new name
            new = [n for n in set(names) if names.count(n) > before.count(n)]
            if len(new) != 1 or names.count(new[0]) != 1:
                raise BridgeError("PhotoCraft did not make OpenFill's mask document")
            mask = str(new[0])
        opened.append(mask)
        rel = self._file("mask")
        self._save_own(mask, rel)
        return self._read(rel).convert("L")

    def export_region(
        self, image: PCImage, rect: Rect, transparent_outside: bool = False, keep_alpha: bool = False
    ) -> tuple[bytes, bytes]:
        """(opaque RGBA PNG, raw RGBA) of ``rect``; outside the canvas is transparent before
        flattening over the mean colour, as GIMP does for an expand."""
        full, _selection = self._snapshot(image)
        if self.on_region:  # where this round's preview frames belong, in the job's first canvas
            self.on_region((rect.x - image.shift[0], rect.y - image.shift[1], rect.width, rect.height))
        crop = full.crop((rect.x, rect.y, rect.x + rect.width, rect.y + rect.height))
        flat = crop.tobytes() if keep_alpha else flatten_over_mean(crop.tobytes())
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

    def _write_psd(self, image: PCImage, layers: list[PCLayer], group_name: str, size: tuple[int, int]) -> str:
        """``layers`` (top first) with masks, names and visibility, in their group, as a PSD the
        size of the canvas, with the backdrop at the bottom, written into the exchange folder:
        its name there. Variations sharing a mask (the job's selection, grown and feathered)
        get it made once."""
        masks: dict[tuple, tuple | None] = {}
        out = []
        for layer in layers:
            key = (layer.mask_kind, layer.grow, layer.feather, id(layer.mask), id(layer.selection))
            if key not in masks:
                mask, box = self._layer_mask(image, layer, size), None
                if mask is not None:
                    box = mask.getbbox() or (0, 0, 1, 1)  # an empty mask: one hidden pixel
                    box = (box[0], box[1], box[2] - box[0], box[3] - box[1], mask.crop(box).tobytes())
                masks[key] = box
            o = layer.origin
            out.append(psd.PsdLayer(layer.name, o.x, o.y, o.width, o.height, layer.rgba, layer.visible, masks[key]))
        if group_name:
            out = [psd.PsdLayer(group_name, children=out)]
        back = self._backdrop(image, size)
        if back is not None:
            out.append(psd.PsdLayer(BACKDROP, 0, 0, size[0], size[1], back.tobytes()))
        rel = self._file("layers", ".psd")
        try:
            with open(self.exchange / rel, "wb") as f:
                psd.write(f, size[0], size[1], out)
        except BaseException:
            self._forget(rel)
            raise
        return rel

    def _bring_in(
        self, image: PCImage, layers: list[PCLayer], group_name: str, before_copy: Callable[[], None] | None = None
    ) -> list[int]:
        """Copy ``layers`` (in their group) into ``image`` above its selected layer: one
        ``layer.copyToDocument``, one step in its history. The copies' top-level ids.

        One scratch document holds them all (a PSD OpenFill wrote); ``image`` is active again
        right after it opens, and it is closed right after the copy. While an expand's canvas
        change is pending the copies go where they belong on the new canvas (``commit``)."""
        size = image.size()
        name, scratch, target = self._open_scratch(image, self._write_psd(image, layers, group_name, size))
        try:
            rows = (self.client.run("layer.list", document=scratch) or {}).get("layers") or []
            tops = [int(r["id"]) for r in rows if not r.get("depth") and r.get("name") != BACKDROP]
            if not tops:
                raise BridgeError("PhotoCraft opened OpenFill's layers without them")
            if before_copy:
                before_copy()
            # Both found again right before the copy: a tab the user closed meanwhile moves them.
            session = self.session()
            target = int(self._find(image, session)["index"])
            scratch = next((int(d["index"]) for d in session.get("documents") or [] if d.get("name") == name), None)
            if scratch is None:
                raise BridgeError("OpenFill's layers were closed in PhotoCraft")
            crop = image.pending_crop
            copied = self.client.run(
                "layer.copyToDocument",
                document=target,
                source=scratch,
                layers=tops,
                offset=[-crop[2], -crop[3]] if crop else [0, 0],
            )
        finally:
            self._close_own(name)
        ids = [int(i) for i in (copied or {}).get("layers") or []]
        if len(ids) != len(tops):
            raise BridgeError("PhotoCraft did not report the copied layer")
        image.known_ids.update(ids)
        return ids

    def commit(self, image: PCImage, canvas: bool = True) -> None:
        """Put the pending results into the document in one step, on top of the stack, then
        make a pending canvas change (an expand grows the canvas only now, under its layers;
        ``canvas`` False leaves it waiting for the layers still to come)."""
        layers, group_name = image.pending, image.pending_group
        if not layers and not (canvas and image.pending_crop is not None):
            return
        # Each of the job's steps is checked to be exactly one more state on top of the last
        # (a step of the user's in between would be taken back with them): only then may a
        # failed job take them back (``discard``).
        history = self._history_of(image)
        if image.steps_added == 0:
            image.rollback_ok = True
        elif history != image.history_after:
            image.rollback_ok = False
        if layers:
            # The results stay pending until they are in the document, so a failure here can be
            # tried again and never loses them.
            reopen = self._select_top(image)
            try:
                copied = self._bring_in(image, layers, group_name)
            finally:
                reopen()
            image.steps_added += 1
            history = self._one_more_state(image, history)
            image.pending, image.pending_group = [], ""
            self._take_ids(image, layers, group_name, copied)
            image._snapshot = None
        if canvas and image.pending_crop is not None:
            new_w, new_h, off_x, off_y = image.pending_crop
            self._on_document(image, "image.crop", x=-off_x, y=-off_y, width=new_w, height=new_h)
            image.steps_added += 1
            history = self._one_more_state(image, history)
            image.pending_crop = None
            image._snapshot = None
        image.history_after = history
        image.revision_after = self._revision(image)

    def _history_of(self, image: PCImage) -> list[str]:
        """The job's document's history (states before the redo ones), without showing it."""
        index = int(self._find(image)["index"])
        return list((self.client.run("document.inspect", document=index) or {}).get("history") or [])

    def _one_more_state(self, image: PCImage, before: list[str]) -> list[str]:
        """The history after one of the job's steps; rollback stays allowed only when that
        step is exactly one more state on top of ``before``."""
        after = self._history_of(image)
        grew = len(after) == len(before) + 1 and after[: len(before)] == before
        at_limit = len(after) == len(before) and after[: len(before) - 1] == before[1:]  # the oldest dropped
        if not (grew or at_limit):
            image.rollback_ok = False
        return after

    def _select_top(self, image: PCImage) -> Callable[[], None]:
        """Make the next copy land on top of the stack, outside any group: select the topmost
        layer. A copy above a selected open group goes into it, so a group on top is closed
        for the copy (a view setting, not a history step); the returned function opens it again."""
        self.activate(image)
        rows = [row for row in self._flat_layers(image) if not row.get("depth")]
        if not rows:
            return lambda: None
        top = int(rows[0]["id"])
        self.client.run("layer.select", layer=top)  # not a history step
        if str(rows[0].get("kind") or "").lower() != "group":
            return lambda: None
        tree = (self.client.run("document.inspect") or {}).get("layers") or []
        expanded = next((row.get("expanded") for row in tree if row.get("id") == top), None)
        if expanded is False:
            return lambda: None
        try:
            self.client.run("layer.setExpanded", layer=top, expanded=False)
        except ControlError:  # a PhotoCraft without it: the copy goes into the group, as before
            return lambda: None

        def reopen() -> None:
            try:
                self.client.run("layer.setExpanded", layer=top, expanded=True)
            except ControlError:
                pass

        return reopen

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
        if not image.known_ids and image.index is not None:
            image.known_ids = self._layer_ids(image.index)
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
        """Take the document back to before history step ``state``: that state and the later
        ones become redo states (``history.selectState``), so nothing is ever deleted, even if
        another tab came to the front at that moment."""
        self._on_document(image, "history.selectState", state=max(0, int(state) - 1))
        image._snapshot = None

    def collapse(self, image: PCImage, since: int, name: str) -> None:
        """Make history steps ``since``.. one step named ``name``: a snapshot of the document as
        it is, the steps taken back (to redo states), and the snapshot selected (PhotoCraft adds
        that as one step, which drops those redo states). The snapshot itself is deleted
        afterwards (the document does not change). Each on the job's document only."""
        snap = self._new_snapshot(image, name)
        self._on_document(image, "history.selectState", state=max(0, int(since) - 1))
        try:
            # Not in an action: PhotoCraft 0.20 would name the step "Action: ..." instead of the job.
            self._on_document(image, "history.selectSnapshot", atomic=False, snapshot=snap)
        except ControlError:
            # Once more; if that fails too the snapshot stays in the History panel, so the
            # result can still be brought back by hand (or with Edit > Redo).
            self._on_document(image, "history.selectSnapshot", atomic=False, snapshot=snap)
        self._on_document(image, "history.deleteSnapshot", edits=False, snapshot=snap)
        image._snapshot = None

    def _new_snapshot(self, image: PCImage, name: str) -> int:
        """A snapshot of the job's document named ``name``: its id (the newest of that name)."""
        out = self._on_document(image, "history.newSnapshot", edits=False, name=name, **{"from": "fullDocument"})
        if isinstance(out, dict) and "snapshot" in out:
            return int(out["snapshot"])
        index = int(self._find(image)["index"])
        snaps = (self.client.run("document.inspect", document=index) or {}).get("snapshots") or []
        ids = [int(sn["id"]) for sn in snaps if sn.get("name") == name]
        if not ids:
            raise BridgeError("PhotoCraft did not make OpenFill's snapshot")
        return max(ids)

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
        rewound, reopen = [], []

        def rewind() -> None:
            self.rewind(image, since)
            rewound.append(True)
            reopen.append(self._select_top(image))

        try:
            copied = self._bring_in(image, [kept], "", before_copy=rewind)
        except BaseException:
            if rewound:
                self._restore(image, layers)
            raise
        finally:
            for again in reopen:
                again()
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
        name = str(opened.get("name") or rel)
        new = [d for d in self.documents() if d.get("name") == name]  # opened last: the last of that name
        if not new:
            raise BridgeError("PhotoCraft did not open the new image")
        image = self.track(PCImage(self, name, int(new[-1]["index"]), path=new[-1].get("path")))
        self.activate(image)
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
            image.steps_added, image.history_after = 0, None  # the job's steps are its result now
            image.revision_after, image.rollback_ok = None, True

    def discard(self, image: PCImage) -> str:
        """A failed or cancelled job: forget results that never reached the document, and take
        back what earlier rounds of it put in (a multi-round expand), but only when the
        document is exactly as the job left it (its history and its revision, which any edit,
        undo or redo changes) and it is the one in front. Taking back moves the history to
        before the job (redo states), it never deletes a state.

        "" nothing of the job was in the document, "undone" it was taken back, "kept" it is
        still in the document (Edit > Undo takes it back)."""
        image.pending, image.pending_group = [], ""
        image.pending_crop = None  # the canvas stays as it was
        image.selection = None
        image._snapshot = None
        steps, expected, revision = image.steps_added, image.history_after, image.revision_after
        safe = image.rollback_ok
        image.steps_added, image.history_after, image.revision_after, image.rollback_ok = 0, None, None, True
        if not (steps and expected):
            return ""
        try:
            session = self.session()
            d = self._find(image, session)
            index = int(d["index"])
            if (
                safe
                and revision is not None
                and d.get("revision") == revision
                and len(expected) > steps
                and list((self.client.run("document.inspect", document=index) or {}).get("history") or []) == expected
            ):
                # One request shows the job's document and moves its history back (_run_at),
                # so the move can't land on another tab.
                self.rewind(image, len(expected) - steps)
                return "undone"
        except (ControlError, BridgeError):
            pass
        return "kept"


def _octagon(img: Image.Image, reach: float) -> Image.Image:
    """``img`` dilated by a regular octagon of radius ``reach`` (a square, then a diamond: the
    octagon is their sum), each built by doubling, so a radius r takes about log r passes.
    ``img`` needs a zero margin of ``reach`` + 1 all round (the shifts wrap)."""
    a = round(reach * 0.41421356)
    b = max(0, round(reach) - a)

    def spread(im: Image.Image, shifts: tuple) -> Image.Image:
        out = im
        for dx, dy in shifts:
            out = ImageChops.lighter(out, ImageChops.offset(im, dx, dy))
        return out

    for ax, ay in ((1, 0), (0, 1)):  # a line of 2k + 1 pixels and its copies s away: 2(k + s) + 1
        k = 0
        while k < a:
            step = min(2 * k + 1, a - k)
            img = spread(img, ((step * ax, step * ay), (-step * ax, -step * ay)))
            k += step
    k = 0
    while k < b:  # a diamond of radius k and its copies s <= k away along the axes: radius k + s
        step = 1 if k == 0 else min(k, b - k)
        img = spread(img, ((step, 0), (-step, 0), (0, step), (0, -step)))
        k += step
    return img


def _grow(mask: Image.Image, radius: int) -> Image.Image:
    """``mask`` grown by ``radius`` pixels, round as Select > Modify > Expand grows it (here an
    octagon), worked only around what is selected. Large radii are grown at 1/f size, f =
    radius // 12, and scaled back (OpenFill's grow is at most 48 px and always feathered
    after, so this costs a pixel or two of the edge): 6000 x 4000 at 48 px in about half a second."""
    box = mask.getbbox()
    if box is None or radius <= 0:
        return mask
    w, h = mask.size
    area = (max(0, box[0] - radius), max(0, box[1] - radius), min(w, box[2] + radius), min(h, box[3] + radius))
    part = mask.crop(area)
    f = max(1, radius // 12)
    sw, sh = -(-part.width // f), -(-part.height // f)
    small = part.resize((sw, sh), Image.BOX) if f > 1 else part
    m = -(-radius // f) + 2
    padded = Image.new("L", (sw + 2 * m, sh + 2 * m), 0)
    padded.paste(small, (m, m))
    grown = _octagon(padded, radius / f).crop((m, m, m + sw, m + sh))
    if f > 1:
        grown = grown.resize((sw * f, sh * f), Image.BILINEAR).crop((0, 0, part.width, part.height))
        grown = ImageChops.lighter(part, grown)
    out = mask.copy()
    out.paste(grown, area[:2])
    return out


def _tiff_channel(path: Path, channel: object) -> tuple[Image.Image, Image.Image] | None:
    """Alpha channel ``channel`` (its index among the document's alpha channels) of the flat,
    uncompressed 8-bit TIFF PhotoCraft saved at ``path`` (the samples after red, green and
    blue), and the colours. None when the file is not that."""
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
    colours = Image.merge("RGB", [Image.frombytes("L", (width, height), bytes(data[k::samples])) for k in range(3)])
    return Image.frombytes("L", (width, height), bytes(data[3 + index :: samples])), colours


def _same_colours(visible: Image.Image, colours: Image.Image | None) -> bool:
    """Whether ``visible`` (RGBA) shows the colours ``colours`` (RGB) where it is opaque, on a
    grid of samples: the two reads were of the same document. True when there is nothing to
    compare."""
    if colours is None:
        return True
    if visible.size != colours.size:
        return False
    w, h = visible.size
    step_x, step_y = max(1, w // 64), max(1, h // 64)
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            r, g, b, a = visible.getpixel((x, y))
            if a == 255 and max(abs(r - c) for r, c in zip((r, g, b), colours.getpixel((x, y)), strict=True)) > 2:
                return False
    return True


def default_profile() -> Path:
    """Where the companion keeps settings, logs and the exchange folder."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "OpenFill for PhotoCraft"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenFill for PhotoCraft"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openfill-photocraft"
