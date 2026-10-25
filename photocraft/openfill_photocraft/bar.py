# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The OpenFill panel: a window without a title bar that stays on top of PhotoCraft, like
Photoshop's contextual task bar. Its header (OpenFill and the engine's status, the gear for
Models and settings, and the collapse control) is the drag handle. Below it: the mode
(Fill, Remove, Expand, Create), the prompt and options, the job card with the live preview
and the variations, and the action buttons at the bottom. Collapsed, it is a small strip
in PhotoCraft's corner.

Tk runs on the main thread; the job runs in the service's worker thread, and slower
look-ups (models on disk, ComfyUI) in a refresher thread. The panel only reads their
results.
"""

from __future__ import annotations

import io
import json
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import ui
from .menu import Watcher
from .modelinfo import hardware_line, needed_by
from .service import Service
from .ui import ACCENT, BAD, BG, CW, DIM, LABEL_W, LINE, OK, PAD, PANEL, TEXT, WARN, W, photo

WIDTH = W
PREVIEW_MAX = (CW - 24, 230)
THUMB_H = 72

TABS = (("fill", "Fill"), ("remove", "Remove"), ("expand", "Expand"), ("txt2img", "Create"))
CREATE_SIZES = (("Square", 1024, 1024), ("Landscape", 1216, 832), ("Portrait", 832, 1216))
STARTER = ("lama-big", "dreamshaper-8-inpainting")
HEAD_H = 45  # the header and its divider
FOOTER_H = 65  # the action bar: divider, 12 px, a 36 px button, 16 px
GEAR_BAR_H = 44  # Models and settings' section control: 12 px and 32 px
CREATE_TOTAL = 474  # the Create tools, as on the board: Models and settings is about this tall
GO = {"fill": "Generate", "remove": "Remove", "expand": "Expand", "txt2img": "Create"}
PLACEHOLDERS = {
    "fill": "Describe what to add, or leave it empty to fill from the surroundings.",
    "expand": "Optional: describe what the new area shows.",
    "txt2img": "Describe the image you want.",
}
NO_SELECTION = "Select an area first: Marquee, Lasso or Object Selection."
NO_MODELS = "No models installed"
STATUS_COLOR = {"Ready": OK, "Not set up": WARN, "Checking...": DIM, "Starting the AI engine...": WARN}


def icon_image(size: int = 64) -> Image.Image:
    """OpenFill's window icon: a white four-point sparkle on a blue rounded square."""
    return ui.app_icon_image(size)


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


def short_name(name: str) -> str:
    """A model's name without its parenthesised details."""
    return name.split(" (")[0].strip() or name


# LaMa and MAT give the same result for the same input: one variation is enough.
PROMPT_FREE = ("lama-big", "mat-places512")


def removal_choices(facts: dict[str, Any]) -> dict[str, str]:
    """Remove object's model menu: label -> Service removal value, default first.

    With LaMa (or MAT) installed the default is LaMa at the image's own resolution
    ("default"); then every other installed model that can remove, used as picked: MAT,
    the erasers, and each generation model (with a background-only prompt)."""
    models = facts.get("models") or {}
    removers = list(models.get("remove", []))
    ids = {model_id for model_id, _name in removers}
    painters = [(i, n) for i, n in models.get("fill", []) if i not in ids]
    active = facts.get("active") or ""
    painters.sort(key=lambda m: m[0] != active)
    out: dict[str, str] = {}
    default = next((m for m in PROMPT_FREE if m in ids), "")
    if default:
        name = next(n for i, n in removers if i == default)
        out[f"{short_name(name)} (default)"] = "default"
    for model_id, name in removers + painters:
        if model_id == default:
            continue
        label = short_name(name)
        while label in out:
            label += " "
        out[label] = model_id
    return out or {"The active model": "default"}


def fill_choices(facts: dict[str, Any]) -> dict[str, str]:
    """Fill's model menu: every installed fill model, then the removal models (with an
    empty prompt they remove; LaMa and MAT ignore a prompt, erasers use their own)."""
    models = facts.get("models") or {}
    out = {name: model_id for model_id, name in models.get("fill", [])}
    for model_id, name in models.get("remove", []):
        if model_id not in out.values():
            out.setdefault(name, model_id)
    return out


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
    """Slow facts for the panel, looked up off the UI thread every few seconds."""

    CACHED = ("comfy_ok", "models", "active", "active_name", "rows", "hardware")

    def __init__(self, service: Service, cache: Path | None = None) -> None:
        self.service = service
        self.cache = cache
        self.facts: dict[str, Any] = self._load_cache()
        self._hardware: str | None = self.facts.get("hardware") or None
        self._saved = ""
        self._preflight: dict[str, Any] | None = None  # what a ComfyUI download would be (Windows)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._loop, name="openfill-bar-refresh", daemon=True).start()

    def _load_cache(self) -> dict[str, Any]:
        """The facts of the last run, so Models and the model list show at once; the first
        look replaces them a moment later."""
        if self.cache is None:
            return {}
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or not data.get("comfy_ok"):
            return {}
        data["models"] = {k: [tuple(m) for m in v] for k, v in (data.get("models") or {}).items()}
        for row in data.get("rows") or []:
            if str(row.get("statusText", "")).startswith(
                ("Downloading", "Waiting", "Removing", "Installing", "Working")
            ):
                row["statusText"] = ""
        data["cached"] = True
        return data

    def _save_cache(self, facts: dict[str, Any]) -> None:
        if self.cache is None or not facts.get("rows"):
            return
        text = json.dumps({k: facts.get(k) for k in self.CACHED})
        if text != self._saved:
            try:
                self.cache.write_text(text, encoding="utf-8")
                self._saved = text
            except OSError:
                pass

    def get(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.facts)

    def _loop(self) -> None:
        while True:
            try:
                facts = self.look()
            except Exception as err:  # noqa: BLE001 - shown on the panel
                facts = {"error": str(err)}
            with self.lock:
                self.facts = facts
            self._save_cache(facts)
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
            if sys.platform == "win32":
                try:
                    facts["setup"] = self.service.setup_progress()
                    if facts["setup"] is None and comfy_layout(app.settings.comfy_dir) is not None:
                        return self.look()  # the download just finished: ComfyUI is there now
                    if self._preflight is None:
                        self._preflight = self.service.comfy_preflight()
                    facts["preflight"] = self._preflight
                except Exception as err:  # noqa: BLE001 - the card offers the folder choice anyway
                    facts["setup"] = {"error": str(err)}
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
            current=current,
            pending=pending,
        )
        busy = [r for r in rows if r["id"] == current.get("model_id") or r["id"] in pending]
        if busy:
            facts["downloading"] = "; ".join(f"{r['name']}: {cat.row_status(r, current, pending)}" for r in busy)
        names = {r["id"]: r["name"] for r in rows}
        if current.get("type") == "download" and current.get("model_id") in names:
            total = current.get("total") or 0
            facts["download"] = {
                "id": current["model_id"],
                "name": names[current["model_id"]],
                "fraction": (current.get("done") or 0) / total if total else 0.0,
            }
        else:
            waiting = [
                t for t in app.server.pending_tasks() if t.get("type") == "download" and t.get("model_id") in names
            ]
            if waiting:
                facts["download"] = {
                    "id": waiting[0]["model_id"],
                    "name": names[waiting[0]["model_id"]],
                    "fraction": 0.0,
                }
        facts["starter_missing"] = [r["id"] for r in rows if r["id"] in STARTER and not r["installed"]]
        for row in rows:
            row["statusText"] = cat.row_status(row, current, pending)
            if not row["runnable"]:
                row["neededBy"] = needed_by(list(app.catalog), row["id"])
        facts["rows"] = rows
        if self._hardware is None:
            try:
                from openfill import hardware

                self._hardware = hardware_line(hardware.detect(app.models_root, app.server.url()))
            except Exception:  # noqa: BLE001 - only shown in the Models view
                self._hardware = ""
        facts["hardware"] = self._hardware
        return facts


