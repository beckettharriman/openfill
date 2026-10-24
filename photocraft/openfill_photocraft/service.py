# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The companion's engine: one OpenFill job at a time against the active PhotoCraft document.

``JobState`` is what the web page polls; ``WebRunner`` is ``App.runner_cls`` (the GTK
progress window's stand-in); ``Service`` runs fill, expand and text to image in a
worker thread and keeps the variations of the last job for the picker.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from openfill.app import App, NothingSelected
from openfill.backend import GenerationCancelled, describe_error
from openfill.progress import parse_preview_frame
from openfill.supervisor import process_name

from .bridge import PCImage, PCLayer, PhotoCraftBridge
from .control import ControlClient, ControlError

NO_SELECTION = "Select an area first: Marquee, Lasso or Object Selection. Select > All works on the whole image."


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
    preview_rect: tuple[int, int, int, int] | None = None  # the region a preview frame shows (job's first canvas)
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
                "previewRect": self.preview_rect,
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
        bridge.on_region = self._region
        self.app = App(interactive=True, io=bridge, runner_cls=WebRunner)
        self.app.job_state = self.state
        self._find_comfy()
        # ComfyUI stops when this companion ends (it is what the backend serves).
        import os

        self.app.server.watch_pid = os.getpid()
        self.app.server.watch_name = process_name(os.getpid()) or "python"
        self._thread: threading.Thread | None = None
        self.watcher = None  # menu.Watcher, set by the launcher: keeps the document's facts current
        self.engine = ""  # "" not started by us, "starting", "ready", or why it failed
        self._warm: threading.Thread | None = None
        self.image: PCImage | None = None
        self.layers: list[PCLayer] = []
        self.setup_task = ""  # the ComfyUI download this companion queued
        # The document's history before the last job, and as the job and the picker left it:
        # while it is still that, Keep and Discard can take the result back and leave one
        # clean step (Keep) or none (Discard) instead of piling deletes on top.
        self._job_base: list[str] = []
        self._expected_history: list[str] = []
        self._collapsed = False  # the job's several steps were made into one (an expand)

    def _find_comfy(self) -> None:
        """First start: use the ComfyUI OpenFill for GIMP uses, or one already on this computer."""
        from openfill.comfy_setup import detect_comfy
        from openfill.config import comfy_layout

        if comfy_layout(self.app.settings.comfy_dir) is not None:
            return
        for folder in gimp_comfy_dirs():
            if comfy_layout(folder) is not None:
                self.app.use_comfy_dir(folder)
                self.app.log.info("using the ComfyUI OpenFill for GIMP uses: %s", folder)
                self.rescan_models()
                return
        found = detect_comfy()
        if found:
            self.app.use_comfy_dir(str(found[0].path))
            self.app.log.info("using the ComfyUI found at %s (%s)", found[0].path, found[0].source)
            self.rescan_models()

    def rescan_models(self) -> None:
        """The catalog plus the user's own model files in the (new) ComfyUI folder."""
        from openfill import catalog as cat
        from openfill import local_models

        app = self.app
        try:
            entries = cat.load_merged(plugin_dir=app.plugin_dir, base=app.paths.base)
            found, _skipped = local_models.scan(app.models_root, entries, app.paths.base)
            app.catalog = cat.merge_entries(entries, found)
        except Exception as err:  # noqa: BLE001 - the catalog stays as it was
            app.log.warning("models not rescanned: %s", err)

    def _region(self, rect: tuple[int, int, int, int]) -> None:
        with self.state.lock:
            self.state.preview_rect = rect

    def _notice(self, text: str) -> None:
        with self.state.lock:
            self.state.notices.append(text)
        if hasattr(self, "app"):
            self.app.log.info("notice: %s", text)

    def warm_up(self) -> None:
        """Start ComfyUI in the background so the first Generate does not wait for it (only
        when it is set up and a model is there to use)."""
        from openfill.config import comfy_layout

        app = self.app
        if comfy_layout(app.settings.comfy_dir) is None or not app.settings.autostart:
            return
        if not (app.has_model_for("fill") or app.has_model_for("remove")):
            return

        def run() -> None:
            self.engine = "starting"
            try:
                health = app.check_backend()
                self.engine = "ready" if health.ok else (health.detail or "not running")
            except Exception as err:  # noqa: BLE001 - shown on the bar; a job tries again
                self.engine = describe_error(err)
            app.log.info("warm-up: %s", self.engine)

        self._warm = threading.Thread(target=run, name="openfill-warm-up", daemon=True)
        self._warm.start()

    # status -------------------------------------------------------------------------

    def photocraft(self) -> dict[str, Any]:
        """The active document and its selection, or why there is none."""
        if self.watcher is not None:
            return self.watcher.snapshot()
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
            self.state.title = {
                "fill": "Object Removal" if params.get("remove") else "Generative Fill",
                "expand": "Generative Expand",
                "txt2img": "Text to Image",
            }.get(kind, kind)
            self.state.model = ""
            self.state.fraction, self.state.started, self.state.finished = 0.0, time.time(), 0.0
            self.state.cancel_requested, self.state.variations, self.state.shown = False, [], 0
            self.state.preview, self.state.preview_seq = b"", self.state.preview_seq + 1
            self.state.preview_rect = None
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
        if self._warm is not None and self._warm.is_alive():
            with self.state.lock:
                self.state.text = "Starting the AI engine (the first time takes about a minute)..."
            self._warm.join()  # one start of ComfyUI, not two
        try:
            image = self.bridge.active_image() if kind != "txt2img" or p.get("target") == "layer" else None
            if kind != "txt2img" and image is None:
                raise RuntimeError("Open an image in PhotoCraft first.")
            self._job_base, self._expected_history, self._collapsed = [], [], False
            base: list[str] | None = None
            if image is not None:
                self.bridge.track(image)
                base = self.bridge.history(image)
            self.image = image
            seed = int(p.get("seed", -1))
            variations = int(p.get("variations", 1))
            model = str(p.get("model") or "active")
            if kind == "fill" and p.get("remove"):
                layers = self._remove(image, p, seed, variations)
            elif kind == "fill":
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
            done_text = self.state.text or "Done"
            if image is not None:
                with self.state.lock:
                    self.state.text = "Adding the result to PhotoCraft..."
                self.bridge.finish(image)  # the results reach the document here, in one step
                self._one_step(image, base)
            self.layers = list(layers or [])
            with self.state.lock:
                self.state.state = "done"
                self.state.fraction = 1.0
                self.state.text = self.state.message = done_text
                self.state.variations = [layer.name for layer in self.layers] if len(self.layers) > 1 else []
                self.state.shown = 0
        except BaseException as err:  # noqa: BLE001 - reported on the page
            cancelled = isinstance(err, GenerationCancelled) or self.state.cancel_requested
            self.app.log.warning("job %s failed: %s\n%s", kind, err, traceback.format_exc())
            with self.state.lock:
                self.state.state = "cancelled" if cancelled else "error"
                if cancelled:
                    self.state.message = "Cancelled - the image was left unchanged."
                elif isinstance(err, NothingSelected):  # the core's message names GIMP's tools
                    self.state.message = NO_SELECTION
                else:
                    self.state.message = describe_error(err)
        finally:
            if image is not None:
                self.bridge.discard(image)  # whatever did not reach the document is dropped
            with self.state.lock:
                self.state.finished = time.time()

    # Remove values of older menus (saved in the panel's settings): LaMa or MAT alone, and the
    # two-pass "LaMa + a generation model", which LaMa at full resolution replaced.
    OLD_REMOVALS = {"lama": "lama-big", "mat": "mat-places512"}

    def _remove(self, image: PCImage, p: dict[str, Any], seed: int, variations: int) -> list:
        """Remove object with ``removal``: "default" (LaMa at the image's own resolution,
        else MAT, else the active model with a background prompt) or any installed model's
        id, used as picked (erasers with their own prompt, generation models with a
        background-only prompt). One layer, one step."""
        how = str(p.get("removal") or "default")
        if how.startswith("refine:") or how == "default":
            how = "active"
        how = self.OLD_REMOVALS.get(how, how)
        layers = self.app.generative_fill(
            image, "", "", variations, 1.0, seed, how, remove=True, as_picked=how != "active"
        )
        for index, layer in enumerate(layers):
            layer.name = "Object Removal" if len(layers) == 1 else f"Object Removal ({index + 1})"
        if image.pending_group:
            image.pending_group = "Object Removal"
        return layers

    # ComfyUI ----------------------------------------------------------------------------

    def comfy_preflight(self) -> dict[str, Any]:
        """What a ComfyUI download would be (Windows): its size, where it goes, and the checks
        (disk space, graphics card). ``blocked`` when it cannot run."""
        from openfill import comfy_setup

        install_dir = comfy_setup.default_install_dir()
        manifest, pre = self.app.setup_preflight(install_dir)
        size = comfy_setup.download_notice(manifest.for_older_gpus() if pre.older_gpu else manifest)
        return {
            "folder": str(install_dir),
            "size": size,
            "blocked": pre.blocked,
            "cpu": pre.needs_cpu_choice,
            "older_gpu": pre.older_gpu,
            "text": pre.text(),
        }

    def setup_comfy(self) -> str:
        """Download and install ComfyUI's portable build (the version OpenFill for GIMP
        installs, checked against its SHA-256) as a background task. The supervisor saves the
        folder in the settings when it is done (see ``setup_progress``)."""
        from openfill import comfy_setup

        pre = self.comfy_preflight()
        if pre["blocked"]:
            raise RuntimeError(pre["text"] or "ComfyUI cannot be installed here.")
        self.setup_task = self.app.queue_comfy_setup(
            comfy_setup.default_install_dir(), bool(pre["cpu"]), bool(pre["older_gpu"])
        )
        return self.setup_task

    def setup_progress(self) -> dict[str, Any] | None:
        """The ComfyUI download while it runs ({fraction, text}), its error when it failed, or
        None. When it has finished, the new folder is taken into use here."""
        from openfill import comfy_setup

        status = self.app.task_status()
        current = status.get("current") or {}
        if current.get("type") == "setup_comfy":
            state = str(current.get("state") or "")
            done, total = int(current.get("done") or 0), int(current.get("total") or 0)
            fraction = float(current.get("fraction") or comfy_setup.overall_fraction(state, done, total))
            return {"fraction": fraction, "text": setup_step_text(state, done, total)}
        task = getattr(self, "setup_task", "")
        if not task:
            return None
        last = next((h for h in status.get("history") or [] if h.get("task_id") == task), None)
        if last is None:
            return {"fraction": 0.0, "text": "Getting ready..."}
        self.setup_task = ""
        if last.get("state") != "done":
            return {"error": str(last.get("message") or last.get("state") or "the download stopped")}
        self.app.reload_settings()
        self.rescan_models()
        self.warm_up()
        return None

    # models -----------------------------------------------------------------------------

    def uninstall(self, model_id: str) -> str:
        """Delete one installed model's files (a task for the download worker). Files another
        installed model also uses stay; companion entries (the Fooocus patch) are their own
        entries and stay too. The model set active by hand falls back to the automatic choice."""
        app = self.app
        task = app.remove_model(model_id)
        if app.settings.active_model == model_id:
            app.settings.active_model = ""
            app.settings.save(app.paths.config)
        return task

    def stop_download(self, model_id: str) -> list[str]:
        """Stop downloading one model: drop its queued tasks and cancel it when it is the one
        running. Its parts (the Fooocus patch) stop too, unless another queued model needs
        them. What was downloaded stays on disk, so Download resumes it. Returns the ids
        stopped."""
        from openfill import catalog as cat

        server = self.app.server
        queued = [t for t in server.pending_tasks() if t.get("type") == "download"]
        try:
            needs = set(cat.by_id(self.app.catalog, model_id).requires_models)
        except cat.CatalogError:
            needs = set()
        others = {t.get("model_id") for t in queued} - {model_id} - needs
        for other in others:
            try:
                needs -= set(cat.by_id(self.app.catalog, other).requires_models)
            except cat.CatalogError:
                pass
        stop = {model_id} | needs
        folder = server.tasks_dir
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if path.name.endswith(".running.json"):
                    continue
                try:
                    task = json.loads(path.read_text(encoding="utf-8"))
                    if task.get("type") == "download" and task.get("model_id") in stop:
                        path.unlink()
                except (OSError, ValueError):
                    pass
        current = server.task_status().get("current") or {}
        if current.get("type") == "download" and current.get("model_id") in stop:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "cancel").write_text(str(time.time()), encoding="utf-8")
        return sorted(stop)

    def show_variation(self, index: int) -> None:
        if not self.image or not (0 <= index < len(self.layers)):
            raise RuntimeError("No variations to pick from.")
        self.bridge.show_variation(self.image, self.layers, index)
        if self._expected_history:
            try:
                self._expected_history = self.bridge.history(self.image)
            except ControlError:
                self._expected_history = []
        with self.state.lock:
            self.state.shown = index

    def _one_step(self, image: PCImage, base: list[str] | None) -> None:
        """After a job: when it left several history steps (an expand crops the canvas, then adds
        its layers, once per round), make them one step named after the job, so one Undo takes
        it all back. Remembers the history for Keep and Discard."""
        try:
            after = self.bridge.history(image)
            if base is None or after[: len(base)] != base or len(after) == len(base):
                return
            if len(after) - len(base) > 1:
                self.bridge.collapse(image, len(base), self.state.title or "OpenFill")
                self._collapsed = True
                after = self.bridge.history(image)
            self._job_base, self._expected_history = base, after
        except ControlError as err:
            self.app.log.warning("the job's steps were not made one: %s", err)

    def keep(self, which: str) -> None:
        """'selected': delete the hidden variations; 'all': keep them (hidden); 'none': delete all."""
        if not self.image or not self.layers:
            return
        shown = self.state.shown
        if which != "all" and self._unchanged_since_job():
            image, base = self.image, self._job_base
            self._job_base = []
            if which == "none":
                self.bridge.rewind(image, len(base))  # as if the job had not run: no step
                self.layers = []
            elif not self._collapsed:
                chosen = self.layers[shown]
                self.bridge.rewind(image, len(base))
                self.bridge.put_back(image, chosen, re.sub(r" \(\d+\)$", "", chosen.name))
                self.layers = [chosen]
            else:  # an expand: its canvas stays; the hidden variations go, still one step
                for i, layer in enumerate(self.layers):
                    if i != shown and layer.id is not None:
                        self.client.run("layer.delete", layer=layer.id)
                self.bridge.collapse(image, len(base), self.state.title or "OpenFill")
                self.layers = [self.layers[shown]]
            with self.state.lock:
                self.state.variations = []
            return
        group = self.layers[0].group
        if which == "none" and group is not None and all(layer.group == group for layer in self.layers):
            doomed_ids = [group]  # the variations' group, in one step
        else:
            doomed_ids = [
                layer.id
                for i, layer in enumerate(self.layers)
                if layer.id is not None and (which == "none" or (which == "selected" and i != shown))
            ]
        for layer_id in doomed_ids:
            try:
                self.client.run("layer.delete", layer=layer_id)
            except ControlError:
                pass
        self.layers = [] if which != "all" else self.layers
        with self.state.lock:
            self.state.variations = []

    def _unchanged_since_job(self) -> bool:
        """True when the document's history is still exactly as the last job and the picker
        left it (nothing else was done since), so the job's step can be taken back."""
        if not self._job_base or self.image is None:
            return False
        try:
            return self.bridge.history(self.image) == self._expected_history
        except ControlError:
            return False


