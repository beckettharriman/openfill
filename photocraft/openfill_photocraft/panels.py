# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Models and settings, behind the panel's gear: three sections.

- Models: this computer in one line, the installed models (each with a trash button that
  uninstalls it), the ones to download (Download, Resume, or the progress and a button that
  stops it), and the Hugging Face key FLUX needs.
- Engine: ComfyUI's folder, when it starts and stops, extra options, and Check, Stop engine
  and Logs.
- Generation: steps, prompt strictness, repaint margin and edge softness.

The views only read the refresher's facts and the settings on the UI thread; downloads,
removals, checks and saves run in a worker thread and report back through ``Bar.call_soon``.
"""

from __future__ import annotations

import threading
import tkinter as tk
from typing import TYPE_CHECKING, Any

from . import ui
from .ui import BAD, BG, CW, DIM, OK

if TYPE_CHECKING:
    from .bar import Bar

# Settings the gear view edits: (key, label, kind). The rest stay in config.json.
SETTINGS = (
    ("autostart", "Start the AI engine with PhotoCraft", bool),
    ("idle_timeout_minutes", "Stop after idle", int),
    ("steps", "Steps", int),
    ("cfg", "Prompt strictness (CFG)", float),
    ("grow_percent", "Repaint margin (%)", float),
    ("feather_percent", "Edge softness (%)", float),
    ("extra_server_args", "Extra ComfyUI options", str),
)
SECTIONS = (("models", "Models"), ("engine", "Engine"), ("generation", "Generation"))
BUSY = ("Downloading", "Waiting", "Installing", "Removing", "Working")


def field(parent: tk.Misc, label: str, control_factory, gap: int = 6) -> tuple[tk.Frame, Any]:
    """An uppercase label above a control."""
    box = ui.frame(parent)
    ui.caption(box, label).pack(anchor="w")
    control = control_factory(box)
    control.pack(anchor="w", pady=(gap, 0))
    return box, control


class GearView(tk.Frame):
    """Models and settings: the section control and the three sections."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        self.section = "models"
        self.tabs = ui.Seg(self, SECTIONS, self.show_section, CW, 13)
        self.tabs.pack(anchor="w", pady=(12, 0))
        self.models = ModelsView(bar, self)
        self.engine = EngineView(bar, self)
        self.generation = GenerationView(bar, self)
        self._loaded = False

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
        if section != "models":
            self.load()
        else:
            self.models.update_view(self.bar.refresher.get())
        self.bar._layout_footer()

    def update_view(self, facts: dict[str, Any]) -> None:
        if self.section == "models":
            self.models.update_view(facts)

    def say(self, text: str, color: str = OK) -> None:
        {"models": self.models, "engine": self.engine, "generation": self.generation}[self.section].say(text, color)

    # the settings ------------------------------------------------------------------------

    def load(self) -> None:
        settings = self.bar.service.app.settings
        self.engine.folder.set(settings.comfy_dir or "")
        self.engine.autostart.set(bool(settings.autostart))
        self.engine.idle.set(str(settings.idle_timeout_minutes))
        self.engine.extra.set(str(settings.extra_server_args or ""))
        for key, entry in self.generation.fields.items():
            entry.set(str(getattr(settings, key)))

    def values(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "autostart": self.engine.autostart.get(),
            "idle_timeout_minutes": self.engine.idle.get(),
            "extra_server_args": self.engine.extra.get(),
        }
        raw.update({key: entry.get() for key, entry in self.generation.fields.items()})
        out = {}
        for key, label, kind in SETTINGS:
            try:
                out[key] = kind(raw[key]) if kind is not bool else bool(raw[key])
            except ValueError as err:
                raise ValueError(f"{label}: {raw[key]!r} is not a number.") from err
        return out

    def save(self) -> None:
        app = self.bar.service.app
        try:
            values = self.values()
        except ValueError as err:
            self.say(str(err), BAD)
            return
        folder = self.engine.folder.get().strip()
        try:
            if folder != (app.settings.comfy_dir or ""):
                app.use_comfy_dir(folder)
                self.bar.service.rescan_models()
                self.bar.service.warm_up()
            for key, value in values.items():
                setattr(app.settings, key, value)
            app.settings.save(app.paths.config)
            app.reload_settings()
        except Exception as err:  # noqa: BLE001 - says what is wrong
            self.say(str(err), BAD)
            return
        self.bar.refresher.wake.set()
        self.say("Settings saved.", OK)


