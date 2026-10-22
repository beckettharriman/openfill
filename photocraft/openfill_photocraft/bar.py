# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The OpenFill bar: a small window that stays on top of PhotoCraft, like Photoshop's
contextual task bar. Prompt, Generate, progress with a live preview, and the
variations picker. Models and settings stay on the web page (the "Models and
settings" button opens it).

Tk runs on the main thread; the job runs in the service's worker thread, and slower
look-ups (models on disk, ComfyUI) in a refresher thread. The bar only reads their
results.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from PIL import Image

from .menu import Watcher
from .service import Service

BG, PANEL, LINE, TEXT, DIM, ACCENT, BAD, OK, WARN = (
    "#1e1f22",
    "#2b2d31",
    "#3a3c42",
    "#e8e8ea",
    "#9a9ba2",
    "#4a8ef0",
    "#e5534b",
    "#3fb950",
    "#e8a33d",
)
WIDTH = 420
PREVIEW_MAX = (WIDTH - 32, 230)
THUMB = 64


def photo(image: Image.Image) -> tk.PhotoImage:
    """A Tk image from a Pillow one, through PNG (Tk reads PNG itself; no ImageTk needed)."""
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return tk.PhotoImage(data=buf.getvalue())


TABS = (("fill", "Fill"), ("remove", "Remove"), ("expand", "Expand"), ("txt2img", "Create"))
EXPAND_PRESETS = (
    ("+25%", None),
    ("1:1", (1, 1)),
    ("4:3", (4, 3)),
    ("3:2", (3, 2)),
    ("16:9", (16, 9)),
    ("9:16", (9, 16)),
)
CREATE_SIZES = (("Square", 1024, 1024), ("Landscape", 1216, 832), ("Portrait", 832, 1216))
STARTER = ("lama-big", "dreamshaper-8-inpainting")


def icon_image(size: int = 64) -> Image.Image:
    """OpenFill's window icon: a white four-point sparkle on a blue rounded square."""
    from PIL import ImageDraw

    scale = 4
    big = size * scale
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 5, fill=(74, 142, 240, 255))
    c, r, w = big / 2, big * 0.36, big * 0.09
    d.polygon(
        [
            (c, c - r),
            (c + w, c - w),
            (c + r, c),
            (c + w, c + w),
            (c, c + r),
            (c - w, c + w),
            (c - r, c),
            (c - w, c - w),
        ],
        fill="white",
    )
    sc, sr, sw = big * 0.76, big * 0.12, big * 0.035
    d.polygon(
        [
            (sc, sc - sr - big * 0.48),
            (sc + sw, sc - sw - big * 0.48),
            (sc + sr, sc - big * 0.48),
            (sc + sw, sc + sw - big * 0.48),
            (sc, sc + sr - big * 0.48),
            (sc - sw, sc + sw - big * 0.48),
            (sc - sr, sc - big * 0.48),
            (sc - sw, sc - sw - big * 0.48),
        ],
        fill="white",
    )
    return im.resize((size, size), Image.LANCZOS)


def primary_monitor() -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the primary monitor (X11 and XWayland, through xrandr)."""
    import re
    import shutil
    import subprocess

    if not shutil.which("xrandr"):
        return None
    try:
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    monitors = []
    for line in out.splitlines():
        m = re.match(r"\s*\d+:\s+\+(\*?)\S+\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", line)
        if m:
            monitors.append((m.group(1) == "*", int(m.group(4)), int(m.group(5)), int(m.group(2)), int(m.group(3))))
    if not monitors:
        return None
    best = next((m for m in monitors if m[0]), monitors[0])
    return best[1], best[2], best[3], best[4]


def expand_margins(width: int, height: int, preset: tuple[int, int] | None) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) pixels that bring the canvas to ``preset``'s aspect ratio,
    split evenly; ``None`` adds 25% on every side."""
    if preset is None:
        x, y = round(width * 0.25), round(height * 0.25)
        return x, y, x, y
    rw, rh = preset
    w, h = width, height
    if width * rh < height * rw:
        w = round(height * rw / rh)
    else:
        h = round(width * rh / rw)
    dx, dy = w - width, h - height
    return dx // 2, dy // 2, dx - dx // 2, dy - dy // 2