SETUP_STEPS = {
    "verifying": "Checking the download...",
    "unpacking": "Unpacking ComfyUI (a few minutes)...",
    "nodes": "Installing the inpaint nodes...",
    "packages": "Installing packages...",
}


def setup_step_text(state: str, done: int, total: int) -> str:
    """What the ComfyUI download is doing, for the panel."""
    from openfill import catalog as cat

    if state == "downloading":
        return f"Downloading ComfyUI: {cat.human_size(done)} of {cat.human_size(total)}"
    if state == "unpacking" and total:
        return f"Unpacking ComfyUI: {cat.human_size(done)} of about {cat.human_size(total)}"
    return SETUP_STEPS.get(state, "Getting ready...")


def gimp_comfy_dirs() -> list[str]:
    """ComfyUI folders set in OpenFill for GIMP's settings (newest GIMP first)."""
    import json
    import os
    import sys
    from pathlib import Path

    home = Path.home()
    if sys.platform == "win32":
        bases = [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "GIMP"]
    elif sys.platform == "darwin":
        bases = [home / "Library" / "Application Support" / "GIMP"]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        bases = [config / "GIMP", home / ".var" / "app" / "org.gimp.GIMP" / "config" / "GIMP"]
    found = []
    for base in bases:
        for cfg in sorted(base.glob("*/openfill/config.json"), reverse=True):
            try:
                folder = json.loads(cfg.read_text(encoding="utf-8")).get("comfy_dir")
            except (OSError, ValueError):
                continue
            if isinstance(folder, str) and folder and folder not in found:
                found.append(folder)
    return found