def cover(image: Image.Image, width: int, height: int, radius: int = 0, bg: str = PANEL) -> Image.Image:
    """``image`` scaled to cover ``width`` x ``height`` (cropped in the middle), corners rounded on ``bg``."""
    im = image.convert("RGB")
    scale = max(width / im.width, height / im.height)
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = im.resize((w, h), Image.LANCZOS)
    left, top = (w - width) // 2, (h - height) // 2
    im = im.crop((left, top, left + width, top + height))
    return rounded(im, radius, bg) if radius else im


def rounded(im: Image.Image, radius: int, bg: str) -> Image.Image:
    mask = Image.new("L", (im.width * 4, im.height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width * 4 - 1, im.height * 4 - 1), radius=radius * 4, fill=255)
    mask = mask.resize(im.size, Image.BOX)
    base = Image.new("RGB", im.size, bg)
    base.paste(im, (0, 0), mask)
    return base


class Bar:
    """The window. ``show(action)`` is what the Filter > Plug-ins entry calls."""

    def __init__(self, service: Service, watcher: Watcher, page_url: str, profile: Path) -> None:
        self.service, self.watcher, self.page_url, self.profile = service, watcher, page_url, profile
        self.settings_file = profile / "bar.json"
        self.prefs = self._load_prefs()
        self.calls: queue.Queue = queue.Queue()  # actions from other threads
        self.refresher = Refresher(service, profile / "bar-models.json")
        self.wrap = CW
        self.colors = {"BG": BG, "OK": OK, "BAD": BAD, "DIM": DIM, "TEXT": TEXT, "WARN": WARN}
        self.view = ""  # "" (the tools) or "settings" (Models and settings)
        self.root = tk.Tk(className="OpenFill")
        self.root.title("OpenFill")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.frameless = ui.make_borderless(self.root)  # before it is tied to PhotoCraft or shown
        self.root.attributes("-topmost", True)  # dropped when tied to PhotoCraft (Windows)
        self.root.protocol("WM_DELETE_WINDOW", self.collapse)  # closing folds it back to the strip
        ui.own_tag(self.root).bind_class(ui.own_tag_name(self.root), "<Unmap>", self._unmapped, add="+")
        self.fonts = ui.fonts(self.root)
        self.small = self.fonts.small
        self.bold = self.fonts.bold
        try:
            self._icon = photo(icon_image())
            self.root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001 - the window works without an icon
            pass
        from .tether import Tether

        self.tether = Tether()
        self.tab = tk.StringVar(value="fill")
        self.variations = tk.IntVar(value=max(1, min(5, int(self.prefs.get("variations", 1)))))
        self.model_choice = tk.StringVar(value="")
        self.target = tk.StringVar(value="image")
        self.size_name = tk.StringVar(value="Square")
        self._preview_seq = -1
        self._images: list[Any] = []  # keep PhotoImages alive
        self._thumbs_for: tuple = ()
        self._model_ids: dict[str, str] = {}
        self._last_state = ""
        self._note: tuple[str, str, str] | None = None  # (text, colour, the context it replaced)
        self._shown: dict[str, Any] = {}  # what each part last showed, so a tick changes only what changed
        self._build()
        self._place()
        self.root.after(150, self._tick)
        self.root.after(30, self._drain)

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

    def _build(self) -> None:
        root, f = self.root, self.fonts
        self._build_header()
        ui.divider(root).pack(fill="x")
        self.gear_bar = ui.frame(root)  # Models / Engine / Generation, shown with the gear view
        # The body scrolls as a whole when the screen is too short for it (no scroll bar).
        # The window's size is set by the panel (one geometry change per switch, see _resize);
        # the body takes what the header and the action bar leave.
        self.footer = ui.frame(root)
        self.footer.pack(side="bottom", fill="x")
        self.scroller = tk.Canvas(root, width=W, height=10, bg=BG, bd=0, highlightthickness=0, yscrollincrement=1)
        self.scroller.pack(fill="both", expand=True)
        self.inner = ui.frame(self.scroller)
        self._inner_item = self.scroller.create_window(0, 0, window=self.inner, anchor="nw", width=W)
        self.inner.bind("<Configure>", lambda _e: self._inner_changed())
        root.bind_all("<MouseWheel>", self._wheel, add="+")
        root.bind_all("<Button-4>", self._wheel, add="+")
        root.bind_all("<Button-5>", self._wheel, add="+")
        try:  # Tk 9: precise touchpad scrolling
            root.bind_all("<TouchpadScroll>", self._wheel, add="+")
        except tk.TclError:
            pass
        self._bottom: int | None = None  # the bottom edge the panel keeps (where it opened from the strip)
        self._resize_soon = False
        self.main = ui.frame(self.inner)
        self.main.pack(fill="x", padx=PAD)
        self.bottom_gap = ui.gap(self.inner, 16)
        self.bottom_gap.pack(fill="x")

        m = self.main
        self.modes = ui.Seg(m, TABS, self.select_tab, CW, 13)
        self.tab_buttons = {key: _SegKey(self.modes, key) for key, _label in TABS}
        self.context_row = ui.frame(m)
        self.context_icon = tk.Label(self.context_row, bg=BG, bd=0, highlightthickness=0)
        self.context_icon.pack(side="left", anchor="n", pady=(1, 0))
        self.context = ui.small(self.context_row, "", wrap=CW - 20)
        self.context.pack(side="left", padx=(6, 0), fill="x")
        self.remove_line = ui.small(m, "Erases what you selected and fills in the background around it.")

        from .expand_view import ExpandView

        self.expand_view = ExpandView(self, m)
        self._thumb_busy = False
        self.prompt = ui.PromptBox(m, self.generate, self._recall)
        self._recall_at = -1

        # Create: size and destination
        self.create_rows = ui.frame(m)
        self.size_seg = ui.Seg(
            self._row(self.create_rows, "Size"),
            [(label, label) for label, _w, _h in CREATE_SIZES],
            self.pick_size,
            ui.FIELD_W,
            12,
        )
        self.size_seg.pack(side="left")
        self.size_buttons = {label: _SegKey(self.size_seg, label) for label, _w, _h in CREATE_SIZES}
        self.size_px = ui.small(self.create_rows, "", wrap=ui.FIELD_W, anchor="center")
        self.size_px.pack(fill="x", padx=(LABEL_W + 12, 0), pady=(4, 0))
        self.target_seg = ui.Seg(
            self._row(self.create_rows, "Result as", pady=(8, 0)),
            (("image", "New image"), ("layer", "Layer in this image")),
            self.pick_target,
            ui.FIELD_W,
            12,
        )
        self.target_seg.pack(side="left")
        self.target_buttons = {key: _SegKey(self.target_seg, key) for key in ("image", "layer")}

        # Options: variations and model
        self.options = ui.frame(m)
        self.var_seg = ui.Seg(
            self._row(self.options, "Variations"),
            [(n, str(n)) for n in range(1, 6)],
            self.pick_variations,
            ui.FIELD_W,
            12,
        )
        self.var_seg.pack(side="left")
        self.var_buttons = [_SegKey(self.var_seg, n) for n in range(1, 6)]
        self.model_box = ui.Select(
            self._row(self.options, "Model", pady=(8, 0)),
            ui.FIELD_W,
            command=self._model_picked,
            manage=lambda: self.select_view("models"),
        )
        self.model_box.pack(side="left")

        # The setup card (ComfyUI missing, no models, a download) and the job card
        self.setup = ui.Card(m)
        self._setup_key: tuple = ()
        self.job = ui.Card(m)
        j = self.job.inner
        head = ui.frame(j, PANEL)
        head.pack(fill="x")
        self.job_title = ui.text(head, "", f.bold, bg=PANEL)
        self.job_title.pack(side="left")
        self.job_model = ui.small(head, "", bg=PANEL, wrap=0)
        self.job_model.pack(side="right")
        self.progress = ui.Progress(j, CW - 24)
        self.progress.pack(fill="x", pady=(8, 0))
        self.job_text = ui.text(j, "", f.small, DIM, PANEL, CW - 24)
        self.job_text.pack(fill="x", pady=(8, 0))
        self.preview = tk.Label(j, bg=PANEL, bd=0, highlightthickness=0)
        self.thumbs = ui.frame(j, PANEL)
        ui.caption(self.thumbs, "Pick a variation", PANEL).pack(anchor="w")
        self.thumb_row = ui.frame(self.thumbs, PANEL)
        self.thumb_row.pack(fill="x", pady=(8, 0))
        self.thumb_cells: list[tk.Canvas] = []

        # Models and settings, behind the gear
        from .panels import GearView

        self.gear = GearView(self, self.inner)
        self.models_view = self.gear.models
        self.settings_view = self.gear

        # The action bar at the bottom
        ui.divider(self.footer).pack(fill="x")
        self.actions = ui.frame(self.footer)
        self.actions.pack(fill="x", padx=PAD, pady=(12, PAD))
        third = (CW - 16) // 3
        self.go = ui.Button(self.actions, "Generate", self.generate, "primary", CW)
        self.cancel_button = ui.Button(self.actions, "Cancel", self.service.cancel, "secondary", CW, 36)
        self.discard_button = ui.Button(self.actions, "Discard", lambda: self.keep("none"), "secondary", third, 36)
        self.keep_all_button = ui.Button(self.actions, "Keep all", lambda: self.keep("all"), "secondary", third, 36)
        self.keep_button = ui.Button(
            self.actions, "Keep", lambda: self.keep("selected"), "primary", CW - 2 * third - 16
        )
        self.save_button = ui.Button(self.actions, "Save", self.gear.save, "primary", CW)
        self._footer_key: tuple = ()

        self._build_pill()
        root.bind("<Escape>", lambda _e: self.service.cancel() if self.service.busy() else None)
        self.select_tab("fill")
        self.pick_variations(self.variations.get())
        self.pick_size("Square")
        self._update_target()

    def _row(self, parent: tk.Misc, label: str, pady=(0, 0)) -> tk.Frame:
        """A form row: an 84 px grey label, then the control (packed into the returned frame)."""
        row = ui.frame(parent)
        row.pack(fill="x", pady=pady)
        cell = ui.frame(row, width=LABEL_W, height=32)
        cell.pack_propagate(False)
        cell.pack(side="left")
        ui.small(cell, label, wrap=0).pack(side="left")
        holder = ui.frame(row)
        holder.pack(side="left", padx=(12, 0))
        return holder

    def _build_header(self) -> None:
        f = self.fonts
        head = self.header = ui.frame(self.root, width=W, height=44)
        head.pack_propagate(False)
        head.pack(fill="x")
        self.collapse_button = ui.IconButton(head, "collapse", self.collapse)
        self.collapse_button.pack(side="right", padx=(2, 10))
        self.gear_button = ui.IconButton(head, "gear", self.toggle_gear)
        self.gear_button.pack(side="right")
        self.head_tools = ui.frame(head)
        logo = tk.Label(self.head_tools, image=ui.app_icon(18, BG), bg=BG, bd=0, highlightthickness=0)
        logo.pack(side="left")
        name = ui.text(self.head_tools, "OpenFill", f.bold)
        name.pack(side="left", padx=(8, 0))
        self.status_dot = tk.Label(self.head_tools, bg=BG, bd=0, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(12, 0))
        self.status = ui.small(self.head_tools, "Checking...", wrap=0)
        self.status.pack(side="left", padx=(6, 0))
        self.head_view = ui.frame(head)
        ui.IconButton(self.head_view, "back", lambda: self.select_tab(self.tab.get())).pack(side="left")
        view_title = ui.text(self.head_view, "Models and settings", f.bold)
        view_title.pack(side="left", padx=(0, 0))
        self.head_tools.pack(side="left", padx=(PAD, 0), fill="y")
        ui.drag_handle(
            (head, self.head_tools, logo, name, self.status_dot, self.status, self.head_view, view_title),
            self.root,
            self._dragged,
        )

    def _build_pill(self) -> None:
        """The collapsed panel: a small strip in a corner of PhotoCraft; a click opens the panel."""
        self.pill = tk.Toplevel(self.root, bg=PANEL)
        self.pill.title("OpenFill")
        self.pill.resizable(False, False)
        self.pill.protocol("WM_DELETE_WINDOW", self.hide)
        ui.make_borderless(self.pill)  # a chip, not a window: it is tied to PhotoCraft's
        self.pill.attributes("-topmost", True)  # dropped when tied to PhotoCraft (Windows)
        self.pill.withdraw()
        self.pill_canvas = tk.Canvas(
            self.pill, width=236, height=36, bg=PANEL, bd=0, highlightthickness=0, cursor="hand2"
        )
        self.pill_canvas.pack()
        self.pill_canvas.bind("<ButtonPress-1>", self._pill_press)
        self.pill_canvas.bind("<B1-Motion>", self._pill_drag)
        self.pill_canvas.bind("<ButtonRelease-1>", self._pill_release)
        self.collapsed = False
        self._pill_grab: tuple[int, int, int, int] | None = None
        self._pill_shown: tuple = ()
        self._draw_pill("Checking...", DIM, None)

    def _draw_pill(self, status: str, color: str, fraction: float | None) -> None:
        key = (status, color, None if fraction is None else round(fraction, 3))
        if key == self._pill_shown:
            return
        self._pill_shown = key
        c, f = self.pill_canvas, self.fonts
        c.delete("all")
        c.create_rectangle(0, 0, 235, 35, outline=LINE, fill=PANEL)
        c.create_image(11, 9, image=ui.app_icon(18, PANEL), anchor="nw")
        x = 11 + 18 + 8
        c.create_text(x, 18, text="OpenFill", font=f.bold, fill=TEXT, anchor="w")
        x += f.bold.measure("OpenFill") + 8
        c.create_image(x, 15, image=ui.dot(color, PANEL), anchor="nw")
        x += 6 + 8
        c.create_text(x, 18, text=ui.ellipsize(status, f.small, 236 - 13 - x), font=f.small, fill=DIM, anchor="w")
        if fraction is not None:
            c.create_rectangle(1, 33, 1 + round(234 * fraction), 35, outline="", fill=ACCENT)
        self.pill_text = status

    def _pill_press(self, event) -> None:
        self._pill_grab = (event.x_root, event.y_root, self.pill.winfo_x(), self.pill.winfo_y())

    def _pill_drag(self, event) -> None:
        if self._pill_grab is not None:
            x0, y0, px, py = self._pill_grab
            self.pill.geometry(f"+{px + event.x_root - x0}+{py + event.y_root - y0}")

    def _pill_release(self, event) -> None:
        grab, self._pill_grab = self._pill_grab, None
        if grab is None or (abs(event.x_root - grab[0]) < 4 and abs(event.y_root - grab[1]) < 4):
            self.expand()  # a click
            return
        self.pill.update_idletasks()  # a drag: remember where, relative to PhotoCraft
        right, bottom = self._anchor()
        self.prefs["pill_off"] = [
            right - (self.pill.winfo_x() + self.pill.winfo_width()),
            bottom - (self.pill.winfo_y() + self.pill.winfo_height()),
        ]

    # scrolling -----------------------------------------------------------------------

    def _max_body(self) -> int:
        return max(200, self.root.winfo_screenheight() - HEAD_H - GEAR_BAR_H - FOOTER_H - 64)

    def _footer_shown(self) -> bool:
        return bool(self._footer_key)

    def _body_height(self) -> int:
        """The body's height: the tools' parts as packed (each part's size is already known,
        so nothing is laid out again for this), or the gear view's fixed height."""
        if self.view:
            return self._gear_total() - HEAD_H - GEAR_BAR_H - (FOOTER_H if self._footer_shown() else 0)
        parts = self._shown.get("parts") or []
        return sum(p.winfo_reqheight() + 12 for p in parts) + 16

    def _gear_total(self) -> int:
        """Models and settings keep one window height for all three sections: as tall as the
        tallest of Engine and Generation, and at least the Create tools."""
        if not getattr(self, "_gear_h", 0):
            self.root.update_idletasks()
            sections = max(self.gear.engine.winfo_reqheight(), self.gear.generation.winfo_reqheight())
            settings = HEAD_H + GEAR_BAR_H + sections + 16 + FOOTER_H
            self._gear_h = max(settings, CREATE_TOTAL)
        return self._gear_h

    def _target_height(self) -> int:
        body = min(self._body_height(), self._max_body())
        return HEAD_H + (GEAR_BAR_H if self.view else 0) + body + (FOOTER_H if self._footer_shown() else 0)

    def _resize(self) -> None:
        """Give the window its height in one geometry change, keeping its bottom edge (the
        corner it opened from at the strip) where it is."""
        self._resize_soon = False
        h = self._target_height()
        if getattr(self, "collapsed", True) or not self.root.winfo_viewable():
            self.root.geometry(f"{W}x{h}")
            return
        x, y, cur = self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_height()
        if self._bottom is None:
            self._bottom = y + cur
        limit = self.root.winfo_screenheight() - 56  # above a taskbar
        bottom = min(self._bottom, limit)
        # a mode taller than the room above the anchor (Expand on a short screen) starts at the
        # top of the screen while it is shown; the next one is back on the anchor
        new_y = max(0, bottom - h)
        if (cur, y) == (h, new_y):
            return
        self._placing = True
        self.root.geometry(f"{W}x{h}+{x}+{new_y}")
        self._placing = False

    def _inner_changed(self) -> None:
        """The body's contents changed size (a job card, a wrapped line, the models list)."""
        want = self.inner.winfo_reqheight()
        self.scroller.configure(scrollregion=(0, 0, W, want))
        if not self.view and not self._resize_soon:
            self._resize_soon = True
            self.root.after_idle(self._resize)

    def _wheel(self, event) -> str | None:
        try:
            if event.widget.winfo_toplevel() is not self.root:
                return None
        except (KeyError, AttributeError, tk.TclError):
            return None
        if self.inner.winfo_reqheight() <= self.scroller.winfo_height():
            return None
        num, delta = getattr(event, "num", 0), getattr(event, "delta", 0)
        if num == 4:
            px = -40
        elif num == 5:
            px = 40
        elif str(event.type) in ("TouchpadScroll", "39"):
            try:
                _dx, dy = self.root.tk.splitlist(self.root.tk.call("tk::PreciseScrollDeltas", delta))
                px = -int(float(dy))
            except tk.TclError:
                return None
        elif sys.platform == "darwin":
            px = -delta * 8
        elif delta:  # Windows (and Tk 9 on X11): 120 per notch; a touchpad sends smaller steps
            px = -round(delta * 40 / 120) or (-1 if delta > 0 else 1)
        else:
            return None
        if px:
            self.scroller.yview_scroll(px, "units")
        return None

    # where the windows go -------------------------------------------------------------

    def _anchor(self) -> tuple[int, int]:
        """The corner OpenFill's windows are placed from: PhotoCraft's bottom right (tied), else
        the primary monitor's."""
        if self.tied and self._pc_rect:
            return self._pc_rect[2], self._pc_rect[3]
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        mx, my, mw, mh = primary_monitor() or (0, 0, sw, sh)
        return mx + mw, my + mh

    def _default_pill_off(self) -> list[int]:
        return [360, 52] if self.tied else [40, 70]  # left of PhotoCraft's panels, above its status bar

    def _clamp(self, x: int, y: int, w: int, h: int) -> tuple[int, int]:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return max(0, min(x, sw - w)), max(0, min(y, sh - h))

    def _place_pill(self) -> None:
        right, bottom = self._pill_corner()
        self._placing = True
        self.pill.geometry(f"+{right - self.pill.winfo_reqwidth()}+{bottom - self.pill.winfo_reqheight()}")
        self._placing = False

    def _place_bar(self) -> None:
        """The full panel opens from the strip: its bottom right where the strip's is."""
        w, h = W, self._target_height()
        right, bottom = self._pill_corner()
        x, y = self._clamp(right - w, bottom - h, w, h)
        self._bottom = bottom
        self._placing = True
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._placing = False

    def _pill_corner(self) -> tuple[int, int]:
        """The strip's bottom right on screen (where it is, or where it would go)."""
        self.pill.update_idletasks()
        w, h = self.pill.winfo_reqwidth(), self.pill.winfo_reqheight()
        dx, dy = self.prefs.get("pill_off") or self._default_pill_off()
        right, bottom = self._anchor()
        x, y = self._clamp(right - dx - w, bottom - dy - h, w, h)
        return x + w, y + h

    def _place(self) -> None:
        self.tied = False
        self._pc_rect: tuple[int, int, int, int] | None = None
        self._placing = False
        self.root.bind_class(ui.own_tag_name(self.root), "<Configure>", self._moved, add="+")
        self.root.withdraw()
        self._gear_total()  # measured once now, while the window is hidden
        self._tie()
        if self.prefs.get("collapsed", True):
            self.collapse()
        else:
            self.expand()

    def _tie(self) -> None:
        """Tie both windows to PhotoCraft's main window when it can be found (Windows)."""
        if self.tied or not self.tether.available:
            return
        if self.tether.attach(self.root) and self.tether.attach(self.pill):
            self.tied = True
            self._pc_rect = self.tether.rect()
            for window in (self.root, self.pill):
                window.attributes("-topmost", False)  # above PhotoCraft only, not every app

    def _follow(self) -> None:
        """Keep the windows where they belong when PhotoCraft moves or resizes."""
        if not self.tether.available:
            return
        if not self.tied:
            self._tie()
            if self.tied:
                self._place_pill() if self.collapsed else self._place_bar()
            return
        rect = self.tether.rect()
        if rect is None or rect == self._pc_rect:
            return  # minimized (Windows hides owned windows itself) or unchanged
        old, self._pc_rect = self._pc_rect, rect
        if old is None:
            return
        if self.collapsed:
            self._place_pill()
        else:
            dx, dy = rect[2] - old[2], rect[3] - old[3]
            if self._bottom is not None:
                self._bottom += dy
            self._placing = True
            self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")
            self._placing = False

    def _keep_on_screen(self) -> None:
        """The panel grows (a job, a view): move it up rather than past the bottom of the screen."""
        if getattr(self, "collapsed", True) or not self.root.winfo_viewable():
            return
        sh = self.root.winfo_screenheight()
        bottom = self.root.winfo_y() + self.root.winfo_height()
        limit = sh - 56  # above a taskbar
        if bottom > limit:
            self._placing = True
            self.root.geometry(f"+{self.root.winfo_x()}+{max(0, limit - self.root.winfo_height())}")
            self._placing = False

    def _moved(self, event) -> None:
        """The panel may be dragged by its header while open; it opens from the strip again next time."""

    def _dragged(self) -> None:
        """Dragged by its header: the panel keeps its new bottom edge from now on."""
        self._bottom = self.root.winfo_y() + self.root.winfo_height()

    def _unmapped(self, event) -> None:
        """Minimizing the panel folds it back to the strip (not when PhotoCraft hides it)."""
        if event.widget is self.root and not self.collapsed:
            self.root.after(10, lambda: self.root.state() == "iconic" and self.collapse())

    def _fade(self, window, start: float, end: float, then=None, steps: int = 5, ms: int = 18) -> None:
        """Fade a window's opacity from ``start`` to ``end`` (a few frames), then ``then()``."""

        def step(i: int) -> None:
            try:
                window.attributes("-alpha", start + (end - start) * i / steps)
            except tk.TclError:
                pass
            if i < steps:
                window.after(ms, step, i + 1)
            elif then:
                then()

        step(1)

    def _reveal(self, window, place) -> None:
        """Show ``window`` already laid out and in place, then fade it in (no jump, no pop-in)."""
        try:
            window.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        window.deiconify()
        place()
        window.update_idletasks()
        window.update()
        place()  # Tk may move a window on its first map: put it back while it is still invisible
        window.lift()
        self._fade(window, 0.0, 1.0)

    def collapse(self) -> None:
        if self.collapsed and self.pill.winfo_viewable():
            return
        self.model_box.close()
        was_open = not self.collapsed and self.root.winfo_viewable()
        self.collapsed = True
        self.prefs["collapsed"] = True

        def show_strip() -> None:
            self.root.withdraw()
            self._reveal(self.pill, self._place_pill)

        if was_open:
            self._fade(self.root, 1.0, 0.0, show_strip, steps=3)
        else:
            show_strip()

    def expand(self) -> None:
        if not self.collapsed and self.root.winfo_viewable():
            self.root.lift()
            return
        self.collapsed = False
        self.prefs["collapsed"] = False
        self._update()  # lay the contents out before it shows
        self.pill.withdraw()
        self._reveal(self.root, self._place_bar)

    # actions ------------------------------------------------------------------------

    def call_soon(self, fn, *args) -> None:
        """Run ``fn`` on the UI thread (for other threads); picked up within 30 ms."""
        self.calls.put((fn, args))

    def _drain(self) -> None:
        try:
            while True:
                fn, args = self.calls.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        finally:
            self.root.after(30, self._drain)

    def show(self, action: str = "") -> None:
        """Bring the panel up; a menu action also picks its mode (Remove starts at once)."""
        self.expand()
        if action in dict(TABS):
            self.select_tab(action)
        elif self.view:
            self.select_tab(self.tab.get())
        if action == "remove":
            self.generate()
        elif action in ("fill", "txt2img", "expand", "", "panel"):
            self.root.focus_force()
            self.prompt.focus()
            self.prompt.select_all()

    def hide(self) -> None:
        self._save_prefs()
        self.root.withdraw()
        self.pill.withdraw()
        self.collapsed = False
        if not self.prefs.get("told_reopen"):
            self.prefs["told_reopen"] = True
            self._save_prefs()
            ui.message(
                self.root,
                "OpenFill keeps running while PhotoCraft is open.\n\n"
                "To bring it back, use Filter > Plug-ins > OpenFill in PhotoCraft.",
            )

    def toggle_gear(self) -> None:
        if self.view:
            self.select_tab(self.tab.get())
        else:
            self.select_view(self.gear.section)

    def select_view(self, view: str) -> None:
        with ui.frozen(self.root):
            self._select_view(view)

    def select_tab(self, key: str) -> None:
        with ui.frozen(self.root):
            self._select_tab(key)

    def _select_view(self, view: str) -> None:
        """Models and settings in place of the tools: ``view`` is its section (models, engine,
        generation; "settings" is the engine). The same section again goes back."""
        section = {"settings": "engine"}.get(view, view)
        if self.view and self.gear.section == section and view == self._last_view:
            self.select_tab(self.tab.get())
            return
        self._last_view = view
        self.model_box.close()
        entering = not self.view
        self.view = "settings"
        if entering:
            self.gear.load()  # the saved settings, once per visit (switching sections keeps edits)
        self.main.pack_forget()
        self.gear.pack(fill="x", padx=PAD, before=self.bottom_gap)
        self.head_tools.pack_forget()
        self.head_view.pack(side="left", padx=(PAD, 0), fill="y")
        self.gear_bar.pack(fill="x", before=self.scroller)
        self.gear_button.set_active(True)
        self.gear.show_section(section)
        self.refresher.wake.set()
        self._layout_footer()
        self.scroller.yview_moveto(0)
        self._resize()

    _last_view = ""

    def _select_tab(self, key: str) -> None:
        self.model_box.close()
        if self.view:
            self.view = ""
            self._last_view = ""
            self.gear.pack_forget()
            self.main.pack(fill="x", padx=PAD, before=self.bottom_gap)
            self.head_view.pack_forget()
            self.gear_bar.pack_forget()
            self.head_tools.pack(side="left", padx=(PAD, 0), fill="y")
            self.gear_button.set_active(False)
        if key != self.tab.get():
            self._note = None
        self.tab.set(key)
        self.modes.set(key)
        if key in PLACEHOLDERS:
            self.prompt.set_placeholder(PLACEHOLDERS[key])
        self.go.set(label=GO[key])
        self._layout_main()
        self._fill_models()
        self._update_context()
        self._layout_footer()
        self.scroller.yview_moveto(0)
        self._resize()

    def _layout_main(self) -> None:
        """Pack the tool view's parts for the current mode (12 px apart)."""
        key = self.tab.get()
        parts: list[tk.Misc] = [self.modes, self.context_row]
        if key == "remove":
            parts.append(self.remove_line)
        if key == "expand":
            parts.append(self.expand_view)
        if key in ("fill", "expand", "txt2img"):
            parts.append(self.prompt)
        if key == "txt2img":
            parts.append(self.create_rows)
        parts.append(self.options)
        if self._shown.get("setup"):
            parts.append(self.setup)
        if self._shown.get("job"):
            parts.append(self.job)
        old = self._shown.get("parts") or []
        if parts == old:
            return
        self._shown["parts"] = parts
        prefix = 0  # the parts that stay where they are
        while prefix < min(len(old), len(parts)) and old[prefix] is parts[prefix]:
            prefix += 1
        for w in old[prefix:]:
            w.pack_forget()
        for w in parts[prefix:]:
            w.pack(fill="x", pady=(12, 0), anchor="w")
        for card in (self.setup, self.job):
            card.lift_corners()

    def _layout_footer(self) -> None:
        if self.view:
            section = self.gear.section
            buttons = [] if section == "models" else [self.save_button]
        else:
            st = self.service.state.snapshot()
            if st["state"] == "running":
                buttons = [self.cancel_button]
            elif st["state"] == "done" and len(st["variations"]) > 1:
                buttons = [self.discard_button, self.keep_all_button, self.keep_button]
            else:
                buttons = [self.go]
        key = tuple(id(b) for b in buttons)
        if key == self._footer_key:
            return
        self._footer_key = key
        for b in self.actions.pack_slaves():
            b.pack_forget()
        if not buttons:
            self.footer.pack_forget()
        else:
            if not self.footer.winfo_manager():
                self.footer.pack(side="bottom", fill="x", before=self.scroller)
            for i, b in enumerate(buttons):
                b.pack(side="left", padx=(8 if i else 0, 0))
        if not self.view:
            self._inner_changed()

    def pick_variations(self, n: int) -> None:
        self.variations.set(n)
        self.var_seg.set(n)
        self.prefs["variations"] = n

    def _fetch_thumbnail(self, key: tuple) -> None:
        """The image for the Expand editor, read once per document size (off the UI thread)."""
        self._thumb_busy = True

        def work() -> None:
            try:
                image = self.service.bridge.active_image()
                if image is not None:
                    visible, _selection = self.service.bridge._snapshot(image)
                    thumb = visible.copy()
                    thumb.thumbnail((420, 420))
                    if self.expand_view.image_key == key:
                        self.call_soon(self.expand_view.set_thumbnail, thumb)
            except Exception:  # noqa: BLE001 - the editor works without a thumbnail
                pass
            finally:
                self._thumb_busy = False

        threading.Thread(target=work, daemon=True).start()

    def pick_size(self, label: str) -> None:
        self.size_name.set(label)
        self.size_seg.set(label)
        _, w, h = next(s for s in CREATE_SIZES if s[0] == label)
        self.size_px.configure(text=f"{w} x {h} px")

    def pick_target(self, value: str) -> None:
        self.target.set(value)
        self._update_target()

    def toggle_target(self) -> None:
        self.pick_target("layer" if self.target.get() == "image" else "image")

    def _update_target(self) -> None:
        self.target_seg.set(self.target.get())

    def _model(self) -> str:
        return self._model_ids.get(self.model_choice.get(), "active")

    def _model_picked(self, label: str) -> None:
        """The Model select: the model picked becomes the active one (Remove keeps its own
        choice; a removal model picked in Fill is used for that run only)."""
        self.model_choice.set(label)
        if self.tab.get() == "remove":
            self.prefs["removal"] = label
            return
        model_id = self._model_ids.get(label)
        facts = self.refresher.get()
        removers = {i for i, _n in (facts.get("models") or {}).get("remove", [])}
        if model_id and model_id != facts.get("active") and model_id not in removers:

            def activate() -> None:
                self.service.app.set_active(model_id)
                self.refresher.wake.set()

            threading.Thread(target=self._safe, args=(activate,), daemon=True).start()

    def generate(self) -> None:
        if self.service.busy() or self.go.disabled:
            return
        kind, prompt = self.tab.get(), self.prompt.get().strip()
        body: dict[str, Any] = {"variations": self.variations.get(), "seed": -1, "model": self._model()}
        try:
            if kind in ("fill", "remove"):
                if not (self.watcher.snapshot().get("selection")):
                    self._say(NO_SELECTION, BAD)
                    return
                body.update(
                    prompt="" if kind == "remove" else prompt, remove=kind == "remove", strength=1.0, negative=""
                )
                if kind == "remove":
                    removal = self._model_ids.get(self.model_choice.get(), "default")
                    body.update(removal=removal)
                    if removal in (*PROMPT_FREE, "default"):
                        body.update(variations=1)  # same input, same result: one is enough
                self.service.start("fill", body)
            elif kind == "expand":
                left, top, right, bottom = self.expand_view.margins()
                if not any((left, top, right, bottom)):
                    self._say("Drag the frame's edges outward, or pick a ratio.", BAD)
                    return
                body.update(prompt=prompt, negative="", left=left, top=top, right=right, bottom=bottom)
                self.expand_view.begin_job()
                try:
                    self.service.start("expand", body)
                except Exception:
                    self.expand_view.end_job(False)
                    raise
            else:
                if not prompt:
                    self._say("Describe the image first.", BAD)
                    return
                _, w, h = next(s for s in CREATE_SIZES if s[0] == self.size_name.get())
                if self.target.get() == "layer" and self.watcher.snapshot().get("selection"):
                    w = h = 0  # a layer sized to the selection and placed on it, as in OpenFill for GIMP
                body.update(prompt=prompt, negative="", width=w, height=h, target=self.target.get())
                self.service.start("txt2img", body)
        except Exception as err:  # noqa: BLE001
            self._say(str(err), BAD)
            return
        self._say("")
        self._thumbs_for = ()
        self._remember(prompt)
        self._save_prefs()
        self._update()

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
        self.prompt.set(past[self._recall_at] if self._recall_at >= 0 else "")
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
        self.select_view("models")

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

    def download_comfy(self) -> None:
        """Download and install ComfyUI in the background (Windows); the setup card shows it."""

        def work() -> None:
            try:
                self.service.setup_comfy()
                self.call_soon(self._say, "Downloading ComfyUI. You can keep working in PhotoCraft.", OK)
            except Exception as err:  # noqa: BLE001 - shown on the panel
                self.call_soon(self._say, str(err), BAD)
            self.refresher.wake.set()

        threading.Thread(target=work, name="openfill-setup", daemon=True).start()

    def _comfy_help(self) -> None:
        webbrowser.open("https://github.com/beckettharriman/openfill/blob/main/docs/install.md#openfill-for-photocraft")

    # updates ----------------------------------------------------------------------

    def _say(self, text: str, color: str = OK) -> None:
        """A message: in the open settings section, or in place of the context line (red with
        an error mark for a problem) until what it was about changes."""
        if self.view and text:
            self.gear.say(text, color)
            return
        if not text:
            self._note = None
        else:
            self._note = (text, color, self._context_base()[0])
        self._update_context()

    def _fill_models(self) -> None:
        facts = self.refresher.get()
        tab = self.tab.get()
        models = facts.get("models") or {}
        if tab == "remove":
            has_any = bool(models.get("remove") or models.get("fill"))
            choices = removal_choices(facts) if has_any else {}
            wanted = self.prefs.get("removal", "")
        else:
            choices = fill_choices(facts) if tab == "fill" else {name: i for i, name in models.get(tab, [])}
            wanted = facts.get("active_name") or ""
        self._model_ids = choices
        names = list(choices)
        if self.model_box.values != names:
            self.model_box.set_values(names)
        current = self.model_choice.get()
        if current not in names:
            current = wanted if wanted in names else (names[0] if names else "")
            self.model_choice.set(current)
        if not facts:
            shown = "Checking..."
        elif not names:
            shown = NO_MODELS
        else:
            shown = current
        self.model_box.set(shown)
        busy = self.service.busy()
        self.go.set(disabled=busy or (bool(facts) and not names))

    def _context_base(self) -> tuple[str, str, str]:
        """(text, colour, icon) for the line under the modes."""
        snap = self.watcher.snapshot()
        doc, sel, tab = snap.get("document"), snap.get("selection"), self.tab.get()
        if not snap.get("connected"):
            return "Waiting for PhotoCraft...", DIM, "info"
        if snap.get("waiting"):
            return (
                "PhotoCraft is not answering. If its window is minimized or covered, bring it into view.",
                WARN,
                "warn",
            )
        if tab == "txt2img":
            if doc and sel and self.target.get() == "layer":
                return f"A layer in {doc['name']}, on the selection ({sel[2]} x {sel[3]} px)", DIM, "selection"
            return "Makes a new image" + (f", or a layer in {doc['name']}" if doc else ""), DIM, "image"
        if not doc:
            return "Open an image in PhotoCraft.", DIM, "image"
        if tab in ("fill", "remove"):
            if sel:
                return f"{doc['name']} · selection {sel[2]} x {sel[3]} px", DIM, "selection"
            return NO_SELECTION, DIM, "selection"
        if not self.expand_view.job_active:  # the canvas grows round by round while it runs
            self.expand_view.set_image(doc["name"], int(doc["width"]), int(doc["height"]))
        if self.expand_view.needs_thumbnail and not self._thumb_busy and not self.service.busy():
            self._fetch_thumbnail(self.expand_view.image_key)
        return f"{doc['name']} · drag the frame to the new canvas size", DIM, "image"

    def _update_context(self) -> None:
        text, color, mark = self._context_base()
        if self._note is not None:
            if self._note[2] != text:
                self._note = None  # what it was about changed (a selection was made, ...)
            else:
                text, color = self._note[0], self._note[1]
                mark = {BAD: "error", OK: "check", WARN: "warn"}.get(color, "info")
        key = (text, color, mark)
        if self._shown.get("context") != key:
            self._shown["context"] = key
            self.context.configure(text=text, fg=color)
            self.context_icon.configure(image=ui.icon(mark, color, 14, BG))

    def _tick(self) -> None:
        try:
            while True:
                fn, args = self.calls.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        try:
            self._follow()
            self._update()
            self._keep_on_screen()
        finally:
            self.root.after(250, self._tick)

    def _engine_status(self, facts: dict[str, Any]) -> str:
        engine = self.service.engine
        if not facts:
            return "Checking..."
        if not facts.get("comfy_ok"):
            return "Not set up"
        if engine == "starting":
            return "Starting the AI engine..."
        if engine in ("", "ready"):
            return "Ready"
        return "AI engine: " + engine[:60]

    def _update(self) -> None:
        facts = self.refresher.get()
        if not self.view:
            self._update_context()
            self._fill_models()
        status = self._engine_status(facts)
        if self._shown.get("status") != status:
            self._shown["status"] = status
            self.status.configure(text=ui.ellipsize(status, self.small, 200))
            self.status_dot.configure(image=ui.dot(STATUS_COLOR.get(status, DIM), BG))
        self._update_setup(facts)
        if facts.get("rows") is not self._shown.get("rows") or self.view:
            # new facts (a look every few seconds): the models list follows, shown or not
            self._shown["rows"] = facts.get("rows")
            self.gear.update_view(facts)

        st = self.service.state.snapshot()
        if self.collapsed:
            if st["state"] == "running":
                kind = "Remove" if (st["title"] or "").startswith(("Remove", "Object")) else None
                kind = kind or {"fill": "Fill", "expand": "Expand", "txt2img": "Create"}.get(st["kind"], "Working")
                self._draw_pill(f"{kind}: {int(st['fraction'] * 100)}%", ACCENT, st["fraction"])
            elif st["state"] == "done" and len(st["variations"]) > 1:
                self.expand()  # pick a variation
            else:
                self._draw_pill(status, STATUS_COLOR.get(status, DIM), None)
        self._update_job(st)
        self._layout_footer()
        if st["state"] != self._last_state and st["state"] in ("done", "error"):
            self.refresher.wake.set()
        self._last_state = st["state"]

    def _update_setup(self, facts: dict[str, Any]) -> None:
        """The setup card: ComfyUI not found, a download running, or no models yet."""
        key: tuple = ()
        setup = facts.get("setup") or {}
        pre = facts.get("preflight") or {}
        if facts and not facts.get("comfy_ok", True) and setup and "error" not in setup:
            key = (
                "setup",
                "download",
                ACCENT,
                "Setting up ComfyUI",
                "It downloads and unpacks in the background, which takes a few minutes. You can keep working.",
                (),
            )
        elif facts and not facts.get("comfy_ok", True) and pre and not pre.get("blocked"):
            line = (
                f"OpenFill runs on ComfyUI. Download it ({pre['size']}, into {pre['folder']}), "
                "or choose the folder of one you have."
            )
            if pre.get("cpu"):
                line += " No NVIDIA graphics card was found: it would run on the processor, which is very slow."
            if setup.get("error"):
                line = f"The download stopped: {setup['error']} " + line
            key = (
                "comfy",
                "warn",
                WARN,
                "AI engine not found",
                line,
                (
                    ("Download ComfyUI", "accent", self.download_comfy),
                    ("Choose folder", "secondary", self.choose_comfy),
                ),
            )
        elif facts and not facts.get("comfy_ok", True):
            line = "OpenFill runs on ComfyUI. Choose its folder (the one with main.py), or install it."
            if pre.get("blocked") and pre.get("text"):
                line = pre["text"] + " " + line
            key = (
                "comfy",
                "warn",
                WARN,
                "AI engine not found",
                line,
                (("Choose folder", "accent", self.choose_comfy), ("How to install", "secondary", self._comfy_help)),
            )
        elif facts.get("download"):
            d = facts["download"]
            key = ("download", "download", ACCENT, f"Downloading {short_name(d['name'])}", "You can keep working.", ())
        elif facts.get("comfy_ok") and not (facts.get("models") or {}).get("fill"):
            key = (
                "models",
                "warn",
                WARN,
                "No models installed",
                "Install at least one model to start. LaMa and DreamShaper 8 (2.2 GB) cover every tool.",
                (("Open model manager", "accent", lambda: self.select_view("models")),),
            )
        shown_key = (*key[:5], tuple(b[0] for b in key[5])) if key else ()
        if shown_key != self._setup_key:
            self._setup_key = shown_key
            self._build_setup(key)
        if key and key[0] in ("download", "setup"):
            fraction = (facts["download"] if key[0] == "download" else setup)["fraction"]
            self.setup_percent.configure(text=f"{int(fraction * 100)}%")
            self.setup_bar.set(fraction)
        was = self._shown.get("setup")
        self._shown["setup"] = bool(key)
        if was != bool(key) and not self.view:
            self._layout_main()

    def _build_setup(self, key: tuple) -> None:
        inner = self.setup.inner
        for child in inner.winfo_children():
            child.destroy()
        if not key:
            return
        kind, mark, color, title, line, buttons = key
        f = self.fonts
        head = ui.frame(inner, PANEL)
        head.pack(fill="x")
        tk.Label(head, image=ui.icon(mark, color, 16, PANEL), bg=PANEL, bd=0).pack(side="left")
        ui.text(head, title, f.bold, bg=PANEL).pack(side="left", padx=(8, 0))
        self.setup_percent = ui.small(head, "", bg=PANEL, wrap=0)
        self.setup_percent.pack(side="right")
        if kind in ("download", "setup"):
            self.setup_bar = ui.Progress(inner, CW - 24)
            self.setup_bar.pack(fill="x", pady=(8, 0))
        ui.small(inner, line, bg=PANEL, wrap=CW - 24).pack(fill="x", pady=(8, 0))
        if buttons:
            row = ui.frame(inner, PANEL)
            row.pack(fill="x", pady=(8, 0))
            n = len(buttons)
            width = (CW - 24 - 8 * (n - 1)) // n
            for i, (label, style, command) in enumerate(buttons):
                w = width if i < n - 1 else CW - 24 - (width + 8) * (n - 1)
                ui.Button(row, label, command, style, w, 32, PANEL).pack(side="left", padx=(8 if i else 0, 0))
        self.setup.lift_corners()

    def _update_job(self, st: dict[str, Any]) -> None:
        shown = st["state"] != "idle"
        if self._shown.get("job") != shown:
            self._shown["job"] = shown
            if not self.view:
                self._layout_main()
        if not shown:
            self._update_preview(st)
            return
        title = st["title"] or st["kind"]
        if self.job_title.cget("text") != title:
            self.job_title.configure(text=title)
        model = short_name(st["model"] or "")
        if self.job_model.cget("text") != model:
            self.job_model.configure(text=model)
        elapsed = int(st["elapsed"])
        clock = f"{elapsed // 60}:{elapsed % 60:02d}"
        several = len(st["variations"]) > 1
        if st["state"] == "running":
            text, color, font = f"{st['text']} · {clock}", DIM, self.small
        elif st["state"] == "done":
            text, color, font = f"Done in {clock}." + (" Pick one, then Keep." if several else ""), OK, self.fonts.body
        elif st["state"] == "cancelled":
            text, color, font = st["message"], DIM, self.fonts.body
        else:
            text, color, font = st["message"], BAD, self.fonts.body
        if (self.job_text.cget("text"), str(self.job_text.cget("fg"))) != (text, color):
            self.job_text.configure(text=text, fg=color, font=font)
        self.progress.set(st["fraction"], OK if st["state"] == "done" else ACCENT)
        self._update_preview(st)
        self._update_variations(st)

    def _update_preview(self, st: dict[str, Any]) -> None:
        in_editor = self.expand_view.job_active
        if st["state"] != "running":
            self.preview.pack_forget()
            if in_editor:
                self.expand_view.end_job(st["state"] == "done")
            return
        if st["previewSeq"] == self._preview_seq:
            return
        self._preview_seq = st["previewSeq"]
        with self.service.state.lock:
            data = self.service.state.preview
        if not data:
            self.preview.pack_forget()
            return
        if in_editor:  # the new area fills in where the frame was dragged
            try:
                with Image.open(io.BytesIO(data)) as im:
                    rect = st.get("previewRect")
                    self.expand_view.show_preview(im.copy(), tuple(rect) if rect else None)
            except Exception:  # noqa: BLE001 - a bad frame is skipped
                pass
            self.preview.pack_forget()
            return
        try:
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                im.thumbnail(PREVIEW_MAX)
                frame = photo(rounded(im, 4, PANEL))
        except Exception:  # noqa: BLE001 - a bad frame is skipped
            return
        self._images = [frame, *[i for i in self._images if i is not frame][:8]]
        self.preview.configure(image=frame)
        if not self.preview.winfo_ismapped():
            self.preview.pack(pady=(12, 0))
            self.job.lift_corners()

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
        for child in self.thumb_row.winfo_children():
            child.destroy()
        n = len(names)
        inner = CW - 24
        width = (inner - 8 * (n - 1)) / n
        photos = []
        self.thumb_cells = []
        f = self.fonts
        for index, layer in enumerate(self.service.layers[:n]):
            x0 = round(index * (width + 8))
            cw = round((index + 1) * (width + 8) - 8) - x0
            on = index == st["shown"]
            cell = tk.Canvas(self.thumb_row, width=cw, height=100, bg=PANEL, bd=0, highlightthickness=0, cursor="hand2")
            cell.pack(side="left", padx=(8 if index else 0, 0))
            if on:
                cell.create_image(0, 0, image=ui.rrect(cw, 80, 4, PANEL, PANEL, ACCENT, 2), anchor="nw")
            try:
                im = Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), layer.rgba)
                thumb = photo(cover(im, cw - 8, THUMB_H, 2))
                cell.create_image(4, 4, image=thumb, anchor="nw")
                photos.append(thumb)
            except Exception:  # noqa: BLE001
                pass
            cell.create_text(cw / 2, 92, text=str(index + 1), font=f.small, fill=TEXT if on else DIM, anchor="center")
            cell.bind("<Button-1>", lambda _e, i=index: self.pick_variation(i))
            self.thumb_cells.append(cell)
        self._images = photos + self._images[:4]
        if not self.thumbs.winfo_ismapped():
            self.thumbs.pack(fill="x", pady=(12, 0))
            self.job.lift_corners()

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


class _SegKey:
    """One choice of a segmented control, with ``invoke()`` (as a button had)."""

    def __init__(self, seg: ui.Seg, key: Any) -> None:
        self.seg, self.key = seg, key

    def invoke(self) -> None:
        self.seg.invoke(self.key)


def tk_available() -> bool:
    """True when Tk can open a window here (Python has Tk, and there is a display)."""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - no Tk or no display
        return False
