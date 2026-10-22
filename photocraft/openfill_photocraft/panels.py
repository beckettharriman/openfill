# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The bar's Models and Settings views: what the web page's tabs did, inside the bar.

Both only read the refresher's facts and the settings on the UI thread; downloads,
checks and saves run in a worker thread and report back through ``Bar.call_soon``.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bar import Bar

# Settings the bar edits: (key, label, kind). The rest stay in config.json.
SETTINGS = (
    ("autostart", "Start the AI engine with PhotoCraft", bool),
    ("idle_timeout_minutes", "Stop the AI engine after idle (minutes, 0 = never)", int),
    ("steps", "Steps", int),
    ("cfg", "Prompt strictness (CFG)", float),
    ("grow_percent", "Repaint margin (% of the selection)", float),
    ("feather_percent", "Edge softness (%)", float),
    ("extra_server_args", "Extra ComfyUI options (for example --lowvram)", str),
)


class ModelsView(ttk.Frame):
    """Installed and downloadable models: Set active, Download or Resume, and progress."""

    def __init__(self, bar: Bar) -> None:
        super().__init__(bar.root)
        self.bar = bar
        self.hardware = ttk.Label(self, style="Dim.TLabel", wraplength=bar.wrap, justify="left")
        self.hardware.pack(fill="x")
        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas = tk.Canvas(holder, height=330, bg=bar.colors["BG"], highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        for widget in (self.canvas, self.rows):
            widget.bind("<Enter>", lambda _e: self._wheel(True))
            widget.bind("<Leave>", lambda _e: self._wheel(False))
        token = ttk.Frame(self)
        token.pack(fill="x", pady=(10, 0))
        ttk.Label(token, text="Hugging Face key (for FLUX)", style="Dim.TLabel").pack(side="left")
        self.token = ttk.Entry(token, show="*", width=12)
        self.token.pack(side="left", fill="x", expand=True, padx=(8, 6))
        ttk.Button(token, text="Save", style="Chip.TButton", command=self._save_token).pack(side="left")
        self._shown: tuple = ()
        self._buttons: dict[str, ttk.Button] = {}

    def _wheel(self, on: bool) -> None:
        if on:
            self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            self.canvas.bind_all("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
            self.canvas.bind_all("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))
        else:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(sequence)

    def update_view(self, facts: dict[str, Any]) -> None:
        hardware = facts.get("hardware") or ""
        self.hardware.configure(
            text=("This computer: " + hardware) if hardware else (facts.get("comfy_problem") or "Looking...")
        )
        rows = [r for r in facts.get("rows") or [] if r["runnable"] or r["installed"]]
        rows.sort(key=lambda r: (not r["active"], not r["installed"], r["name"].lower()))
        shown = tuple((r["id"], r["installed"], r["active"], r["partial"]) for r in rows)
        if shown != self._shown:
            self._shown = shown
            self._build_rows(rows)
        for row in rows:
            button = self._buttons.get(row["id"])
            busy = row["statusText"].startswith(("Downloading", "Waiting", "Installing", "Removing", "Working"))
            if button is not None and not row["installed"]:
                if busy:
                    button.configure(text=row["statusText"], state="disabled")
                else:
                    button.configure(text="Resume" if row["partial"] else "Download", state="normal")

    def _build_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows.winfo_children():
            child.destroy()
        self._buttons = {}
        for row in rows:
            line = ttk.Frame(self.rows)
            line.pack(fill="x", pady=(0, 8), padx=(0, 6))
            text = ttk.Frame(line)
            text.pack(side="left", fill="x", expand=True)
            ttk.Label(text, text=row["name"], wraplength=self.bar.wrap - 130, justify="left").pack(anchor="w")
            what = ", ".join(c for c in row["capabilities"] if c != "patch")
            detail = " · ".join(x for x in (what, row["size"], "installed" if row["installed"] else "") if x)
            ttk.Label(text, text=detail, style="Dim.TLabel").pack(anchor="w")
            if row["installed"] and not row["runnable"]:
                continue  # a part other models use (the Fooocus patch): nothing to press
            if row["installed"] and row["runnable"]:
                if row["active"]:
                    b = ttk.Label(line, text="Active", style="Ok.TLabel")
                else:
                    b = ttk.Button(
                        line, text="Set active", style="Chip.TButton", command=lambda i=row["id"]: self._activate(i)
                    )
            else:
                b = ttk.Button(line, text="Download", style="Chip.TButton", command=lambda i=row["id"]: self._get(i))
                self._buttons[row["id"]] = b
            b.pack(side="right", anchor="n")
            for widget in (line, text, *text.winfo_children()):
                widget.bind("<Enter>", lambda _e: self._wheel(True), add="+")

    def _run(self, fn, *args, done: str = "") -> None:
        def work() -> None:
            try:
                fn(*args)
                if done:
                    self.bar.call_soon(self.bar._say, done, self.bar.colors["OK"])
            except Exception as err:  # noqa: BLE001 - shown on the bar
                self.bar.call_soon(self.bar._say, str(err), self.bar.colors["BAD"])
            self.bar.refresher.wake.set()

        threading.Thread(target=work, daemon=True).start()

    def _activate(self, model_id: str) -> None:
        self._run(self.bar.service.app.set_active, model_id)

    def _get(self, model_id: str) -> None:
        self._run(self.bar.service.app.queue_download, model_id, done="Downloading. You can keep working.")

    def _save_token(self) -> None:
        value = self.token.get().strip()
        self.token.delete(0, "end")
        self._run(self.bar.service.app.save_token, value, done="Hugging Face key saved.")


class SettingsView(ttk.Frame):
    """ComfyUI's folder, when it starts and stops, and the generation settings."""

    def __init__(self, bar: Bar) -> None:
        super().__init__(bar.root)
        self.bar = bar
        ttk.Label(self, text="ComfyUI folder", style="Dim.TLabel").pack(anchor="w")
        folder = ttk.Frame(self)
        folder.pack(fill="x", pady=(2, 8))
        self.folder = ttk.Entry(folder)
        self.folder.pack(side="left", fill="x", expand=True)
        ttk.Button(folder, text="Browse", style="Chip.TButton", command=self._browse).pack(side="left", padx=(6, 0))
        self.vars: dict[str, tk.Variable] = {}
        for key, label, kind in SETTINGS:
            if kind is bool:
                var = tk.BooleanVar()
                ttk.Checkbutton(self, text=label, variable=var).pack(anchor="w", pady=(0, 6))
            else:
                var = tk.StringVar()
                line = ttk.Frame(self)
                line.pack(fill="x", pady=(0, 6))
                width = 26 if kind is str else 7
                ttk.Entry(line, textvariable=var, width=width).pack(side="right")
                ttk.Label(line, text=label, style="Dim.TLabel", wraplength=self.bar.wrap - 200).pack(
                    side="left", anchor="w"
                )
            self.vars[key] = var
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Check", style="Chip.TButton", command=self.check).pack(side="left")
        ttk.Button(buttons, text="Stop AI engine", style="Chip.TButton", command=self.stop).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="Logs", style="Chip.TButton", command=self.logs).pack(side="left", padx=(6, 0))

    def load(self) -> None:
        settings = self.bar.service.app.settings
        self.folder.delete(0, "end")
        self.folder.insert(0, settings.comfy_dir or "")
        for key, _label, kind in SETTINGS:
            value = getattr(settings, key)
            self.vars[key].set(bool(value) if kind is bool else str(value))

    def _browse(self) -> None:
        from tkinter import filedialog

        folder = filedialog.askdirectory(parent=self.bar.root, title="Choose your ComfyUI folder", mustexist=True)
        if folder:
            self.folder.delete(0, "end")
            self.folder.insert(0, folder)

    def save(self) -> None:
        app = self.bar.service.app
        say, colors = self.bar._say, self.bar.colors
        try:
            values = {key: kind(self.vars[key].get()) for key, _label, kind in SETTINGS}
        except ValueError as err:
            say(f"A number is not valid: {err}", colors["BAD"])
            return
        folder = self.folder.get().strip()
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
            say(str(err), colors["BAD"])
            return
        self.bar.refresher.wake.set()
        say("Settings saved.", colors["OK"])

    def check(self) -> None:
        bar = self.bar
        bar._say("Checking the AI engine (starting it takes about a minute)...", bar.colors["DIM"])

        def work() -> None:
            try:
                health = bar.service.app.check_backend()
                device = getattr(health, "device", "")
                text = ("Working: " if health.ok else "Not working: ") + str(health.detail)
                text += f" (on {device})" if device else ""
                bar.call_soon(bar._say, text, bar.colors["OK" if health.ok else "BAD"])
            except Exception as err:  # noqa: BLE001
                bar.call_soon(bar._say, str(err), bar.colors["BAD"])

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        self.bar._say(self.bar.service.app.stop_backend(), self.bar.colors["DIM"])
        self.bar.service.engine = ""

    def logs(self) -> None:
        self.bar.service.app.open_log_folder()
