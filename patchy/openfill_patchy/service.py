# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""The companion's engine for Patchy: the PhotoCraft companion's ``Service`` (one job at a
time, the variations picker, ComfyUI set-up), with Keep and Discard done the Patchy way,
and ``Watcher``, which keeps the panel's facts current from Patchy's pushed events
instead of polling.

A result is one step in Patchy's history (``bridge``), so Discard is one Undo and Keep is
one run that undoes it and adds the chosen variation alone. Both happen only while the
history is still where the job left it (Patchy's ``historyStateId``); after any other
edit they delete layers instead, as a step of their own, so the artist's work is never
undone.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openfill_photocraft.menu import DocInfo
from openfill_photocraft.service import Service

from .bridge import PatchyBridge, PatchyImage
from .client import COMPANION_ID, PatchyClient, PatchyError, WorkspaceLost
from .focus import FocusReturn

# What Plugins > Companions may ask of a running OpenFill: "show" the panel (or a mode of it),
# or "quit". Anything else is ignored.
PANEL_ACTIONS = ("show", "panel", "fill", "remove", "expand", "txt2img")


class PatchyService(Service):
    """``Service`` against Patchy (``bridge.PatchyBridge``)."""

    host = "Patchy"

    def __init__(self, client: PatchyClient, bridge: PatchyBridge) -> None:
        super().__init__(client, bridge)  # type: ignore[arg-type] - the client is only kept
        self.image: PatchyImage | None = None  # type: ignore[assignment]
        self.focus: FocusReturn | None = None  # set by the launcher when there is a panel

    def _find_comfy(self) -> None:
        """First start: the ComfyUI OpenFill for PhotoCraft or for GIMP uses, or one found here."""
        from openfill.config import comfy_layout

        if comfy_layout(self.app.settings.comfy_dir) is None:
            for folder in photocraft_comfy_dirs():
                if comfy_layout(folder) is not None:
                    self.app.use_comfy_dir(folder)
                    self.app.log.info("using the ComfyUI OpenFill for PhotoCraft uses: %s", folder)
                    self.rescan_models()
                    return
        super()._find_comfy()

    def photocraft(self) -> dict[str, Any]:
        """The active document and its selection (the page's ``photocraft`` field)."""
        if self.watcher is not None:
            return self.watcher.snapshot()
        return {"connected": False, "error": "not watching Patchy"}

    def _one_step(self, image: PatchyImage, base: Any) -> None:  # type: ignore[override]
        """The job's result is one step already (the bridge counts OpenFill's steps)."""

    def _unchanged_since_job(self) -> bool:
        return self.image is not None and self.bridge.unchanged(self.image)

    def start(self, kind: str, params: dict[str, Any]) -> None:
        """Generate (and Remove's immediate run from the menu): the job starts, and Patchy
        gets the keyboard back."""
        super().start(kind, params)
        self._hand_back()

    def _hand_back(self) -> None:
        if self.focus is not None:
            self.focus.request()

    def show_variation(self, index: int) -> None:
        if not self.image or not (0 <= index < len(self.layers)):
            raise RuntimeError("No variations to pick from.")
        self.bridge.show_variation(self.image, self.layers, index)
        with self.state.lock:
            self.state.shown = index

    def keep(self, which: str) -> None:
        """'selected': only the shown variation stays; 'all': keep them (hidden); 'none':
        take the result back."""
        if not self.image or not self.layers:
            return
        self._hand_back()  # the click was in the panel; Patchy gets the keyboard back
        image, shown = self.image, self.state.shown
        if which == "all":
            pass
        elif self._unchanged_since_job():
            if which == "none":
                self.bridge.rewind(image)  # as if the job had not run: no step
                self.layers = []
            else:
                chosen = self.layers[shown]
                self.bridge.keep_one(image, chosen, re.sub(r" \(\d+\)$", "", chosen.name))
                self.layers = [chosen]
        else:
            group = self.layers[0].group
            if which == "none" and group is not None and all(layer.group == group for layer in self.layers):
                doomed = [group]  # the variations' group, in one step
            else:
                doomed = [
                    layer.id
                    for i, layer in enumerate(self.layers)
                    if layer.id is not None and (which == "none" or i != shown)
                ]
            self.bridge.remove_layers(image, [str(i) for i in doomed])
            self.layers = [] if which == "none" else [self.layers[shown]]
        with self.state.lock:
            self.state.variations = []


@dataclass
class Watcher:
    """Keeps ``info`` current from Patchy's events and reports picks of OpenFill in
    Plugins > Companions. Same face as the PhotoCraft companion's ``menu.Watcher``.

    Events arrive on the client's event thread, which must not wait for Patchy; anything
    that needs ``get_state`` is left to this watcher's own thread, at most every
    ``interval`` seconds, plus a slow look every ``fallback`` seconds."""

    client: PatchyClient
    on_pick: Callable[[str], None]
    on_quit: Callable[[], None] | None = None  # Plugins > Companions > Quit OpenFill
    interval: float = 0.25
    fallback: float = 10.0
    info: DocInfo = field(default_factory=DocInfo)
    native_id: int = 0  # Patchy's main window (Windows HWND), from get_info
    lock: threading.Lock = field(default_factory=threading.Lock)
    _dirty: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        threading.Thread(target=self._loop, name="openfill-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.info.snapshot()

    def set_waiting(self, waiting: bool) -> None:
        with self.lock:
            self.info.waiting = waiting

    def on_event(self, event: dict[str, Any]) -> None:
        """One ``notifications/patchy/event`` (called on the client's event thread)."""
        kind = event.get("type")
        if kind == "companionCommand":
            if event.get("companion") not in (None, COMPANION_ID):
                return
            action = str(event.get("action") or "show")
            if action == "quit":
                if self.on_quit is not None:
                    self.on_quit()
            elif action in PANEL_ACTIONS:
                self.on_pick("panel" if action == "show" else action)
            return
        if kind == "selectionChanged":
            bounds = event.get("bounds")
            with self.lock:
                if self.info.connected and str(event.get("documentId")) == str(self.info.index):
                    self.info.selection = [int(bounds[k]) for k in ("x", "y", "width", "height")] if bounds else None
                    self.info.updated = time.time()
                    return
        self._dirty.set()  # another document, a closed one, or the layers or history changed

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._dirty.wait(self.fallback)
            if self._stop.is_set():
                return
            self._dirty.clear()
            try:
                self.poll()
                if not self.native_id:
                    self.read_window()  # after a reattach, or when it was not there at the start
            except WorkspaceLost as err:
                with self.lock:
                    self.info = DocInfo(connected=False, error=str(err))
            except PatchyError:
                self._dirty.set()  # busy: look again shortly
            except Exception as err:  # noqa: BLE001 - keep watching
                with self.lock:
                    self.info.connected, self.info.error = False, str(err)
            self._stop.wait(self.interval)

    def poll(self) -> None:
        """One look at Patchy's state."""
        state = self.client.get_state(patience=0)
        docs = state.get("documents") or []
        active = next((d for d in docs if str(d.get("id")) == str(state.get("activeDocumentId"))), None)
        with self.lock:
            waiting = self.info.waiting
            if active is None:
                self.info = DocInfo(connected=True, documents=len(docs), updated=time.time(), waiting=waiting)
                return
            selection = active.get("selection") or {}
            bounds = selection.get("bounds") if selection.get("exists") else None
            self.info = DocInfo(
                connected=True,
                name=str(active.get("name") or ""),
                index=str(active["id"]),  # Patchy's document id
                width=int(active.get("width") or 0),
                height=int(active.get("height") or 0),
                mode="RGB",
                selection=[int(bounds[k]) for k in ("x", "y", "width", "height")] if bounds else None,
                active_layer_kind="",
                documents=len(docs),
                updated=time.time(),
                waiting=waiting,
            )

    def read_window(self) -> None:
        """Patchy's main window, for the tether (``get_info.mainWindow``)."""
        try:
            info = self.client.get_info(patience=0)
            self.native_id = int((info.get("mainWindow") or {}).get("nativeId") or 0)
        except (PatchyError, ValueError):
            pass


def photocraft_comfy_dirs() -> list[str]:
    """The ComfyUI folder OpenFill for PhotoCraft is set to, if any."""
    from openfill_photocraft.bridge import default_profile

    cfg = Path(default_profile()) / "openfill" / "config.json"
    try:
        folder = json.loads(cfg.read_text(encoding="utf-8")).get("comfy_dir")
    except (OSError, ValueError):
        return []
    return [folder] if isinstance(folder, str) and folder else []
