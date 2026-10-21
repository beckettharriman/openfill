# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The companion's engine: one OpenFill job at a time against the active PhotoCraft document.

``JobState`` is what the web page polls; ``WebRunner`` is ``App.runner_cls`` (the GTK
progress window's stand-in); ``Service`` runs fill, expand and text to image in a
worker thread and keeps the variations of the last job for the picker.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from openfill.app import App
from openfill.backend import GenerationCancelled, describe_error
from openfill.progress import parse_preview_frame
from openfill.supervisor import process_name

from .bridge import PCImage, PCLayer, PhotoCraftBridge
from .control import ControlClient, ControlError


@dataclass
class JobState:
    """Everything the page shows about the current or last job."""

    state: str = "idle"  # idle | running | done | error | cancelled
    kind: str = ""
    title: str = ""
    model: str = ""
    fraction: float = 0.0
    text: str = ""
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    preview: bytes = b""
    preview_mime: str = ""
    preview_seq: int = 0
    cancel_requested: bool = False
    variations: list[str] = field(default_factory=list)  # names, best first
    shown: int = 0
    notices: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "kind": self.kind,
                "title": self.title,
                "model": self.model,
                "fraction": round(self.fraction, 4),
                "text": self.text,
                "message": self.message,
                "elapsed": round((self.finished or time.time()) - self.started, 1) if self.started else 0,
                "previewSeq": self.preview_seq,
                "variations": list(self.variations),
                "shown": self.shown,
                "notices": list(self.notices[-5:]),
            }


class WebRunner:
    """``App.runner_cls``: the job runs in the service's worker thread; progress, preview
    frames and Cancel go through ``JobState`` instead of a GTK window."""

    def __init__(self, app: App, title: str, model_name: str, interactive: bool = True) -> None:
        self.app = app
        self.state: JobState = app.job_state  # set by Service
        with self.state.lock:
            self.state.title, self.state.model = title, model_name
            if self.state.state != "running":
                self.state.state, self.state.started, self.state.fraction = "running", time.time(), 0.0

    def _progress(self, fraction: float, text: str) -> None:
        with self.state.lock:
            self.state.fraction, self.state.text = max(0.0, min(1.0, float(fraction))), str(text)

    def _preview(self, payload: bytes) -> None:
        frame = parse_preview_frame(payload)
        if frame is None:
            return
        with self.state.lock:
            self.state.preview, self.state.preview_mime = frame.data, frame.mime
            self.state.preview_seq += 1

    def _cancelled(self) -> bool:
        return self.state.cancel_requested

    def next_round(self) -> None:
        pass

    def run(self, job: Any) -> object:
        result = job(self._progress, self._preview, self._cancelled)
        if self.state.cancel_requested:
            raise GenerationCancelled("cancelled by the user after the result arrived")
        return result

    def done(self, message: str = "Done", close_after_ms: int = 1000) -> None:
        with self.state.lock:
            self.state.text = message

    def fail(self, error: BaseException) -> None:
        pass  # Service records the outcome