class Refresher:
    """Slow facts for the bar, looked up off the UI thread every few seconds."""

    def __init__(self, service: Service) -> None:
        self.service = service
        self.facts: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._loop, name="openfill-bar-refresh", daemon=True).start()

    def get(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.facts)

    def _loop(self) -> None:
        while True:
            try:
                facts = self.look()
            except Exception as err:  # noqa: BLE001 - shown on the bar
                facts = {"error": str(err)}
            with self.lock:
                self.facts = facts
            self.wake.wait(4.0)
            self.wake.clear()

    def look(self) -> dict[str, Any]:
        from openfill import catalog as cat
        from openfill.config import comfy_layout, comfy_problem

        app = self.service.app
        comfy_ok = comfy_layout(app.settings.comfy_dir) is not None
        facts: dict[str, Any] = {
            "comfy_ok": comfy_ok,
            "comfy_problem": "" if comfy_ok else comfy_problem(app.settings.comfy_dir),
        }
        if not comfy_ok:
            return facts
        rows = app.model_rows(None)
        status = app.task_status()
        current = status.get("current") or {}
        pending = [t.get("model_id") for t in app.server.pending_tasks()]
        models: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            if row["installed"] and row["runnable"] and not row["missing_models"] and not row["missing_nodes"]:
                for capability in row["capabilities"]:
                    models.setdefault(capability, []).append((row["id"], row["name"]))
        active = app.active_model_id()
        facts.update(
            models=models,
            active=active,
            active_name=next((r["name"] for r in rows if r["id"] == active), ""),
            downloading="",
        )
        busy = [r for r in rows if r["id"] == current.get("model_id") or r["id"] in pending]
        if busy:
            facts["downloading"] = "; ".join(f"{r['name']}: {cat.row_status(r, current, pending)}" for r in busy)
        facts["starter_missing"] = [r["id"] for r in rows if r["id"] in STARTER and not r["installed"]]
        return facts


