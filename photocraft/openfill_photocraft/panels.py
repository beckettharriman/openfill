# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Models and settings, behind the panel's gear: three sections.

- Models: this computer in one line, the installed models (each with a trash button that
  uninstalls it), the ones to download (Download, Resume, or the progress and a button that
  stops it), and the Hugging Face key FLUX needs.
- Engine: ComfyUI's folder, when it starts and stops, extra options, the Remote GPU (ComfyUI
  on another computer), and Check, Stop engine and Logs.
- Generation: steps, prompt strictness, repaint margin and edge softness.

The views only read the refresher's facts and the settings on the UI thread; downloads,
removals, checks and saves run in a worker thread and report back through ``Bar.call_soon``.
"""

from __future__ import annotations

import threading
import tkinter as tk
from typing import TYPE_CHECKING, Any

from . import about, ui
from .service import photocraft_text
from .ui import BAD, BG, CW, DIM, OK, WARN

if TYPE_CHECKING:
    from .bar import Bar

# Settings the gear view edits: (key, label, kind). The rest stay in config.json.
SETTINGS = (
    ("autostart", "Start the AI engine with PhotoCraft", bool),
    ("idle_timeout_minutes", "Stop after idle", int),
    ("request_timeout", "Time limit", int),
    ("steps", "Steps", int),
    ("cfg", "Prompt strictness (CFG)", float),
    ("grow_percent", "Repaint margin (%)", float),
    ("feather_percent", "Edge softness (%)", float),
    ("extra_server_args", "Extra ComfyUI options", str),
    ("custom_workflow", "Custom ComfyUI workflow", str),
    ("custom_workflow_inputs", "Workflow inputs", str),
)
CUSTOM_HINT = (
    "Your own ComfyUI workflow as one more model for Fill. In ComfyUI use Workflow > Export (API). "
    "OpenFill sends it the selection's picture (the selection is its alpha, or goes to a Load Image "
    "Mask), the prompt and a seed, and takes the image of its Save Image node. It runs on your own "
    "ComfyUI only."
)
SECTIONS = (("models", "Models"), ("engine", "Engine"), ("generation", "Generation"))
SHARED_HINT = (
    "Use the models of another ComfyUI, such as Krita AI Diffusion's, without downloading them again. "
    "OpenFill only reads these folders: it never downloads, deletes or installs anything there."
)
BUSY = ("Downloading", "Waiting", "Installing", "Removing", "Working")


def field(parent: tk.Misc, label: str, control_factory, gap: int = 6) -> tuple[tk.Frame, Any]:
    """An uppercase label above a control."""
    box = ui.frame(parent)
    ui.caption(box, label).pack(anchor="w")
    control = control_factory(box)
    control.pack(anchor="w", pady=(ui.px(gap), 0))
    return box, control


class GearView(tk.Frame):
    """Models and settings: the section control and the three sections."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        self.section = "models"
        # the section control stays under the header; only the section below it scrolls
        self.tabs = ui.Seg(bar.gear_bar, SECTIONS, self._section_picked, CW, 13)
        self.tabs.pack(anchor="w", padx=ui.px(16), pady=ui.px((12, 0)))
        self.models = ModelsView(bar, self)
        self.engine = EngineView(bar, self)
        self.generation = GenerationView(bar, self)
        self._loaded = False

    def _section_picked(self, section: str) -> None:
        with ui.frozen(self.bar.root):
            self.show_section(section)

    def show_section(self, section: str) -> None:
        if section == "settings":
            section = "engine"
        self.section = section
        self.tabs.set(section)
        for name, view in (("models", self.models), ("engine", self.engine), ("generation", self.generation)):
            if name == section:
                view.pack(fill="x", anchor="w")
            else:
                view.pack_forget()
        if section == "models":
            self.models.update_view(self.bar.refresher.get())  # built already: this only updates it
        self.bar._layout_footer()
        if hasattr(self.bar, "scroller"):
            self.bar.scroller.yview_moveto(0)

    def update_view(self, facts: dict[str, Any]) -> None:
        """Keep the models list current, shown or not, so opening it shows it at once."""
        self.models.update_view(facts)

    def say(self, text: str, color: str = OK) -> None:
        {"models": self.models, "engine": self.engine, "generation": self.generation}[self.section].say(text, color)

    # the settings ------------------------------------------------------------------------

    def load(self) -> None:
        settings = self.bar.service.app.settings
        self.engine.load_backend(settings)
        self.engine.folder.set(settings.comfy_dir or "")
        self.engine.autostart.set(bool(settings.autostart))
        self.engine.idle.set(str(settings.idle_timeout_minutes))
        self.engine.timeout.set(str(settings.request_timeout))
        self.engine.extra.set(str(settings.extra_server_args or ""))
        from openfill import shared_models

        self.engine.shared.set(shared_models.display(settings.shared_models))
        self.engine.remote_url.set(settings.remote_url or "")
        self.engine.remote_ssh.set(settings.remote_ssh or "")
        for key, entry in self.generation.fields.items():
            entry.set(str(getattr(settings, key)))
        self.generation.loaded(settings)

    def values(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "autostart": self.engine.autostart.get(),
            "idle_timeout_minutes": self.engine.idle.get(),
            "request_timeout": self.engine.timeout.get(),
            "extra_server_args": self.engine.extra.get(),
        }
        raw.update({key: entry.get() for key, entry in self.generation.fields.items()})
        out = {}
        for key, label, kind in SETTINGS:
            try:
                out[key] = kind(raw[key]) if kind is not bool else bool(raw[key])
            except ValueError as err:
                raise ValueError(f"{label}: {raw[key]!r} is not a number.") from err
        from openfill.config import time_limit_problem

        problem = time_limit_problem(out["request_timeout"])
        if problem:
            raise ValueError(problem)
        return out

    def save(self) -> None:
        app = self.bar.service.app
        try:
            values = self.values()
        except ValueError as err:
            self.say(photocraft_text(str(err)), BAD)
            return
        from dataclasses import replace

        folder = self.engine.folder.get().strip()
        backend, cloud = self.engine.backend_values()
        if backend == self.engine.loaded_backend:
            # not switched here: keep what is set now (the browser page may have changed it since)
            backend = app.settings.backend
        for key in ("cloud_provider", "cloud_url", "cloud_model", "cloud_mask_mode"):  # the same for cloud
            if cloud[key] == self.engine.loaded_cloud.get(key):
                cloud[key] = getattr(app.settings, key)
        prev_provider = app.settings.cloud_provider
        trial = replace(app.settings)
        trial.set_cloud(
            cloud["cloud_provider"],
            cloud["cloud_url"],
            cloud["cloud_model"],
            cloud["cloud_api_key"],
            cloud.get("cloud_mask_mode"),
        )
        if backend == "cloud" and not trial.cloud_key_usable:
            self.say(trial.cloud_key_problem() or "Paste your API key for the cloud service first.", BAD)
            return
        from openfill import shared_models

        typed = self.engine.shared.get().strip()
        try:
            from openfill import catalog as cat

            new_root = cat.default_models_root(folder) if folder and folder != (app.settings.comfy_dir or "") else None
            shared = app.check_shared_models(typed, new_root)  # first: a bad folder changes nothing
        except Exception as err:  # noqa: BLE001 - says what is wrong
            self.say(str(err), BAD)
            return
        try:
            if folder and folder != (app.settings.comfy_dir or ""):
                app.use_comfy_dir(folder)
                self.bar.service.rescan_models()
                self.bar.service.warm_up()
            for key, value in values.items():
                setattr(app.settings, key, value)
            # a new key is bound to this service and address; an empty field never moves the saved one
            app.settings.set_cloud(
                cloud["cloud_provider"],
                cloud["cloud_url"],
                cloud["cloud_model"],
                cloud["cloud_api_key"],
                cloud.get("cloud_mask_mode"),
            )
            was_cloud, app.settings.backend = app.settings.uses_cloud, backend
            self._apply_remote(app.settings)
            app.settings.save(app.paths.config)
            app.reload_settings()
            if shared_models.parse(shared) != shared_models.parse(app.settings.shared_models):
                app.set_shared_models(shared)  # saves, writes the yaml, rescans; the engine picks it up when idle
            app.log.info("settings saved; images are made by: %s", app.settings.backend)
        except Exception as err:  # noqa: BLE001 - says what is wrong
            self.say(photocraft_text(str(err)), BAD)
            return
        self.engine.load_backend(app.settings)  # clears the key field: the key is saved, never shown
        if (
            app.settings.uses_cloud
            and app.settings.cloud_provider == "gemini"
            and prev_provider != "gemini"
            and hasattr(self.bar, "pick_variations")
        ):
            self.bar.pick_variations(1)
        self.bar.service.engine = ""
        if (was_cloud and not app.settings.uses_cloud) or app.settings.uses_remote:
            self.bar.service.warm_up()  # a Remote GPU: connects, so the bar says at once if it answers
        self.bar.refresher.wake.set()
        self.generation.loaded(app.settings)
        self.say("Settings saved.", OK)

    def _apply_remote(self, settings: Any) -> None:
        """The Remote GPU fields; a pasted pairing code is moved out of the address into the settings."""
        settings.remote_url = self.engine.remote_url.get().strip()
        settings.remote_ssh = self.engine.remote_ssh.get().strip()
        settings.take_pairing_code()
        self.engine.remote_url.set(settings.remote_url)