class ModelsView(tk.Frame):
    """This computer, the installed models and the ones to download, and the Hugging Face key."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        self.hardware = ui.small(self, "Looking...")
        self.hardware.pack(fill="x", pady=(12, 0))
        self.note = ui.small(self, "")
        self.installed_title = ui.caption(self, "Installed")
        self.installed = ui.Box(self)
        self.available_title = ui.caption(self, "Available")
        self.available = ui.Box(self)
        self.key_box = ui.frame(self)
        ui.caption(self.key_box, "Hugging Face key (for FLUX)").pack(anchor="w")
        row = ui.frame(self.key_box)
        row.pack(fill="x", pady=(6, 0))
        save = ui.Button(row, "Save", self._save_token)
        self.token = ui.Field(row, CW - 8 - save.w, "hf_...", secret=True, on_enter=self._save_token)
        self.token.pack(side="left")
        save.pack(side="left", padx=(8, 0))
        self.key_box.pack(fill="x", pady=(12, 0))
        self._shown: tuple = ()
        self._rows: dict[str, dict[str, Any]] = {}

    def say(self, text: str, color: str = OK) -> None:
        if not text:
            self.note.pack_forget()
            return
        self.note.configure(text=text, fg=color)
        self.note.pack(fill="x", pady=(8, 0), after=self.hardware)

    def update_view(self, facts: dict[str, Any]) -> None:
        hardware = facts.get("hardware") or ""
        line = hardware or facts.get("comfy_problem") or ("Looking..." if not facts.get("error") else facts["error"])
        if self.hardware.cget("text") != line:
            self.hardware.configure(text=line)
        rows = [r for r in facts.get("rows") or [] if r["runnable"] or r["installed"]]
        installed = [r for r in rows if r["installed"]]
        installed.sort(key=lambda r: (not r["runnable"], not r["active"], r["name"].lower()))
        available = sorted((r for r in rows if not r["installed"]), key=lambda r: r["name"].lower())
        shown = tuple(
            (
                r["id"],
                r["installed"],
                r["partial"],
                self._mode(r),
                r.get("missing_models") != [],
                bool(r.get("missing_nodes")),
            )
            for r in installed + available
        )
        if shown != self._shown:
            self._shown = shown
            self._build(installed, available)
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
            title.pack(anchor="w", pady=(12, 0), before=self.key_box)
            box.pack(fill="x", pady=(6, 0), before=self.key_box)
            for i, row in enumerate(rows):
                if i:
                    ui.divider(box.inner).pack(fill="x")
                self._row(box.inner, row)
            box.lift_corners()
        self.bar._fit_height()

    def _row(self, parent: tk.Misc, row: dict[str, Any]) -> None:
        from .modelinfo import model_detail

        f = ui.fonts(self)
        line = ui.frame(parent)
        line.pack(fill="x")
        body = ui.frame(line)
        body.pack(fill="x", padx=12, pady=10)
        top = ui.frame(body)
        top.pack(fill="x")
        right = ui.frame(top)
        right.pack(side="right")
        parts: dict[str, Any] = {}
        mode = self._mode(row)
        status = row.get("statusText", "")
        detail = model_detail(row, row.get("neededBy", ""))
        if row["installed"]:
            if row.get("missing_models"):
                detail += " · Needs one more download"
                ui.Button(right, "Download", lambda i=row["id"]: self._get(i)).pack(side="left")
            elif row.get("missing_nodes"):
                detail += " · Needs extra parts"
                ui.Button(right, "Install", lambda i=row["id"]: self._nodes(i)).pack(side="left")
            elif mode == "working":
                ui.small(right, status, wrap=0).pack(side="left")
            elif row["runnable"]:
                ui.IconButton(right, "trash", lambda r=row: self._uninstall(r)).pack(side="left")
        elif mode == "busy":
            parts["percent"] = ui.small(right, "", wrap=0)
            parts["percent"].pack(side="left", padx=(0, 4))
            ui.IconButton(right, "close", lambda i=row["id"]: self._stop(i)).pack(side="left")
            if status.startswith("Downloading"):
                parts["bar"] = ui.Progress(body, CW - 2 - 24, BG)
                parts["bar"].pack(fill="x", pady=(8, 0))
        elif mode == "working":
            ui.small(right, status, wrap=0).pack(side="left")
        else:
            label = "Resume" if row["partial"] else "Download"
            ui.Button(right, label, lambda i=row["id"]: self._get(i)).pack(side="left")
        right.update_idletasks()
        wrap = CW - 2 - 24 - 12 - right.winfo_reqwidth()
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
                bar.call_soon(self.say, str(err), BAD)
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
    """ComfyUI's folder, starting and stopping it, and its extra options."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar

        def folder_row(box):
            row = ui.frame(box)
            browse = ui.Button(row, "Browse", self._browse)
            self.folder = ui.Field(row, CW - 8 - browse.w)
            self.folder.pack(side="left")
            browse.pack(side="left", padx=(8, 0))
            return row

        box, _ = field(self, "ComfyUI folder", folder_row)
        box.pack(fill="x", pady=(12, 0))
        self.autostart = ui.Checkbox(self, "Start the AI engine with PhotoCraft")
        self.autostart.pack(anchor="w", pady=(12, 0))

        def idle_row(box):
            row = ui.frame(box)
            self.idle = ui.Field(row, 72)
            self.idle.pack(side="left")
            ui.small(row, "minutes, 0 = never", wrap=0).pack(side="left", padx=(8, 0))
            return row

        box, _ = field(self, "Stop after idle", idle_row)
        box.pack(fill="x", pady=(12, 0))
        box, self.extra = field(self, "Extra ComfyUI options", lambda b: ui.Field(b, CW, "for example --lowvram"))
        box.pack(fill="x", pady=(12, 0))
        ui.divider(self).pack(fill="x", pady=(12, 0))
        tools = ui.frame(self)
        tools.pack(fill="x", pady=(12, 0))
        ui.caption(tools, "Engine").pack(anchor="w")
        row = ui.frame(tools)
        row.pack(anchor="w", pady=(8, 0))
        for i, (label, command) in enumerate((("Check", self.check), ("Stop engine", self.stop), ("Logs", self.logs))):
            ui.Button(row, label, command).pack(side="left", padx=(8 if i else 0, 0))
        self.result = ui.small(tools, "")

    def say(self, text: str, color: str = OK) -> None:
        if not text:
            self.result.pack_forget()
            return
        self.result.configure(text=text, fg=color)
        self.result.pack(fill="x", pady=(8, 0))

    def _browse(self) -> None:
        from tkinter import filedialog

        folder = filedialog.askdirectory(parent=self.bar.root, title="Choose your ComfyUI folder", mustexist=True)
        if folder:
            self.folder.set(folder)

    def check(self) -> None:
        bar = self.bar
        self.say("Checking the AI engine (starting it takes about a minute)...", DIM)

        def work() -> None:
            try:
                health = bar.service.app.check_backend()
                device = getattr(health, "device", "")
                text = ("Working: " if health.ok else "Not working: ") + str(health.detail)
                text += f" on {device}." if device else "."
                bar.call_soon(self.say, text, OK if health.ok else BAD)
            except Exception as err:  # noqa: BLE001
                bar.call_soon(self.say, str(err), BAD)

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        self.say(self.bar.service.app.stop_backend(), DIM)
        self.bar.service.engine = ""

    def logs(self) -> None:
        self.bar.service.app.open_log_folder()


class GenerationView(tk.Frame):
    """Steps, prompt strictness, repaint margin and edge softness."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.bar = bar
        half = (CW - 12) // 2
        self.fields: dict[str, ui.Field] = {}
        keys = (("steps", "cfg"), ("grow_percent", "feather_percent"))
        labels = {key: label for key, label, _kind in SETTINGS}
        for pair in keys:
            row = ui.frame(self)
            row.pack(fill="x", pady=(12, 0))
            for i, key in enumerate(pair):
                box, entry = field(row, labels[key], lambda b: ui.Field(b, half))
                box.pack(side="left", padx=(12 if i else 0, 0))
                self.fields[key] = entry
        ui.small(
            self,
            "Steps: more is slower and finer. Strictness: how closely the prompt is followed. "
            "Margin: how far past the selection the fill may repaint, as a share of its size.",
        ).pack(fill="x", pady=(12, 0))
        self.result = ui.small(self, "")

    def say(self, text: str, color: str = OK) -> None:
        if not text:
            self.result.pack_forget()
            return
        self.result.configure(text=text, fg=color)
        self.result.pack(fill="x", pady=(8, 0))