class Bar:
    """The window. ``show(action)`` is what the Filter > Plug-ins entries call."""

    def __init__(self, service: Service, watcher: Watcher, page_url: str, profile: Path) -> None:
        self.service, self.watcher, self.page_url, self.profile = service, watcher, page_url, profile
        self.settings_file = profile / "bar.json"
        self.prefs = self._load_prefs()
        self.calls: queue.Queue = queue.Queue()  # actions from other threads
        self.refresher = Refresher(service)
        self.root = tk.Tk(className="OpenFill")
        self.root.title("OpenFill")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.minsize(WIDTH, 120)
        self.root.maxsize(WIDTH, 4000)  # a fixed width; long texts wrap
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            self._icon = photo(icon_image())
            self.root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001 - the window works without an icon
            pass
        self._style()
        self.tab = tk.StringVar(value="fill")
        self.variations = tk.IntVar(value=int(self.prefs.get("variations", 1)))
        self.model_choice = tk.StringVar(value="Active model")
        self.target = tk.StringVar(value="image")
        self.size_name = tk.StringVar(value="Square")
        self.margins = [0, 0, 0, 0]
        self.preset = tk.StringVar(value="")
        self._preview_seq = -1
        self._images: list[Any] = []  # keep PhotoImages alive
        self._thumbs_for: tuple = ()
        self._model_ids: dict[str, str] = {}
        self._last_state = ""
        self._build()
        self._place()
        self.root.after(150, self._tick)

    # setup ------------------------------------------------------------------------

    def _load_prefs(self) -> dict[str, Any]:
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_prefs(self) -> None:
        try:
            self.settings_file.write_text(json.dumps(self.prefs), encoding="utf-8")
        except OSError:
            pass

    def _style(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            f = tkfont.nametofont(name)
            families = set(tkfont.families(self.root))
            for family in ("Inter", "Noto Sans", "Segoe UI", "SF Pro Text", "Helvetica Neue", "DejaVu Sans"):
                if family in families:
                    f.configure(family=family)
                    break
            f.configure(size=10)
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.small = tkfont.nametofont("TkDefaultFont").copy()
        self.small.configure(size=9)
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(
            ".",
            background=BG,
            foreground=TEXT,
            fieldbackground=PANEL,
            bordercolor=LINE,
            lightcolor=PANEL,
            darkcolor=PANEL,
            troughcolor=PANEL,
            focuscolor=ACCENT,
            insertcolor=TEXT,
        )
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Dim.TLabel", foreground=DIM, font=self.small)
        s.configure("Bad.TLabel", foreground=BAD)
        s.configure("Ok.TLabel", foreground=OK)
        s.configure("Warn.TLabel", foreground=WARN)
        s.configure("TButton", background=PANEL, foreground=TEXT, padding=(10, 5), borderwidth=1, relief="flat")
        s.map("TButton", background=[("active", LINE), ("disabled", BG)], foreground=[("disabled", DIM)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=self.bold)
        s.map("Accent.TButton", background=[("active", "#5d9cf5"), ("disabled", LINE)])
        s.configure("Tab.TButton", background=BG, foreground=DIM, padding=(14, 6), width=0)
        s.configure("TabOn.TButton", background=PANEL, foreground=TEXT, padding=(14, 6), font=self.bold, width=0)
        s.map("Tab.TButton", background=[("active", PANEL)])
        s.configure("Chip.TButton", padding=(8, 3), font=self.small, width=0)
        s.configure("ChipOn.TButton", padding=(8, 3), font=self.small, background=ACCENT, foreground="#ffffff", width=0)
        s.configure("TEntry", padding=6)
        s.configure("TCombobox", padding=4, arrowcolor=TEXT)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, borderwidth=0, thickness=6)
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _build(self) -> None:
        pad = {"padx": 14}
        top = ttk.Frame(self.root)
        top.pack(fill="x", pady=(10, 0), **pad)
        self.tab_buttons = {}
        for key, label in TABS:
            b = ttk.Button(top, text=label, style="Tab.TButton", command=lambda k=key: self.select_tab(k))
            b.pack(side="left")
            self.tab_buttons[key] = b
        ttk.Button(top, text="\u2013", style="Tab.TButton", command=self.collapse).pack(side="right")
        self._build_pill()
        self.context = ttk.Label(self.root, style="Dim.TLabel", wraplength=WIDTH - 44, justify="left")
        self.context.pack(fill="x", pady=(8, 0), **pad)

        self.body = ttk.Frame(self.root)
        self.body.pack(fill="x", pady=(6, 0), **pad)

        # Fill and Create: the prompt
        self.prompt_row = ttk.Frame(self.body)
        self.prompt = ttk.Entry(self.prompt_row, width=14)
        self.prompt.pack(side="left", fill="x", expand=True)
        self.prompt.bind("<Return>", lambda _e: self.generate())
        self.prompt.bind("<KP_Enter>", lambda _e: self.generate())
        self.prompt.bind("<Up>", lambda _e: self._recall(1))
        self.prompt.bind("<Down>", lambda _e: self._recall(-1))
        self._recall_at = -1
        self.go = ttk.Button(self.prompt_row, text="Generate", style="Accent.TButton", command=self.generate)
        self.go.pack(side="left", padx=(8, 0))
        self.hint = ttk.Label(self.body, style="Dim.TLabel", wraplength=WIDTH - 44, justify="left")

        # Expand: presets
        self.expand_row = ttk.Frame(self.body)
        self.preset_buttons = {}
        for label, ratio in EXPAND_PRESETS:
            b = ttk.Button(
                self.expand_row,
                text=label,
                style="Chip.TButton",
                command=lambda lab=label, r=ratio: self.pick_preset(lab, r),
            )
            b.pack(side="left", padx=(0, 4))
            self.preset_buttons[label] = b
        self.expand_size = ttk.Label(self.body, style="Dim.TLabel")

        # Create: size and destination
        self.create_row = ttk.Frame(self.body)
        self.size_buttons = {}
        for label, _w, _h in CREATE_SIZES:
            b = ttk.Button(
                self.create_row, text=label, style="Chip.TButton", command=lambda lab=label: self.pick_size(lab)
            )
            b.pack(side="left", padx=(0, 4))
            self.size_buttons[label] = b
        self.target_row = ttk.Frame(self.body)
        ttk.Label(self.target_row, text="Result as", style="Dim.TLabel").pack(side="left")
        self.target_buttons = {}
        for value, label in (("image", "New image"), ("layer", "Layer in this image")):
            b = ttk.Button(
                self.target_row, text=label, style="Chip.TButton", command=lambda v=value: self.pick_target(v)
            )
            b.pack(side="left", padx=(6, 0))
            self.target_buttons[value] = b

        # Options: variations and model
        self.options = ttk.Frame(self.body)
        ttk.Label(self.options, text="Variations", style="Dim.TLabel").pack(side="left")
        self.var_buttons = []
        for n in (1, 2, 3, 4):
            b = ttk.Button(
                self.options, text=str(n), width=2, style="Chip.TButton", command=lambda k=n: self.pick_variations(k)
            )
            b.pack(side="left", padx=(4 if n == 1 else 2, 0))
            self.var_buttons.append(b)
        self.model_row = ttk.Frame(self.body)
        ttk.Label(self.model_row, text="Model", style="Dim.TLabel").pack(side="left")
        self.model_box = ttk.Combobox(self.model_row, textvariable=self.model_choice, state="readonly", width=12)
        self.model_box.pack(side="left", padx=(16, 0), fill="x", expand=True)

        # Progress and result
        self.job = ttk.Frame(self.root)
        self.job_title = ttk.Label(self.job, font=self.bold)
        self.job_title.pack(anchor="w")
        self.progress = ttk.Progressbar(self.job, mode="determinate", maximum=1000, style="Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(6, 2))
        self.job_text = ttk.Label(self.job, style="Dim.TLabel")
        self.job_text.pack(anchor="w")
        self.preview = tk.Label(self.job, bg=BG, bd=0)
        self.thumbs = ttk.Frame(self.job)
        self.job_buttons = ttk.Frame(self.job)
        self.cancel_button = ttk.Button(self.job_buttons, text="Cancel", command=self.service.cancel)
        self.discard_button = ttk.Button(self.job_buttons, text="Discard", command=lambda: self.keep("none"))
        self.keep_all_button = ttk.Button(self.job_buttons, text="Keep all", command=lambda: self.keep("all"))
        self.keep_button = ttk.Button(
            self.job_buttons, text="Keep", style="Accent.TButton", command=lambda: self.keep("selected")
        )
        self.message = ttk.Label(self.root, wraplength=WIDTH - 44, justify="left")

        # Setup banner and footer
        self.setup = ttk.Frame(self.root)
        self.setup_text = ttk.Label(self.setup, style="Warn.TLabel", wraplength=WIDTH - 44, justify="left")
        self.setup_text.pack(anchor="w")
        self.setup_buttons = ttk.Frame(self.setup)
        self.setup_button = ttk.Button(self.setup_buttons, style="Accent.TButton", command=self._setup_action)
        self.setup_button.pack(side="left")
        self.setup_help = ttk.Button(self.setup_buttons, text="How to install ComfyUI", command=self._comfy_help)
        self.footer = ttk.Frame(self.root)
        self.footer.pack(side="bottom", fill="x", pady=(10, 10), **pad)
        self.status = ttk.Label(self.footer, style="Dim.TLabel")
        self.status.pack(side="left")
        ttk.Button(self.footer, text="Models and settings", style="Chip.TButton", command=self.open_page).pack(
            side="right"
        )

        self.root.bind("<Escape>", lambda _e: self.service.cancel() if self.service.busy() else None)
        self.select_tab("fill")
        self.pick_variations(self.variations.get())
        self.pick_size("Square")
        self._update_target()

    def _build_pill(self) -> None:
        """The collapsed bar: one line with the status; a click opens the bar again."""
        self.pill = tk.Toplevel(self.root, bg=BG)
        self.pill.title("OpenFill")
        self.pill.resizable(False, False)
        self.pill.attributes("-topmost", True)
        self.pill.protocol("WM_DELETE_WINDOW", self.hide)
        self.pill.withdraw()
        row = ttk.Frame(self.pill)
        row.pack(fill="x", padx=10, pady=6)
        ttk.Label(row, text="OpenFill", font=self.bold).pack(side="left")
        self.pill_text = ttk.Label(row, style="Dim.TLabel", width=24)
        self.pill_text.pack(side="left", padx=(8, 8))
        ttk.Button(row, text="Open", style="Chip.TButton", command=self.expand).pack(side="right")
        for widget in (row, self.pill_text):
            widget.bind("<Button-1>", lambda _e: self.expand())
        self.collapsed = False

    def collapse(self) -> None:
        self.collapsed = True
        self.prefs["collapsed"] = True
        x = self.root.winfo_x() + self.root.winfo_width()
        y = self.root.winfo_y()
        self.root.withdraw()
        self.pill.update_idletasks()
        self.pill.geometry(f"+{max(0, x - self.pill.winfo_reqwidth())}+{y}")
        self.pill.deiconify()
        self.pill.lift()

    def expand(self) -> None:
        if self.collapsed:
            self.collapsed = False
            self.prefs["collapsed"] = False
            self.pill.withdraw()
        self.root.deiconify()
        self.root.lift()

    def _place(self) -> None:
        self.root.update_idletasks()
        x, y = self.prefs.get("x"), self.prefs.get("y")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = max(WIDTH, self.root.winfo_reqwidth())
        if not isinstance(x, int) or not isinstance(y, int) or not (0 <= x <= sw - w and 0 <= y < sh - 120):
            mx, my, mw, _mh = primary_monitor() or (0, 0, sw, sh)
            x, y = max(mx, mx + mw - w - 40), my + 110  # top right, below a window's title and menu bars
        self.root.geometry(f"+{x}+{y}")
        self.root.bind("<Configure>", self._moved)

    def _moved(self, event) -> None:
        if event.widget is self.root:
            self.prefs["x"], self.prefs["y"] = self.root.winfo_x(), self.root.winfo_y()

    # actions ------------------------------------------------------------------------

    def call_soon(self, fn, *args) -> None:
        """Run ``fn`` on the UI thread (for other threads)."""
        self.calls.put((fn, args))

    def show(self, action: str = "") -> None:
        """Bring the bar up; a menu action also picks its tab (Remove starts at once)."""
        self.expand()
        self.root.attributes("-topmost", True)
        if action in dict(TABS):
            self.select_tab(action)
        if action == "remove":
            self.generate()
        elif action in ("fill", "txt2img", ""):
            self.root.focus_force()
            self.prompt.focus_set()
            self.prompt.select_range(0, "end")

    def hide(self) -> None:
        self._save_prefs()
        self.root.withdraw()
        self.pill.withdraw()
        self.collapsed = False
        if not self.prefs.get("told_reopen"):
            self.prefs["told_reopen"] = True
            self._save_prefs()
            from tkinter import messagebox

            messagebox.showinfo(
                "OpenFill",
                "OpenFill keeps running while PhotoCraft is open.\n\n"
                "To bring this window back, use Filter > Plug-ins > OpenFill Panel in PhotoCraft "
                "(or any OpenFill entry there).",
            )

    def select_tab(self, key: str) -> None:
        self.tab.set(key)
        for k, b in self.tab_buttons.items():
            b.configure(style="TabOn.TButton" if k == key else "Tab.TButton")
        for w in (
            self.prompt_row,
            self.hint,
            self.expand_row,
            self.expand_size,
            self.create_row,
            self.options,
            self.model_row,
            self.target_row,
        ):
            w.pack_forget()
        if key in ("fill", "txt2img", "expand"):
            if key == "expand":
                self.expand_row.pack(fill="x")
                self.expand_size.pack(anchor="w", pady=(4, 6))
            self.prompt_row.pack(fill="x")
            if key == "txt2img":
                self.create_row.pack(fill="x", pady=(8, 0))
                self.target_row.pack(fill="x", pady=(6, 0))
        self.go.configure(text={"fill": "Generate", "remove": "Remove", "expand": "Expand", "txt2img": "Generate"}[key])
        if key == "remove":
            self.prompt_row.pack(fill="x")
            self.prompt.pack_forget()
            self.go.pack_forget()
            self.go.pack(side="left")
            self.hint.configure(text="Erases what you selected and fills in the background around it.")
            self.hint.pack(anchor="w", pady=(6, 0))
        else:
            self.go.pack_forget()
            self.prompt.pack(side="left", fill="x", expand=True)
            self.go.pack(side="left", padx=(8, 0))
            placeholder = {
                "fill": "Describe what to add, or leave empty to fill from the surroundings",
                "expand": "Optional: describe what the new area shows",
                "txt2img": "Describe the image you want",
            }[key]
            self.hint.configure(text=placeholder)
            self.hint.pack(anchor="w", pady=(4, 0), after=self.prompt_row)
        if key != "remove":
            self.options.pack(fill="x", pady=(8, 0))
            self.model_row.pack(fill="x", pady=(6, 0))
        self._fill_models()
        self._update_context()

    def pick_variations(self, n: int) -> None:
        self.variations.set(n)
        self.prefs["variations"] = n
        for i, b in enumerate(self.var_buttons, start=1):
            b.configure(style="ChipOn.TButton" if i == n else "Chip.TButton")

    def pick_preset(self, label: str, ratio) -> None:
        info = self.watcher.snapshot().get("document") or {}
        if not info:
            return
        self.margins = list(expand_margins(int(info["width"]), int(info["height"]), ratio))
        self.preset.set(label if any(self.margins) else "")
        for k, b in self.preset_buttons.items():
            b.configure(style="ChipOn.TButton" if k == self.preset.get() else "Chip.TButton")
        self._update_context()
        if not any(self.margins):
            self.expand_size.configure(text=f"The image is already {label}. Pick another size.")

    def pick_size(self, label: str) -> None:
        self.size_name.set(label)
        for k, b in self.size_buttons.items():
            b.configure(style="ChipOn.TButton" if k == label else "Chip.TButton")

    def pick_target(self, value: str) -> None:
        self.target.set(value)
        self._update_target()

    def toggle_target(self) -> None:
        self.pick_target("layer" if self.target.get() == "image" else "image")

    def _update_target(self) -> None:
        for value, b in self.target_buttons.items():
            b.configure(style="ChipOn.TButton" if value == self.target.get() else "Chip.TButton")

    def _model(self) -> str:
        return self._model_ids.get(self.model_choice.get(), "active")

    def generate(self) -> None:
        if self.service.busy():
            return
        kind, prompt = self.tab.get(), self.prompt.get().strip()
        body: dict[str, Any] = {"variations": self.variations.get(), "seed": -1, "model": self._model()}
        try:
            if kind in ("fill", "remove"):
                if not (self.watcher.snapshot().get("selection")):
                    self._say("Select an area first: Marquee, Lasso or Object Selection.", BAD)
                    return
                body.update(
                    prompt="" if kind == "remove" else prompt, remove=kind == "remove", strength=1.0, negative=""
                )
                if kind == "remove":
                    body.update(variations=1, model="active")
                self.service.start("fill", body)
            elif kind == "expand":
                if not any(self.margins):
                    self._say("Pick how far to expand: one of the sizes above.", BAD)
                    return
                left, top, right, bottom = self.margins
                body.update(prompt=prompt, negative="", left=left, top=top, right=right, bottom=bottom)
                self.service.start("expand", body)
                self.margins = [0, 0, 0, 0]
                self.preset.set("")
                for b in self.preset_buttons.values():
                    b.configure(style="Chip.TButton")
            else:
                if not prompt:
                    self._say("Describe the image first.", BAD)
                    return
                _, w, h = next(s for s in CREATE_SIZES if s[0] == self.size_name.get())
                body.update(prompt=prompt, negative="", width=w, height=h, target=self.target.get())
                self.service.start("txt2img", body)
        except Exception as err:  # noqa: BLE001
            self._say(str(err), BAD)
            return
        self._say("")
        self._thumbs_for = ()
        self._remember(prompt)
        self._save_prefs()

    def _remember(self, prompt: str) -> None:
        if prompt:
            past = [p for p in self.prefs.get("prompts", []) if p != prompt]
            self.prefs["prompts"] = [prompt, *past][:30]
        self._recall_at = -1

    def _recall(self, step: int) -> str:
        """Up and Down in the prompt go through the earlier prompts."""
        past = self.prefs.get("prompts", [])
        if not past:
            return "break"
        self._recall_at = max(-1, min(len(past) - 1, self._recall_at + step))
        self.prompt.delete(0, "end")
        if self._recall_at >= 0:
            self.prompt.insert(0, past[self._recall_at])
        return "break"

    def keep(self, which: str) -> None:
        threading.Thread(target=self._safe, args=(self.service.keep, which), daemon=True).start()

    def pick_variation(self, index: int) -> None:
        threading.Thread(target=self._safe, args=(self.service.show_variation, index), daemon=True).start()

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as err:  # noqa: BLE001
            self.call_soon(self._say, str(err), BAD)

    def open_page(self) -> None:
        webbrowser.open(self.page_url + "#models")

    def _setup_action(self) -> None:
        facts = self.refresher.get()
        if not facts.get("comfy_ok"):
            self.choose_comfy()
            return

        def queue_starters() -> None:
            for model_id in facts.get("starter_missing") or []:
                self.service.app.queue_download(model_id)
            self.refresher.wake.set()

        threading.Thread(target=self._safe, args=(queue_starters,), daemon=True).start()

    def choose_comfy(self) -> None:
        """Ask for the ComfyUI folder and use it."""
        from tkinter import filedialog

        folder = filedialog.askdirectory(parent=self.root, title="Choose your ComfyUI folder", mustexist=True)
        if not folder:
            return
        try:
            self.service.app.use_comfy_dir(folder)
        except Exception as err:  # noqa: BLE001 - says what the folder lacks
            self._say(str(err), BAD)
            return
        self.service.rescan_models()
        self.service.warm_up()
        self._say("ComfyUI found. It is starting in the background.", OK)
        self.refresher.wake.set()

    def _comfy_help(self) -> None:
        webbrowser.open("https://github.com/beckettharriman/gimp-openfill#install")

    # updates ----------------------------------------------------------------------

    def _say(self, text: str, color: str = OK) -> None:
        if not text:
            self.message.pack_forget()
            return
        self.message.configure(text=text, foreground=color)
        self.message.pack(fill="x", padx=14, pady=(8, 0), before=self.footer)

    def _fill_models(self) -> None:
        facts = self.refresher.get()
        capability = {"fill": "fill", "remove": "remove", "expand": "expand", "txt2img": "txt2img"}[self.tab.get()]
        entries = (facts.get("models") or {}).get(capability, [])
        active = facts.get("active_name") or ""
        first = f"{active} (active)" if active else "The active model"
        names = [first] + [name for _id, name in entries]
        self._model_ids = {first: "active", **{name: model_id for model_id, name in entries}}
        if list(self.model_box["values"]) != names:
            self.model_box["values"] = names
        if self.model_choice.get() not in names:
            self.model_choice.set(first)

    def _update_context(self) -> None:
        snap = self.watcher.snapshot()
        doc, sel, tab = snap.get("document"), snap.get("selection"), self.tab.get()
        if not snap.get("connected"):
            text = "Waiting for PhotoCraft..."
        elif snap.get("waiting"):
            text = "PhotoCraft is not answering. If its window is minimized or covered, bring it into view."
        elif tab == "txt2img":
            text = "Makes a new image" + (f", or a layer in {doc['name']}." if doc else ".")
        elif not doc:
            text = "Open an image in PhotoCraft."
        elif tab in ("fill", "remove"):
            text = (
                f"{doc['name']}: selection {sel[2]} x {sel[3]} px"
                if sel
                else "Select an area first: Marquee, Lasso or Object Selection."
            )
        else:
            w, h = int(doc["width"]), int(doc["height"])
            if any(self.margins):
                left, top, right, bottom = self.margins
                self.expand_size.configure(text=f"{w} x {h}  >  {w + left + right} x {h + top + bottom} px")
            elif "already" not in self.expand_size.cget("text"):
                self.expand_size.configure(text=f"{w} x {h} px. Pick a size.")
            text = f"{doc['name']}: adds new image around the edges."
        if self.context.cget("text") != text:
            self.context.configure(text=text)

    def _tick(self) -> None:
        try:
            while True:
                fn, args = self.calls.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        try:
            self._update()
        finally:
            self.root.after(250, self._tick)

    def _update(self) -> None:
        self._update_context()
        facts = self.refresher.get()
        self._fill_models()
        # setup banner
        problem = ""
        if facts and not facts.get("comfy_ok", True):
            problem, button = (
                "OpenFill runs on ComfyUI, the free AI engine, and did not find it on this computer. "
                "If you have it, choose its folder (the one with main.py).",
                "Choose ComfyUI folder",
            )
        elif facts.get("downloading"):
            problem, button = f"Downloading: {facts['downloading']}", ""
        elif facts.get("comfy_ok") and not (facts.get("models") or {}).get("fill"):
            problem, button = (
                "OpenFill needs a model to start: LaMa for removing things (0.2 GB) and "
                "DreamShaper 8 for filling and expanding (2 GB).",
                "Download them (2.2 GB)",
            )
        if problem:
            self.setup_text.configure(text=problem)
            if button:
                self.setup_button.configure(text=button)
                self.setup_buttons.pack(anchor="w", pady=(6, 0), fill="x")
                if facts.get("comfy_ok", True):
                    self.setup_help.pack_forget()
                else:
                    self.setup_help.pack(side="left", padx=(6, 0))
            else:
                self.setup_buttons.pack_forget()
            self.setup.pack(fill="x", padx=14, pady=(10, 0), before=self.footer)
        else:
            self.setup.pack_forget()
        engine = self.service.engine
        if not facts:
            status = "Checking..."
        elif not facts.get("comfy_ok"):
            status = "Not set up"
        elif engine == "starting":
            status = "Starting the AI engine..."
        elif engine in ("", "ready"):
            status = "Ready"
        else:
            status = "AI engine: " + engine[:60]
        self.status.configure(text=status)

        st = self.service.state.snapshot()
        busy = self.service.busy()
        if self.collapsed:
            if st["state"] == "running":
                self.pill_text.configure(
                    text=f"{ {'fill': 'Fill', 'expand': 'Expand', 'txt2img': 'Create'}.get(st['kind'], 'Working') }: {int(st['fraction'] * 100)}%"
                )
            elif st["state"] == "done" and len(st["variations"]) > 1:
                self.expand()  # pick a variation
            else:
                self.pill_text.configure(text=status)
        self.go.state(["disabled"] if busy else ["!disabled"])
        if st["state"] == "idle":
            self.job.pack_forget()
            return
        self.job.pack(fill="x", padx=14, pady=(12, 0), before=self.footer)
        title = st["title"] or st["kind"]
        self.job_title.configure(text=title + (f"  ({st['model']})" if st["model"] else ""))
        self.progress["value"] = int(st["fraction"] * 1000)
        elapsed = int(st["elapsed"])
        clock = f"{elapsed // 60}:{elapsed % 60:02d}"
        if st["state"] == "running":
            self.job_text.configure(text=f"{st['text']}  {clock}", style="Dim.TLabel")
        elif st["state"] == "done":
            several = len(st["variations"]) > 1
            done = f"Done in {clock}." + (" Click a variation to see it, then Keep." if several else "")
            self.job_text.configure(text=done, style="Ok.TLabel")
        else:
            self.job_text.configure(text=st["message"], style="Bad.TLabel")
        self._update_preview(st)
        self._update_variations(st)
        for b in (self.cancel_button, self.discard_button, self.keep_all_button, self.keep_button):
            b.pack_forget()
        if st["state"] == "running":
            self.cancel_button.pack(side="right")
        elif len(st["variations"]) > 1:
            self.keep_button.pack(side="right")
            self.keep_all_button.pack(side="right", padx=(0, 6))
            self.discard_button.pack(side="right", padx=(0, 6))
        self.job_buttons.pack(fill="x", pady=(8, 0))
        if st["state"] != self._last_state and st["state"] in ("done", "error"):
            self.refresher.wake.set()
        self._last_state = st["state"]

    def _update_preview(self, st: dict[str, Any]) -> None:
        if st["state"] != "running":
            self.preview.pack_forget()
            return
        if st["previewSeq"] == self._preview_seq:
            return
        self._preview_seq = st["previewSeq"]
        with self.service.state.lock:
            data = self.service.state.preview
        if not data:
            self.preview.pack_forget()
            return
        try:
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                im.thumbnail(PREVIEW_MAX)
                frame = photo(im)
        except Exception:  # noqa: BLE001 - a bad frame is skipped
            return
        self._images = [frame, *[i for i in self._images if i is not frame][:8]]
        self.preview.configure(image=frame)
        if not self.preview.winfo_ismapped():
            where = {"before": self.job_buttons} if self.job_buttons.winfo_manager() else {}
            self.preview.pack(pady=(8, 0), **where)

    def _update_variations(self, st: dict[str, Any]) -> None:
        names = st["variations"]
        key = (tuple(names), st["shown"])
        if len(names) < 2 or st["state"] != "done":
            self.thumbs.pack_forget()
            self._thumbs_for = ()
            return
        if key == self._thumbs_for:
            return
        self._thumbs_for = key
        for child in self.thumbs.winfo_children():
            child.destroy()
        ttk.Label(self.thumbs, text="Pick a variation:", style="Dim.TLabel").pack(anchor="w")
        row = ttk.Frame(self.thumbs)
        row.pack(anchor="w", pady=(4, 0))
        photos = []
        for index, layer in enumerate(self.service.layers[: len(names)]):
            try:
                im = Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), layer.rgba).convert("RGB")
                im.thumbnail((THUMB * 2, THUMB))
                thumb = photo(im)
            except Exception:  # noqa: BLE001
                thumb = None
            border = ACCENT if index == st["shown"] else BG
            cell = tk.Label(
                row,
                image=thumb,
                text=str(index + 1),
                compound="top",
                fg=TEXT,
                bg=border,
                bd=0,
                padx=2,
                pady=2,
                cursor="hand2",
            )
            cell.pack(side="left", padx=(0, 6))
            cell.bind("<Button-1>", lambda _e, i=index: self.pick_variation(i))
            photos.append(thumb)
        self._images = photos + self._images[:4]
        self.thumbs.pack(fill="x", pady=(8, 0), before=self.job_buttons)

    def run(self, alive) -> None:
        """Tk's loop until ``alive()`` turns false (PhotoCraft closed)."""

        def check() -> None:
            if not alive():
                self._save_prefs()
                self.root.destroy()
                return
            self.root.after(1000, check)

        self.root.after(1000, check)
        self.root.mainloop()
        self._save_prefs()


def tk_available() -> bool:
    """True when Tk can open a window here (Python built with Tk, and a display)."""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - no Tk or no display
        return False
