# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""The "ComfyUI is required" notice and the progress of downloading it.

Shown on first run and whenever Generative Fill, Remove or Expand is started
without a working ComfyUI. The logic (manifest, checks, detection, install
steps) lives in ``comfy_setup``; this module only draws it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import catalog as cat  # noqa: E402
from . import comfy_setup, ui_text  # noqa: E402
from .app import App  # noqa: E402
from .backend import BackendError  # noqa: E402

RESP_DOWNLOAD = 1
RESP_HAVE = 2
RESP_USE_FOUND = 3

STATE_TEXT = {
    "verifying": "Checking the download...",
    "unpacking": "Unpacking ComfyUI (a few minutes)...",
    "nodes": "Installing the inpaint nodes...",
    "packages": "Installing packages...",
}


def test_hooks() -> dict[str, Any]:
    """Hooks for GUI test runs: the JSON file named by OPENFILL_TEST_UI_FILE (same file as the other dialogs)."""
    import json
    import os

    path = os.environ.get("OPENFILL_TEST_UI_FILE")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dump_buttons(dialog: Gtk.Dialog, out_path: str) -> None:
    """Write the screen position of each button of ``dialog`` (centre, pixels) so a test can click it."""
    import json

    top_window = dialog.get_window()
    if top_window is None:
        return
    _ok, ox, oy = top_window.get_origin()
    buttons = {}
    for child in dialog.get_action_area().get_children():
        if isinstance(child, Gtk.Button):
            pos = child.translate_coordinates(
                dialog, child.get_allocated_width() // 2, child.get_allocated_height() // 2
            )
            if pos:
                buttons[child.get_label() or ""] = [ox + pos[0], oy + pos[1]]
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"title": dialog.get_title(), "origin": [ox, oy], "buttons": buttons}, fh)


def notice_title() -> str:
    """Heading of the notice."""
    return "ComfyUI is required"


def download_button_label(manifest: comfy_setup.SetupManifest, pre: comfy_setup.Preflight) -> str:
    """Label of the main button; says plainly when the download would run on the processor."""
    size = comfy_setup.download_notice(pre.manifest(manifest))
    if pre.needs_cpu_choice:
        if pre.gpu == "amd":
            return f"Download ComfyUI anyway ({size}, very slow: this card would not be used)"
        return f"Download ComfyUI anyway ({size}, very slow without an NVIDIA card)"
    if pre.amd_build:
        return f"Download ComfyUI for AMD ({size})"
    return f"Download ComfyUI ({size})"


def progress_text(current: dict[str, Any]) -> tuple[float, str]:
    """Bar fraction and step text for a ``setup_comfy`` task status."""
    state = str(current.get("state") or "")
    done, total = int(current.get("done") or 0), int(current.get("total") or 0)
    fraction = float(current.get("fraction") or comfy_setup.overall_fraction(state, done, total))
    if state == "downloading":
        return fraction, f"Downloading ComfyUI: {cat.human_size(done)} of {cat.human_size(total)}"
    if state == "unpacking" and total:
        return fraction, f"Unpacking ComfyUI: {cat.human_size(done)} of about {cat.human_size(total)}"
    return fraction, STATE_TEXT.get(state, "Getting ready...")


def _choose_folder(parent: Gtk.Window | None, title: str, start: str = "") -> str | None:
    chooser = Gtk.FileChooserDialog(title=title, transient_for=parent, action=Gtk.FileChooserAction.SELECT_FOLDER)
    chooser.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK)
    if start and Path(start).is_dir():
        chooser.set_current_folder(start)
    path = chooser.get_filename() if chooser.run() == Gtk.ResponseType.OK else None
    chooser.destroy()
    return path


