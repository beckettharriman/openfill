# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Variations picker: after several variations land as layers in a group, a
small window shows one thumbnail per variation. Clicking a thumbnail shows
that variation in the image and hides the others; "Keep all" leaves the
group as it is, "Discard" removes it.

GTK only. The caller passes callbacks that do the GIMP work (see
``gimp-openfill.py``: ``_pick_variation``), so this module stays free of
GIMP calls and is driven entirely by the result layers.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from . import ui_text  # noqa: E402
from .dialogs import style  # noqa: E402

THUMB = 160


class VariationPicker(Gtk.Window):
    """``thumbnails``: list of (name, GdkPixbuf or None).

    ``on_show(index)`` is called when a thumbnail is clicked (not on open:
    the image keeps the state the insertion left); ``on_keep_all()`` and ``on_discard()`` when those
    buttons are pressed. "Keep selected" and closing the window keep the
    current choice. ``on_closed()`` runs once at the end in every case.
    """

    def __init__(
        self,
        title: str,
        thumbnails: list[tuple[str, GdkPixbuf.Pixbuf | None]],
        on_show: Callable[[int], None],
        on_keep_all: Callable[[], None],
        on_discard: Callable[[], None],
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(title=ui_text.PICKER_TITLE.format(title=title))
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        self.set_resizable(False)
        self.set_border_width(12)
        self._on_show = on_show
        self._on_keep_all = on_keep_all
        self._on_discard = on_discard
        self._on_closed = on_closed
        self._done = False
        self._sources: list[int] = []
        self.choice = 0
        self.outcome = "keep"

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)
        head = Gtk.Label(xalign=0)
        head.set_markup(f"<b>{GLib.markup_escape_text(ui_text.picker_heading(len(thumbnails)))}</b>")
        box.pack_start(head, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.buttons: list[Gtk.ToggleButton] = []
        group = None
        for index, (name, pixbuf) in enumerate(thumbnails):
            button = Gtk.RadioButton.new_from_widget(group)
            group = group or button
            button.set_mode(False)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            image = Gtk.Image()
            if pixbuf is not None:
                image.set_from_pixbuf(pixbuf)
            else:
                image.set_size_request(THUMB, THUMB)
            inner.pack_start(image, False, False, 0)
            inner.pack_start(Gtk.Label(label=ui_text.VARIATION_LABEL.format(number=index + 1)), False, False, 0)
            button.add(inner)
            button.set_tooltip_text(name)
            button.connect("toggled", self._toggled, index)
            row.pack_start(button, False, False, 0)
            self.buttons.append(button)
        box.pack_start(row, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_markup(f"<small>{GLib.markup_escape_text(ui_text.PICKER_HINT)}</small>")
        hint.set_line_wrap(True)
        hint.set_max_width_chars(60)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        discard = Gtk.Button.new_with_mnemonic("_Discard")
        style(discard, "danger")
        discard.connect("clicked", self._discard)
        actions.pack_start(discard, False, False, 0)
        keep = Gtk.Button.new_with_mnemonic("_Keep selected")
        style(keep, "primary")
        keep.set_can_default(True)
        keep.connect("clicked", lambda *_: self.finish("keep"))
        actions.pack_end(keep, False, False, 0)
        keep_all = Gtk.Button.new_with_mnemonic("Keep _all")
        keep_all.connect("clicked", self._keep_all)
        actions.pack_end(keep_all, False, False, 0)
        box.pack_start(actions, False, False, 0)

        self.connect("delete-event", lambda *_: (self.finish("keep"), True)[1])
        self.connect("key-press-event", self._key)
        self.show_all()
        keep.grab_default()
        if self.buttons:
            self.buttons[0].grab_focus()
        self.forced_show = _ensure_visible(self.get_title() or "")
        GLib.timeout_add(200, self._recheck_visible)

    def _toggled(self, button: Gtk.ToggleButton, index: int) -> None:
        if button.get_active() and not self._done:
            self.choice = index
            self._on_show(index)

    def _key(self, _w: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.finish("keep")
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.finish("keep")
            return True
        if Gdk.KEY_1 <= event.keyval <= Gdk.KEY_9:
            index = event.keyval - Gdk.KEY_1
            if index < len(self.buttons):
                self.buttons[index].set_active(True)
                return True
        return False

    def _keep_all(self, *_: object) -> None:
        self._on_keep_all()
        self.finish("keep_all")

    def _discard(self, *_: object) -> None:
        self._on_discard()
        self.finish("discard")

    def add_timeout(self, ms: int, fn: Callable[[], object]) -> None:
        """Run ``fn`` once after ``ms`` unless the picker has closed by then."""

        def fire() -> bool:
            self._sources.remove(source)
            if not self._done:
                fn()
            return False

        source = GLib.timeout_add(ms, fire)
        self._sources.append(source)

    def _recheck_visible(self) -> bool:
        if not self._done and _ensure_visible(self.get_title() or ""):
            self.forced_show = True
        return False

    def finish(self, outcome: str) -> None:
        """Close the picker with ``outcome`` (keep, keep_all, discard); later calls do nothing."""
        if self._done:
            return
        self._done = True
        for source in self._sources:  # test timers still pending must not outlive the window
            GLib.source_remove(source)
        self._sources.clear()
        self.outcome = outcome
        self.destroy()
        if self._on_closed:
            self._on_closed()


def _ensure_visible(title: str) -> bool:
    """Windows: show the picker's native window when GTK showed the widget but
    the window is not WS_VISIBLE (seen in plug-in processes started by GIMP
    3.2.6 on Windows, as with the progress window) and bring it to the front.
    Returns True when it had to be shown here. Best effort, never raises."""
    if sys.platform != "win32" or not title:
        return False
    try:
        import ctypes
        import os

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        found: list[int] = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd: int, _: object) -> bool:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value != os.getpid():
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 512)
            if buf.value == title:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(proto(cb), None)
        if not found:
            return False
        hwnd = ctypes.c_void_p(found[0])
        if user32.IsWindowVisible(hwnd):
            return False
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001 - best effort
        return False


def _play_step(picker: VariationPicker, step: str) -> bool:
    if picker._done:
        return False
    if step.isdigit() and 0 < int(step) <= len(picker.buttons):
        picker.buttons[int(step) - 1].clicked()
    elif step == "keep_all":
        picker._keep_all()
    elif step == "discard":
        picker._discard()
    elif step == "keep":
        picker.finish("keep")
    return False


def scaled(pixbuf: GdkPixbuf.Pixbuf | None, size: int = THUMB) -> GdkPixbuf.Pixbuf | None:
    """``pixbuf`` scaled down to fit ``size`` x ``size`` (never up); None stays None."""
    if pixbuf is None:
        return None
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if max(w, h) <= size:
        return pixbuf
    k = size / max(w, h)
    return pixbuf.scale_simple(max(1, int(w * k)), max(1, int(h * k)), GdkPixbuf.InterpType.BILINEAR)


def run_picker(
    title: str,
    thumbnails: list[tuple[str, GdkPixbuf.Pixbuf | None]],
    on_show: Callable[[int], None],
    on_keep_all: Callable[[], None],
    on_discard: Callable[[], None],
    autoclose_seconds: float = 0,
    test_steps: list[str] | None = None,
) -> str:
    """Show the picker and block (in a nested main loop) until it closes; returns the outcome.

    ``test_steps`` (test hook) is played back one step per second: a
    variation number ("1", "2", ...) clicks that thumbnail; "keep",
    "keep_all" and "discard" press the buttons.
    """
    picker = VariationPicker(
        title,
        thumbnails,
        on_show,
        on_keep_all,
        on_discard,
        on_closed=lambda: Gtk.main_quit() if Gtk.main_level() > 0 else None,
    )
    if autoclose_seconds and autoclose_seconds > 0:
        picker.add_timeout(int(autoclose_seconds * 1000), lambda: picker.finish("keep"))
    for index, step in enumerate(test_steps or []):
        picker.add_timeout(1000 * (index + 1), lambda s=str(step): _play_step(picker, s))
    Gtk.main()
    return picker.outcome