class Service:
    """Owns the PhotoCraft connection, OpenFill's App and the one running job."""

    def __init__(self, client: ControlClient, bridge: PhotoCraftBridge) -> None:
        self.client = client
        self.bridge = bridge
        self.state = JobState()
        bridge.notify = self._notice
        self.app = App(interactive=True, io=bridge, runner_cls=WebRunner)
        self.app.job_state = self.state
        self._find_comfy()
        # ComfyUI stops when this companion ends (it is what the backend serves).
        import os

        self.app.server.watch_pid = os.getpid()
        self.app.server.watch_name = process_name(os.getpid()) or "python"
        self._thread: threading.Thread | None = None
        self.image: PCImage | None = None
        self.layers: list[PCLayer] = []

    def _find_comfy(self) -> None:
        """First start: use a ComfyUI already on this computer (as the GIMP plug-in's notice does)."""
        from openfill.comfy_setup import detect_comfy
        from openfill.config import comfy_layout

        if comfy_layout(self.app.settings.comfy_dir) is not None:
            return
        found = detect_comfy()
        if found:
            self.app.use_comfy_dir(str(found[0].path))
            self.app.log.info("using the ComfyUI found at %s (%s)", found[0].path, found[0].source)

    def _notice(self, text: str) -> None:
        with self.state.lock:
            self.state.notices.append(text)
        if hasattr(self, "app"):
            self.app.log.info("notice: %s", text)

    # status -------------------------------------------------------------------------

    def photocraft(self) -> dict[str, Any]:
        """The active document and its selection, or why there is none."""
        try:
            image = self.bridge.active_image()
            if image is None:
                return {"connected": True, "document": None}
            d = self.client.run("document.inspect")
            sel = d.get("selectionBounds") if d.get("hasSelection") else None
            return {
                "connected": True,
                "document": {
                    "name": image.name,
                    "width": d.get("width"),
                    "height": d.get("height"),
                    "mode": d.get("mode"),
                },
                "selection": sel,
            }
        except ControlError as err:
            return {"connected": False, "error": str(err)}

    # jobs -------------------------------------------------------------------------------

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: str, params: dict[str, Any]) -> None:
        if self.busy():
            raise RuntimeError("A job is already running.")
        with self.state.lock:
            self.state.state, self.state.kind, self.state.message, self.state.text = "running", kind, "", "Starting..."
            self.state.fraction, self.state.started, self.state.finished = 0.0, time.time(), 0.0
            self.state.cancel_requested, self.state.variations, self.state.shown = False, [], 0
            self.state.preview, self.state.preview_seq = b"", self.state.preview_seq + 1
        self._thread = threading.Thread(target=self._job, args=(kind, params), name="openfill-job", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        with self.state.lock:
            self.state.cancel_requested = True
        try:
            self.app.server.request_cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            from openfill.comfy_api import ComfyClient

            ComfyClient(self.app.server.url(), timeout=3).interrupt()
        except Exception:  # noqa: BLE001
            pass

    def _job(self, kind: str, p: dict[str, Any]) -> None:
        image = None
        try:
            image = self.bridge.active_image() if kind != "txt2img" or p.get("target") == "layer" else None
            if kind != "txt2img" and image is None:
                raise RuntimeError("Open an image in PhotoCraft first.")
            if image is not None:
                self.bridge.track(image)
            self.image = image
            seed = int(p.get("seed", -1))
            variations = int(p.get("variations", 1))
            model = str(p.get("model") or "active")
            if kind == "fill":
                layers = self.app.generative_fill(
                    image,
                    str(p.get("prompt", "")),
                    str(p.get("negative", "")),
                    variations,
                    float(p.get("strength", 1.0)),
                    seed,
                    model,
                    remove=bool(p.get("remove")),
                )
            elif kind == "expand":
                layers = self.app.generative_expand(
                    image,
                    str(p.get("prompt", "")),
                    str(p.get("negative", "")),
                    int(p.get("left", 0)),
                    int(p.get("top", 0)),
                    int(p.get("right", 0)),
                    int(p.get("bottom", 0)),
                    variations,
                    seed,
                    model,
                )
            elif kind == "txt2img":
                self.app.text_to_image(
                    image,
                    str(p.get("prompt", "")),
                    str(p.get("negative", "")),
                    int(p.get("width", 0)),
                    int(p.get("height", 0)),
                    variations,
                    seed,
                    model,
                    str(p.get("target", "image")),
                )
                layers = []
            else:
                raise RuntimeError(f"unknown job {kind}")
            self.layers = list(layers or [])
            with self.state.lock:
                self.state.state = "done"
                self.state.fraction = 1.0
                self.state.message = self.state.text or "Done"
                self.state.variations = [layer.name for layer in self.layers] if len(self.layers) > 1 else []
                self.state.shown = 0
        except BaseException as err:  # noqa: BLE001 - reported on the page
            cancelled = isinstance(err, GenerationCancelled) or self.state.cancel_requested
            self.app.log.warning("job %s failed: %s\n%s", kind, err, traceback.format_exc())
            with self.state.lock:
                self.state.state = "cancelled" if cancelled else "error"
                self.state.message = "Cancelled - the image was left unchanged." if cancelled else describe_error(err)
        finally:
            if image is not None:
                self.bridge.finish(image)
            with self.state.lock:
                self.state.finished = time.time()

    def show_variation(self, index: int) -> None:
        if not self.image or not (0 <= index < len(self.layers)):
            raise RuntimeError("No variations to pick from.")
        self.bridge.show_variation(self.image, self.layers, index)
        with self.state.lock:
            self.state.shown = index

    def keep(self, which: str) -> None:
        """'selected': delete the hidden variations; 'all': keep them (hidden); 'none': delete all."""
        if not self.image or not self.layers:
            return
        shown = self.state.shown
        doomed = [
            layer for i, layer in enumerate(self.layers) if which == "none" or (which == "selected" and i != shown)
        ]
        for layer in doomed:
            try:
                self.client.run("layer.delete", layer=layer.id)
            except ControlError:
                pass
        self.layers = [] if which != "all" else self.layers
        with self.state.lock:
            self.state.variations = []