class _Notice(Gtk.Dialog):
    def __init__(self, app: App, parent: Gtk.Window | None, message: str) -> None:
        super().__init__(title=notice_title(), transient_for=parent, modal=True)
        self.app = app
        self.install_dir = comfy_setup.default_install_dir()
        self.found = app.found_comfy()
        self.manifest = comfy_setup.load_manifest()
        self.pre: comfy_setup.Preflight | None = None
        self.set_default_size(560, -1)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(14)
        head = Gtk.Label(xalign=0)
        head.set_markup(f"<b>{GLib.markup_escape_text(notice_title())}</b>")
        box.pack_start(head, False, False, 0)
        text = Gtk.Label(label=message, xalign=0)
        text.set_line_wrap(True)
        box.pack_start(text, False, False, 0)
        self.folder_label = Gtk.Label(xalign=0)
        self.folder_label.set_line_wrap(True)
        row = Gtk.Box(spacing=6)
        row.pack_start(self.folder_label, True, True, 0)
        change = Gtk.Button(label="Change folder...")
        change.connect("clicked", self._change_folder)
        row.pack_start(change, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.checks = Gtk.Label(xalign=0)
        self.checks.set_line_wrap(True)
        self.checks.set_selectable(True)
        box.pack_start(self.checks, False, False, 0)
        if self.found:
            first = self.found[0]
            self.add_button(f"Use the ComfyUI found in {first.path}", RESP_USE_FOUND)
        self.download = self.add_button("Download ComfyUI", RESP_DOWNLOAD)
        self.add_button("I already have ComfyUI...", RESP_HAVE)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.set_default_response(RESP_DOWNLOAD if sys.platform == "win32" else RESP_HAVE)
        self.refresh()
        self.show_all()
        if sys.platform != "win32":
            # The download is the Windows build; the checks say how to install ComfyUI here instead.
            row.hide()
            self.download.hide()

    def refresh(self) -> None:
        self.folder_label.set_text(f"It will be installed in {self.install_dir}")
        self.manifest, self.pre = self.app.setup_preflight(self.install_dir)
        self.download.set_label(download_button_label(self.manifest, self.pre))
        self.download.set_sensitive(not self.pre.blocked)
        self.checks.set_text(self.pre.text())

    def _change_folder(self, *_: object) -> None:
        picked = _choose_folder(self, "Install ComfyUI in", str(self.install_dir.parent))
        if picked:
            self.install_dir = Path(picked) / "ComfyUI"
            self.refresh()


def _download_with_progress(app: App, install_dir: Path, cpu: bool, older_gpu: bool = False, amd: bool = False) -> bool:
    """Queue the setup task and show its progress until it ends. True when ComfyUI is ready."""
    from .progress_window import ProgressWindow

    task_id = app.queue_comfy_setup(install_dir, cpu, older_gpu, amd)
    win = ProgressWindow(
        "Setting up ComfyUI",
        "",
        on_cancel=app.server.request_cancel,
        position=(app.settings.progress_window_x, app.settings.progress_window_y),
        show_preview=False,
        quit_main_on_close=False,
    )
    result = {"ok": False}

    def tick() -> bool:
        try:
            status = app.task_status()
        except Exception as err:  # noqa: BLE001 - the status file is best effort
            app.log.debug("setup status poll failed: %s", err)
            return True
        current = status.get("current") or {}
        if current.get("task_id") == task_id:
            fraction, text = progress_text(current)
            win.set_step(fraction, text)
            return True
        last = next((h for h in status.get("history") or [] if h.get("task_id") == task_id), None)
        if last is None:
            win.set_step(0.0, "Getting ready...")
            return True
        result["ok"] = last.get("state") == "done"
        win.finish(
            result["ok"], "ComfyUI is ready" if result["ok"] else str(last.get("message") or last.get("state")), 1200
        )
        Gtk.main_quit()
        return False

    GLib.timeout_add(1000, tick)
    Gtk.main()
    if result["ok"]:
        app.reload_settings()
    return result["ok"] and app.comfy_ready()


def _apply_hooks(dialog: Gtk.Dialog) -> None:
    """Test hooks ``setup_dump`` (write the button positions) and ``setup_action`` (answer after ``setup_delay`` s)."""
    hooks = test_hooks()
    dump = hooks.get("setup_dump")
    if dump:
        GLib.timeout_add(1500, lambda: (dump_buttons(dialog, str(dump)), False)[1])
    action = hooks.get("setup_action")
    if action:
        response = {"download": RESP_DOWNLOAD, "cancel": Gtk.ResponseType.CANCEL}[str(action)]
        delay = int(float(hooks.get("setup_delay", 3)) * 1000)
        GLib.timeout_add(delay, lambda: (dialog.response(response), False)[1])


def ensure_comfy(app: App, message: str = "", parent: Gtk.Window | None = None) -> bool:
    """Make sure a working ComfyUI exists; True when it does afterwards.

    Returns at once when ComfyUI is configured and usable. Otherwise shows the
    notice: use a ComfyUI found on this PC, download the pinned one, or point at
    an existing folder.
    """
    if app.comfy_ready():
        return True
    message = message or "OpenFill runs its AI models in ComfyUI, and none was found on this PC."
    while True:
        try:
            dialog = _Notice(app, parent, message)
            _apply_hooks(dialog)
        except comfy_setup.SetupError as err:
            gi_message(parent, str(err))
            return False
        answer = dialog.run()
        install_dir, cpu = dialog.install_dir, bool(dialog.pre and dialog.pre.needs_cpu_choice)
        older_gpu = bool(dialog.pre and dialog.pre.older_gpu)
        amd = bool(dialog.pre and dialog.pre.amd_build)
        found = dialog.found
        dialog.destroy()
        if answer == RESP_DOWNLOAD:
            return _download_with_progress(app, install_dir, cpu, older_gpu, amd)
        if answer == RESP_USE_FOUND and found:
            try:
                app.use_comfy_dir(str(found[0].path))
                return True
            except BackendError as err:
                gi_message(parent, str(err))
                continue
        if answer == RESP_HAVE:
            picked = _choose_folder(parent, "Choose your ComfyUI folder", app.settings.comfy_dir)
            if not picked:
                continue
            try:
                app.use_comfy_dir(picked)
                return True
            except BackendError as err:
                gi_message(parent, ui_text.with_next_step(str(err)))
                continue
        return False


def gi_message(parent: Gtk.Window | None, text: str) -> None:
    """A small modal message."""
    box = Gtk.MessageDialog(
        transient_for=parent, modal=True, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=text
    )
    box.run()
    box.destroy()