class ModelsView(tk.Frame):
    """This computer, the installed models and the ones to download, and the Hugging Face key."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        self.hardware = ui.small(self, "Looking...")
        self.hardware.pack(fill="x", pady=ui.px((12, 0)))
        self.note = ui.small(self, "")
        self.installed_title = ui.caption(self, "Installed")
        self.installed = ui.Box(self)
        self.available_title = ui.caption(self, "Available")
        self.available = ui.Box(self)
        self.key_box = ui.frame(self)
        ui.caption(self.key_box, "Hugging Face key (for FLUX)").pack(anchor="w")
        row = ui.frame(self.key_box)
        row.pack(fill="x", pady=ui.px((6, 0)))
        save = ui.Button(row, "Save", self._save_token)
        self.token = ui.Field(row, CW - ui.px(8) - save.w, "hf_...", secret=True, on_enter=self._save_token)
        self.token.pack(side="left")
        save.pack(side="left", padx=ui.px((8, 0)))
        self.key_box.pack(fill="x", pady=ui.px((12, 0)))
        self._shown: tuple = ()
        self._rows: dict[str, dict[str, Any]] = {}

    def say(self, text: str, color: str = OK) -> None:
        if not text:
            self.note.pack_forget()
            return
        self.note.configure(text=text, fg=color)
        self.note.pack(fill="x", pady=ui.px((8, 0)), after=self.hardware)

    def update_view(self, facts: dict[str, Any]) -> None:
        hardware = facts.get("hardware") or ""
        line = hardware or facts.get("comfy_problem") or ("Looking..." if not facts.get("error") else facts["error"])
        if self.hardware.cget("text") != line:
            self.hardware.configure(text=line)
        rows = [r for r in facts.get("rows") or [] if r["runnable"] or r["installed"]]
        installed = [r for r in rows if r["installed"]]
        installed.sort(key=lambda r: (not r["runnable"], not r["active"], r["name"].lower()))
        available = sorted((r for r in rows if not r["installed"]), key=lambda r: r["name"].lower())
        order = (tuple(r["id"] for r in installed), tuple(r["id"] for r in available))
        if order != self._shown:  # which models are where: lay both lists out again
            self._shown = order
            self._build(installed, available)
        else:  # the same rows: rebuild only a row whose buttons change (a download starts, ...)
            for box, rows in ((self.installed, installed), (self.available, available)):
                for i, row in enumerate(rows):
                    if self._rows.get(row["id"], {}).get("key") != self._row_key(row):
                        old = self._rows[row["id"]]["frame"]
                        self._row(box.inner, row, first=i == 0, before=old)
                        old.destroy()
                box.lift_corners()
        current = facts.get("current") or {}
        for row in installed + available:
            parts = self._rows.get(row["id"])
            if not parts:
                continue
            status = row.get("statusText", "")
            if "percent" in parts:
                pct = status.split()[-1] if status.startswith("Downloading") else ("Waiting" if status else "")
                if parts["percent"].cget("text") != pct:
                    parts["percent"].configure(text=pct)
            if "bar" in parts:
                total = current.get("total") or 0
                if current.get("model_id") == row["id"] and total:
                    parts["bar"].set((current.get("done") or 0) / total)

    @staticmethod
    def _mode(row: dict[str, Any]) -> str:
        status = row.get("statusText", "")
        if status.startswith(("Downloading", "Waiting")) and not row["installed"]:
            return "busy"
        if status.startswith(BUSY):
            return "working"
        return "idle"

    def _row_key(self, row: dict[str, Any]) -> tuple:
        """What a row's widgets depend on (not the percent, which changes in place)."""
        status = row.get("statusText", "")
        mode = self._mode(row)
        return (
            row["name"],
            row.get("size"),
            row["installed"],
            row["partial"],
            mode,
            status if mode == "working" else status.startswith("Downloading"),
            bool(row.get("missing_models")),
            bool(row.get("missing_nodes")),
            row.get("neededBy", ""),
        )

    def _build(self, installed: list[dict[str, Any]], available: list[dict[str, Any]]) -> None:
        for box in (self.installed, self.available):
            for child in box.inner.winfo_children():
                child.destroy()
        self._rows = {}
        for title, box, rows in (
            (self.installed_title, self.installed, installed),
            (self.available_title, self.available, available),
        ):
            title.pack_forget()
            box.pack_forget()
            if not rows:
                continue
            title.pack(anchor="w", pady=ui.px((12, 0)), before=self.key_box)
            box.pack(fill="x", pady=ui.px((6, 0)), before=self.key_box)
            for i, row in enumerate(rows):
                self._row(box.inner, row, first=i == 0)
            box.lift_corners()

    def _row(self, parent: tk.Misc, row: dict[str, Any], first: bool, before: tk.Misc | None = None) -> None:
        """One model: its name and what it does, and its button(s) on the right. Sizes are
        worked out from the buttons' known widths, so building a row lays nothing out."""
        from .modelinfo import model_detail

        f = ui.fonts(self)
        line = ui.frame(parent)
        line.pack(fill="x", **({"before": before} if before is not None else {}))
        if not first:
            ui.divider(line).pack(fill="x")
        body = ui.frame(line)
        body.pack(fill="x", padx=ui.px(12), pady=ui.px(10))
        top = ui.frame(body)
        top.pack(fill="x")
        right = ui.frame(top)
        right.pack(side="right")
        parts: dict[str, Any] = {"frame": line, "key": self._row_key(row)}
        mode = self._mode(row)
        status = row.get("statusText", "")
        detail = model_detail(row, row.get("neededBy", ""))
        width = 0

        def add(widget, w: int, padx=(0, 0)) -> None:
            nonlocal width
            widget.pack(side="left", padx=padx)
            width += w + sum(padx)

        if row["installed"]:
            if row.get("missing_models"):
                detail += " · Needs one more download"
                b = ui.Button(right, "Download", lambda i=row["id"]: self._get(i))
                add(b, b.w)
            elif row.get("missing_nodes"):
                detail += " · Needs extra parts"
                b = ui.Button(right, "Install", lambda i=row["id"]: self._nodes(i))
                add(b, b.w)
            elif mode == "working":
                t = ui.small(right, status, wrap=0)
                add(t, f.small.measure(status))
            elif row["runnable"]:
                add(ui.IconButton(right, "trash", lambda r=row: self._uninstall(r)), ui.IconButton.SIZE)
        elif mode == "busy":
            parts["percent"] = ui.small(right, "", wrap=0)
            add(parts["percent"], f.small.measure("Waiting"), ui.px((0, 4)))
            add(ui.IconButton(right, "close", lambda i=row["id"]: self._stop(i)), ui.IconButton.SIZE)
            if status.startswith("Downloading"):
                parts["bar"] = ui.Progress(body, CW - 2 - ui.px(24), BG)
                parts["bar"].pack(fill="x", pady=ui.px((8, 0)))
        elif mode == "working":
            t = ui.small(right, status, wrap=0)
            add(t, f.small.measure(status))
        else:
            b = ui.Button(right, "Resume" if row["partial"] else "Download", lambda i=row["id"]: self._get(i))
            add(b, b.w)
        wrap = CW - 2 - ui.px(24) - ui.px(12) - width
        names = ui.frame(top)
        names.pack(side="left", fill="x")
        ui.text(names, row["name"], f.body, wrap=wrap).pack(anchor="w")
        ui.small(names, detail, wrap=wrap).pack(anchor="w")
        self._rows[row["id"]] = parts

    def _run(self, fn, *args, done: str = "") -> None:
        bar = self.bar

        def work() -> None:
            try:
                fn(*args)
                if done:
                    bar.call_soon(self.say, done, OK)
            except Exception as err:  # noqa: BLE001 - shown in the view
                bar.call_soon(self.say, photocraft_text(str(err)), BAD)
            bar.refresher.wake.set()

        threading.Thread(target=work, daemon=True).start()

    def _get(self, model_id: str) -> None:
        self.say("")
        self._run(self.bar.service.app.queue_download, model_id)

    def _nodes(self, model_id: str) -> None:
        self.say("")
        self._run(self.bar.service.app.queue_node_install, model_id)

    def _stop(self, model_id: str) -> None:
        self._run(self.bar.service.stop_download, model_id)

    def _uninstall(self, row: dict[str, Any]) -> None:
        from .bar import short_name

        if not ui.ask(
            self.bar.root,
            f"Uninstall {short_name(row['name'])}?\n\n"
            f"Its files ({row['size']}) are deleted. Files other models use are kept. You can download it again later.",
            "Uninstall",
        ):
            return
        self.say("")
        self._run(self.bar.service.uninstall, row["id"])

    def _save_token(self) -> None:
        value = self.token.get().strip()
        self.token.set("")
        self._run(self.bar.service.app.save_token, value, done="Hugging Face key saved.")


class EngineView(tk.Frame):
    """Where images are made: this computer (ComfyUI's folder, starting and stopping it, its
    extra options) or a cloud service (its key, model and address, and where pictures go)."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        self.loaded_backend = ""  # what the switch showed when the settings were loaded
        self.loaded_cloud: dict[str, str] = {}  # and the cloud fields
        from openfill.config import BACKENDS

        choices = [(b.id, b.short or b.label) for b in BACKENDS if b.photocraft]
        box, self.backend = field(
            self, "Where images are made", lambda b: ui.Seg(b, choices, self._backend_picked, CW, 13)
        )
        box.pack(fill="x", pady=ui.px((12, 0)))
        self.local = ui.frame(self)
        self.cloud = ui.frame(self)
        self.remote = ui.frame(self)
        self._build_remote(self.remote)
        self._build_cloud(self.cloud)
        self._build_local(self.local)
        self.local.pack(fill="x")
        ui.divider(self).pack(fill="x", pady=ui.px((12, 0)))
        tools = ui.frame(self)
        tools.pack(fill="x", pady=ui.px((12, 0)))
        ui.caption(tools, "Engine").pack(anchor="w")
        row = ui.frame(tools)
        row.pack(anchor="w", pady=ui.px((8, 0)))
        for i, (label, command) in enumerate((("Check", self.check), ("Stop engine", self.stop), ("Logs", self.logs))):
            ui.Button(row, label, command).pack(side="left", padx=ui.px((8 if i else 0, 0)))
        self.result = ui.small(tools, "")
        self._tools = tools
        ui.divider(self).pack(fill="x", pady=ui.px((12, 0)))
        info = ui.frame(self)
        info.pack(fill="x", pady=ui.px((12, 0)))
        ui.caption(info, "About").pack(anchor="w")
        ui.small(info, about().replace(" · works", "\nWorks")).pack(fill="x", pady=ui.px((8, 0)))  # one line each

    def _build_cloud(self, parent: tk.Misc) -> None:
        from openfill import ui_text

        ui.small(parent, f"{ui_text.CLOUD_INTRO} {ui_text.CLOUD_WHEN}").pack(fill="x", pady=ui.px((12, 0)))
        box, self.provider = field(
            parent,
            "Service",
            lambda b: ui.Seg(b, ui_text.PHOTOCRAFT_CLOUD_PROVIDERS, self._cloud_changed, CW, 12, fit=True),
        )
        box.pack(fill="x", pady=ui.px((12, 0)))
        box, self.key = field(parent, "API key", lambda b: ui.Field(b, CW, ui_text.CLOUD_KEY_EMPTY, secret=True))
        box.pack(fill="x", pady=ui.px((12, 0)))
        self.key_hint = ui.small(parent, ui_text.CLOUD_KEY_HINT)
        self.key_hint.pack(fill="x", pady=ui.px((6, 0)))
        box, self.model = field(parent, "Model", lambda b: ui.Field(b, CW))
        box.pack(fill="x", pady=ui.px((12, 0)))
        box, self.url = field(parent, "Address", lambda b: ui.Field(b, CW))
        box.pack(fill="x", pady=ui.px((12, 0)))
        self._url_hint = ui.small(parent, "Empty model or address: the service's own. For LiteLLM, its /v1 address.")
        self._url_hint.pack(fill="x", pady=ui.px((6, 0)))
        self.mask_box, self.mask_mode = field(
            parent, "Mask method", lambda b: ui.Seg(b, ui_text.CLOUD_MASK_MODES, None, CW, 12)
        )

        def timeout_row(box):
            row = ui.frame(box)
            self.timeout = ui.Field(row, ui.px(88))
            self.timeout.pack(side="left")
            ui.small(row, "seconds, 0 = auto (local jobs too)", wrap=0).pack(side="left", padx=ui.px((8, 0)))
            return row

        box, _ = field(parent, "Time limit", timeout_row)
        box.pack(fill="x", pady=ui.px((12, 0)))
        self.note = ui.small(parent, "", fg=WARN)
        self.note.pack(fill="x", pady=ui.px((12, 0)))
        ui.small(parent, ui_text.CLOUD_COST).pack(fill="x", pady=ui.px((6, 0)))
        self.url.entry.bind("<KeyRelease>", lambda _e: self._cloud_changed(), add="+")

    def _cloud_changed(self, _key: object = None) -> None:
        from openfill import ui_text
        from openfill.backends.cloud import cloud_note, provider

        info = provider(self.provider.get())
        for entry, text in (
            (self.model, info.default_model or "the model name your service uses"),
            (self.url, info.base_url),
        ):
            entry.placeholder = text
            if not entry.get():  # only an empty field is redrawn: typing is never disturbed
                entry.set("")
        url = self.url.get().strip()
        self.key_hint.configure(text=ui_text.cloud_key_hint(info.id))
        self.key.placeholder = ui_text.cloud_key_placeholder(
            getattr(self, "_key_for", ""),
            info.id,
            url,
            getattr(self, "_has_key", False),
            env_var=getattr(self, "_gemini_env", ""),
            session_key=getattr(self, "_gemini_session", False),
        )
        if not self.key.get():
            self.key.set("")
        if info.id == "gemini":
            self.mask_box.pack(fill="x", pady=ui.px((12, 0)), after=self._url_hint)
            if _key == "gemini" and hasattr(self.bar, "pick_variations"):
                self.bar.pick_variations(1)
        else:
            self.mask_box.pack_forget()
        if hasattr(self.bar, "refit_gear") and self.backend.get() == "cloud":
            self.bar.refit_gear()  # the Mask method row came or went
        self.note.configure(text=cloud_note(info.id, url))

    def _backend_picked(self, key: str) -> None:
        with ui.frozen(self.bar.root):
            self._show_backend(key)

    def _show_backend(self, key: str) -> None:
        self.backend.set(key)
        shown = {"cloud": self.cloud, "remote": self.remote}.get(key, self.local)
        for frame in (self.local, self.remote, self.cloud):
            if frame is not shown:
                frame.pack_forget()
        shown.pack(fill="x", after=self.backend.master)  # under the switch, above the tools
        if hasattr(self.bar, "refit_gear"):
            self.bar.refit_gear()
        if hasattr(self.bar, "scroller"):
            self.bar.scroller.yview_moveto(0)

    def _build_remote(self, parent: tk.Misc) -> None:
        """Remote GPU: ComfyUI on another computer of yours (docs/remote-gpu.md)."""
        ui.small(parent, "ComfyUI runs on another computer of yours, with the OpenFill GPU server.").pack(
            fill="x", pady=ui.px((12, 0))
        )
        box, self.remote_url = field(
            parent, "Remote GPU pairing code", lambda b: ui.Field(b, CW, "ofgpu://... from the OpenFill GPU server")
        )
        box.pack(fill="x", pady=ui.px((12, 0)))
        box, self.remote_ssh = field(
            parent, "Over SSH (optional)", lambda b: ui.Field(b, CW, "user@computer, empty = connect directly")
        )
        box.pack(fill="x", pady=ui.px((12, 0)))

    def load_backend(self, settings) -> None:
        backend = settings.backend if settings.backend in ("comfyui", "remote", "cloud") else "comfyui"
        self.provider.set(
            settings.cloud_provider if settings.cloud_provider in ("openrouter", "openai", "gemini") else "openrouter"
        )
        self.model.set(settings.cloud_model or "")
        self.url.set(settings.cloud_url or "")
        mask_mode = getattr(settings, "cloud_mask_mode", "second_image")
        self.mask_mode.set(mask_mode if mask_mode in ("second_image", "overlay") else "second_image")
        self._has_key, self._key_for = bool(settings.cloud_api_key), settings.cloud_key_for
        creds = getattr(settings, "gemini_credentials", None)
        self._gemini_env = getattr(settings, "gemini_env_var", "")
        self._gemini_session = bool(creds.has_session_key) if creds is not None else False
        self.key.set("")  # the saved key is never put on screen
        self._cloud_changed()
        self.loaded_backend = backend
        self.loaded_cloud = self.backend_values()[1]
        self._show_backend(backend)

    def backend_values(self) -> tuple[str, dict[str, str]]:
        """(the backend picked, the cloud fields; ``cloud_api_key`` is "" unless a new key was typed)."""
        return self.backend.get(), {
            "cloud_provider": self.provider.get(),
            "cloud_model": self.model.get().strip(),
            "cloud_url": self.url.get().strip(),
            "cloud_mask_mode": self.mask_mode.get(),
            "cloud_api_key": self.key.get().strip(),
        }

    def _build_local(self, parent: tk.Misc) -> None:
        self._local_parent = parent

        def folder_row(box):
            row = ui.frame(box)
            browse = ui.Button(row, "Browse", self._browse)
            self.folder = ui.Field(row, CW - ui.px(8) - browse.w)
            self.folder.pack(side="left")
            browse.pack(side="left", padx=ui.px((8, 0)))
            return row

        box, _ = field(parent, "ComfyUI folder", folder_row)
        box.pack(fill="x", pady=ui.px((12, 0)))
        self.autostart = ui.Checkbox(parent, f"Start the AI engine with {self.bar.host}")
        self.autostart.pack(anchor="w", pady=ui.px((12, 0)))

        def idle_row(box):
            row = ui.frame(box)
            self.idle = ui.Field(row, ui.px(72))
            self.idle.pack(side="left")
            ui.small(row, "minutes, 0 = never", wrap=0).pack(side="left", padx=ui.px((8, 0)))
            return row

        box, _ = field(parent, "Stop after idle", idle_row)
        box.pack(fill="x", pady=ui.px((12, 0)))
        box, self.extra = field(parent, "Extra ComfyUI options", lambda b: ui.Field(b, CW, "for example --lowvram"))
        box.pack(fill="x", pady=ui.px((12, 0)))

        def shared_row(box):
            column = ui.frame(box)
            row = ui.frame(column)
            add = ui.Button(row, "Add", self._add_shared)
            find = ui.Button(row, "Find", self._find_shared)
            self.shared = ui.Field(row, CW - ui.px(16) - add.w - find.w, "Krita AI Diffusion's server folder")
            self.shared.pack(side="left")
            add.pack(side="left", padx=ui.px((8, 0)))
            find.pack(side="left", padx=ui.px((8, 0)))
            row.pack(anchor="w")
            ui.small(column, SHARED_HINT).pack(anchor="w", pady=ui.px((6, 0)))
            return column

        box, _ = field(parent, "Models from another ComfyUI", shared_row)
        box.pack(fill="x", pady=ui.px((12, 0)))

    def say(self, text: str, color: str = OK) -> None:
        if not text:
            self.result.pack_forget()
            return
        self.result.configure(text=text, fg=color)
        self.result.pack(fill="x", pady=ui.px((8, 0)))

    def _browse(self) -> None:
        from tkinter import filedialog

        folder = filedialog.askdirectory(parent=self.bar.root, title="Choose your ComfyUI folder", mustexist=True)
        if folder:
            self.folder.set(folder)

    def _add_shared(self) -> None:
        from tkinter import filedialog

        from openfill import shared_models

        folder = filedialog.askdirectory(
            parent=self.bar.root, title="Choose the folder of another ComfyUI", mustexist=True
        )
        if folder:
            folders = [*shared_models.parse(self.shared.get()), folder]
            self.shared.set(shared_models.display(shared_models.join(folders)))

    def _find_shared(self) -> None:
        from openfill import shared_models

        found = self.bar.service.app.shared_suggestions()
        if not found:
            self.say("No other ComfyUI was found. Use Add to pick its folder.", DIM)
            return
        folders = [*shared_models.parse(self.shared.get()), *(str(c.path) for c in found)]
        self.shared.set(shared_models.display(shared_models.join(folders)))
        self.say("Added " + ", ".join(f"{c.source} ({c.path})" for c in found) + ". Save to use its models.", OK)

    def check(self) -> None:
        bar = self.bar
        if bar.service.app.uses_cloud:
            self.say("Checking the key with the cloud service (no picture is sent)...", DIM)
        else:
            self.say("Checking the AI engine (starting it takes about a minute)...", DIM)

        def work() -> None:
            try:
                health = bar.service.app.check_backend()
                device = getattr(health, "device", "")
                text = ("Working: " if health.ok else "Not working: ") + photocraft_text(str(health.detail))
                text += f" on {device}." if device else "."
                note = bar.service.app.backend_note() if health.ok else ""
                text += f" {photocraft_text(note)}" if note else ""
                bar.call_soon(self.say, text, OK if health.ok else BAD)
            except Exception as err:  # noqa: BLE001
                bar.call_soon(self.say, photocraft_text(str(err)), BAD)

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        self.say(self.bar.service.stop_engine(), DIM)

    def logs(self) -> None:
        self.bar.service.app.open_log_folder()


class GenerationView(tk.Frame):
    """Steps, prompt strictness, repaint margin and edge softness."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        half = (CW - ui.px(12)) // 2
        self.fields: dict[str, ui.Field] = {}
        keys = (("steps", "cfg"), ("grow_percent", "feather_percent"))
        labels = {key: label for key, label, _kind in SETTINGS}
        for pair in keys:
            row = ui.frame(self)
            row.pack(fill="x", pady=ui.px((12, 0)))
            for i, key in enumerate(pair):
                box, entry = field(row, labels[key], lambda b: ui.Field(b, half))
                box.pack(side="left", padx=ui.px((12 if i else 0, 0)))
                self.fields[key] = entry
        ui.small(
            self,
            "Steps: more is slower and finer. Strictness: how closely the prompt is followed. "
            "Margin: how far past the selection the fill may repaint, as a share of its size.",
        ).pack(fill="x", pady=ui.px((12, 0)))
        # Advanced: a custom ComfyUI workflow, hidden until asked for
        self.advanced = ui.Checkbox(self, "Advanced")
        self.advanced.pack(anchor="w", pady=ui.px((12, 0)))
        self.advanced_box = ui.frame(self)

        def workflow_row(box):
            row = ui.frame(box)
            browse = ui.Button(row, "Browse", self._browse_workflow)
            self.fields["custom_workflow"] = ui.Field(row, CW - ui.px(8) - browse.w, "an API format .json file")
            self.fields["custom_workflow"].pack(side="left")
            browse.pack(side="left", padx=ui.px((8, 0)))
            return row

        box, _ = field(self.advanced_box, labels["custom_workflow"], workflow_row)
        box.pack(fill="x", pady=ui.px((12, 0)))
        box, self.fields["custom_workflow_inputs"] = field(
            self.advanced_box,
            labels["custom_workflow_inputs"],
            lambda b: ui.Field(b, CW, "found by itself, or image=10, mask=11, prompt=6.text"),
        )
        box.pack(fill="x", pady=ui.px((12, 0)))
        ui.small(self.advanced_box, CUSTOM_HINT).pack(fill="x", pady=ui.px((8, 0)))
        self.found = ui.small(self.advanced_box, "")
        self.found.pack(fill="x", pady=ui.px((8, 0)))
        self.advanced.var.trace_add("write", lambda *_a: self._show_advanced())
        self.result = ui.small(self, "")

    def _show_advanced(self) -> None:
        if self.advanced.get():
            self.advanced_box.pack(fill="x", after=self.advanced)
        else:
            self.advanced_box.pack_forget()
        if hasattr(self.bar, "refit_gear"):
            self.bar.refit_gear()  # the settings view grows to fit the Advanced fields
        else:
            self.bar._resize()

    def loaded(self, settings: Any) -> None:
        """After load or save: open Advanced when a workflow is set, and say what was found in it."""
        from openfill import custom_workflow

        path = str(getattr(settings, "custom_workflow", "") or "")
        if path and not self.advanced.get():
            self.advanced.set(True)
        text = custom_workflow.describe_file(path, str(getattr(settings, "custom_workflow_inputs", "") or ""))
        bad = bool(text) and not text.startswith("Found:")
        self.found.configure(text=text, fg=BAD if bad else DIM)

    def _browse_workflow(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self.bar.root,
            title="Choose a ComfyUI workflow (API format)",
            filetypes=[("ComfyUI workflow (API)", "*.json"), ("All files", "*")],
        )
        if path:
            self.fields["custom_workflow"].set(path)

    def say(self, text: str, color: str = OK) -> None:
        if not text:
            self.result.pack_forget()
            return
        self.result.configure(text=text, fg=color)
        self.result.pack(fill="x", pady=ui.px((8, 0)))
