# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Floating progress window with live preview, ETA, downloads and Cancel.

GTK 3 only (no GIMP calls in here). The generation itself runs in a worker
thread (see ``app.Runner``); the worker never touches GTK, it posts updates
through :meth:`ProgressWindow.post`, which marshals them onto the GTK main
loop with ``GLib.idle_add``. Preview frames are throttled to a few per
second and decoded on the main thread with GdkPixbuf.
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from . import ui_text  # noqa: E402
from .dialogs import style  # noqa: E402
from .progress import Eta, FrameThrottle, download_row, fit_within, format_duration, parse_preview_frame  # noqa: E402

PREVIEW_LONG_SIDE = 320


def _window_rect_from_handle(handle_bytes: bytes | None) -> tuple[int, int, int, int] | None:
    """Windows only: (x, y, w, h) of the native window GIMP reported as the
    progress parent (the image window of the display the plug-in runs on)."""
    if sys.platform != "win32" or not handle_bytes:
        return None
    try:
        raw = bytes(handle_bytes)
        hwnd = int.from_bytes(raw[:8], "little") if len(raw) >= 8 else int.from_bytes(raw, "little")
        if not hwnd:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        rect = RECT()
        if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):  # type: ignore[attr-defined]
            return None
        return rect.l, rect.t, rect.r - rect.l, rect.b - rect.t
    except Exception:  # noqa: BLE001 - positioning is best effort
        return None


class _NoActivate:
    """Windows: keep the progress window from taking the keyboard focus.

    GTK's focus-on-map hint is not enough there (keep-above re-activates the
    window), so the native window gets WS_EX_NOACTIVATE before it is shown
    and, should it still become the foreground window, the previous
    foreground window is given back its focus. Clicks (Cancel) still work.
    """

    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    SW_SHOWNOACTIVATE = 4

    def __init__(self, title: str) -> None:
        self.title = title
        self.hwnd = 0
        self.previous = 0
        self.forced_show = False  # ensure_visible() had to show the native window
        if sys.platform != "win32":
            return
        try:
            self.user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            self.previous = self.user32.GetForegroundWindow()
        except Exception:  # noqa: BLE001
            self.previous = 0

    def _find(self) -> int:
        import os

        user32 = self.user32
        found = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd: int, _: object) -> bool:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value != os.getpid():
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 512)
            if buf.value == self.title:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(proto(cb), None)
        return found[0] if found else 0

    def before_show(self) -> None:
        """Mark the realized native window WS_EX_NOACTIVATE (call before it is shown)."""
        if sys.platform != "win32":
            return
        try:
            self.hwnd = self._find()
            if self.hwnd:
                get = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
                put = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)
                get.restype = ctypes.c_ssize_t
                put.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
                style = get(ctypes.c_void_p(self.hwnd), self.GWL_EXSTYLE)
                put(ctypes.c_void_p(self.hwnd), self.GWL_EXSTYLE, style | self.WS_EX_NOACTIVATE)
        except Exception:  # noqa: BLE001 - focus handling is best effort
            self.hwnd = 0

    def ensure_visible(self) -> bool:
        """Show the native window if GTK showed the widget but the window is not
        WS_VISIBLE (seen in plug-in processes started by GIMP 3.2.6 on Windows).
        Returns True when it had to be shown here."""
        if sys.platform != "win32" or not self.hwnd:
            return False
        try:
            if self.user32.IsWindowVisible(ctypes.c_void_p(self.hwnd)):
                return False
            self.user32.ShowWindow(ctypes.c_void_p(self.hwnd), self.SW_SHOWNOACTIVATE)
            return True
        except Exception:  # noqa: BLE001 - best effort
            return False

    def after_show(self) -> bool:
        """GLib timeout callback: hand the focus back if the window took it; never repeats."""
        if self.ensure_visible():
            self.forced_show = True
        if sys.platform != "win32" or not self.previous:
            return False
        try:
            fg = self.user32.GetForegroundWindow()
            if self.hwnd and fg == self.hwnd:
                self.user32.SetForegroundWindow(ctypes.c_void_p(self.previous))
        except Exception:  # noqa: BLE001
            pass
        return False


class ProgressWindow(Gtk.Window):
    """Non-modal, always-on-top status window for one operation."""

    def __init__(
        self,
        title: str,
        model_name: str,
        on_cancel: Callable[[], None] | None = None,
        position: tuple[int, int] | None = None,
        anchor_handle: bytes | None = None,
        on_moved: Callable[[int, int], None] | None = None,
        show_preview: bool = True,
        quit_main_on_close: bool = True,
    ) -> None:
        super().__init__(title=ui_text.PROGRESS_TITLE.format(title=title))
        self._quit_main_on_close = quit_main_on_close
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(True)
        # Never take the keyboard focus away from the image window: shown
        # without activation; clicks (Cancel) still work.
        self.set_focus_on_map(False)
        self.set_resizable(False)
        self.set_deletable(False)
        self.set_border_width(10)
        self._on_cancel = on_cancel
        self._on_moved = on_moved
        self._started = time.monotonic()
        self._eta = Eta()
        self._throttle = FrameThrottle(0.25)
        self._finished = False
        self._closed = False
        self._close_timer = 0
        self._cancel_requested = False
        self._last_pos: tuple[int, int] | None = None
        self.preview_frames = 0

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(box)
        self.title_label = Gtk.Label(xalign=0)
        model_text = f"  <small>with {GLib.markup_escape_text(model_name)}</small>" if model_name else ""
        self.title_label.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>{model_text}")
        box.pack_start(self.title_label, False, False, 0)
        self.step_label = Gtk.Label(label=ui_text.PROGRESS_READY, xalign=0)
        self.step_label.set_line_wrap(True)
        self.step_label.set_width_chars(36)
        self.step_label.set_max_width_chars(36)
        self.step_label.set_lines(2)
        box.pack_start(self.step_label, False, False, 0)
        self.bar = Gtk.ProgressBar()
        self.bar.set_show_text(True)
        box.pack_start(self.bar, False, False, 0)
        self.time_label = Gtk.Label(label=ui_text.elapsed_text("0:00"), xalign=0)
        self.time_label.get_style_context().add_class("dim-label")
        box.pack_start(self.time_label, False, False, 0)
        self.preview = Gtk.Image()
        self.preview.set_size_request(PREVIEW_LONG_SIDE, 160)
        preview_box = Gtk.Overlay()
        preview_box.add(self.preview)
        self.preview_hint = Gtk.Label(label=ui_text.PROGRESS_PREVIEW_HINT)
        self.preview_hint.set_line_wrap(True)
        self.preview_hint.set_max_width_chars(30)
        self.preview_hint.set_justify(Gtk.Justification.CENTER)
        self.preview_hint.get_style_context().add_class("dim-label")
        preview_box.add_overlay(self.preview_hint)
        frame = Gtk.Frame()
        frame.add(preview_box)
        if not show_preview:
            frame.set_no_show_all(True)
        box.pack_start(frame, False, False, 0)
        self.downloads_label = Gtk.Label(xalign=0)
        self.downloads_label.set_no_show_all(True)
        box.pack_start(self.downloads_label, False, False, 0)
        self.message = Gtk.Label(xalign=0)
        self.message.set_line_wrap(True)
        self.message.set_max_width_chars(40)
        self.message.set_selectable(True)
        box.pack_start(self.message, False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.cancel_button = Gtk.Button(label="Cancel")
        style(self.cancel_button, "danger")
        self.cancel_button.set_size_request(120, 32)
        self.cancel_button.set_tooltip_text(ui_text.PROGRESS_CANCEL_TIP)
        self.cancel_button.connect("clicked", self._cancel_clicked)
        buttons.pack_end(self.cancel_button, False, False, 0)
        self.close_button = Gtk.Button(label="Close")
        self.close_button.connect("clicked", lambda *_: self.close_now())
        self.close_button.set_no_show_all(True)
        buttons.pack_end(self.close_button, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.connect("configure-event", self._configure)
        self.connect("delete-event", lambda *_: True)  # only Close/finish close it
        self._no_activate = _NoActivate(self.get_title())
        self.realize()  # native window exists now, not yet shown
        self._no_activate.before_show()
        self.get_child().show_all()
        self._place(position, anchor_handle)  # before showing: no jump, never off screen
        self.show()
        if self._no_activate.ensure_visible():
            self._no_activate.forced_show = True
        GLib.timeout_add(150, self._no_activate.after_show)
        self._timer = GLib.timeout_add(500, self._tick)

    # placement -----------------------------------------------------------

    @staticmethod
    def _work_areas() -> list[tuple[int, int, int, int]]:
        areas = []
        try:
            display = Gdk.Display.get_default()
            for i in range(display.get_n_monitors()):
                work = display.get_monitor(i).get_workarea()
                areas.append((work.x, work.y, work.width, work.height))
        except Exception:  # noqa: BLE001 - placement is best effort
            pass
        return areas

    def _place(self, position: tuple[int, int] | None, anchor_handle: bytes | None) -> None:
        """Remembered position (clamped onto a current monitor, so a position
        saved on a monitor that is gone cannot hide the window), else the top
        right of the image window, else of the primary monitor."""
        _min, natural = self.get_preferred_size()
        # the preview grows the window once frames arrive: reserve that room
        width, height = max(natural.width, 360), natural.height + (PREVIEW_LONG_SIDE - 160)
        areas = self._work_areas()
        if position and position[0] >= 0 and position[1] >= 0:
            x, y = int(position[0]), int(position[1])
        else:
            rect = _window_rect_from_handle(anchor_handle)
            if rect is not None:
                scale = max(1, self.get_scale_factor())
                rect = tuple(v // scale for v in rect)
            if rect is None:
                display = Gdk.Display.get_default()
                monitor = display.get_primary_monitor() or display.get_monitor(0)
                work = monitor.get_workarea()
                rect = (work.x, work.y, work.width, work.height)
            rx, ry, rw, _ = rect
            x, y = rx + rw - width - 24, ry + 48
        self.move(*ui_text.clamp_position(x, y, width, height, areas))

    def _configure(self, _widget: Gtk.Widget, event: Gdk.EventConfigure) -> bool:
        self._last_pos = (event.x, event.y)
        return False

    # worker-facing API (thread safe) -------------------------------------

    def post(self, fn: Callable, *args: object) -> None:
        """Run ``fn(*args)`` once on the GTK main loop (safe from any thread)."""
        GLib.idle_add(lambda: (fn(*args), False)[1])

    def set_step(self, fraction: float, text: str) -> None:
        """Update the progress bar (0..1) and the step text."""
        self.post(self._set_step, fraction, text)

    def preview_frame(self, payload: bytes) -> None:
        """Show a backend preview frame (binary websocket payload), throttled."""
        if not self._throttle.admit():
            return
        frame = parse_preview_frame(payload)
        if frame is not None:
            self.post(self._show_preview, frame.data)

    def finish(self, success: bool, message: str = "", close_after_ms: int = 1000) -> None:
        """End the operation: success closes after ``close_after_ms``; failure waits for Close."""
        self.post(self._finish, success, message, close_after_ms)

    def set_downloads(self, current: dict | None, pending: list[str] | None = None) -> None:
        """Show the supervisor's current task and the queued model ids."""
        self.post(self._set_downloads, current, pending or [])

    @property
    def cancelled(self) -> bool:
        """True once Cancel was pressed."""
        return self._cancel_requested

    # main-thread implementation -------------------------------------------

    def _set_step(self, fraction: float, text: str) -> None:
        if self._finished:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        self.bar.set_fraction(fraction)
        self.bar.set_text(f"{fraction * 100:.0f}%")
        if text:
            self.step_label.set_text(ui_text.plain_step_text(text))

    def _show_preview(self, data: bytes) -> None:
        if self._finished:
            return
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except GLib.Error:
            return
        if pixbuf is None:
            return
        w, h = fit_within(pixbuf.get_width(), pixbuf.get_height(), PREVIEW_LONG_SIDE)
        if (w, h) != (pixbuf.get_width(), pixbuf.get_height()):
            pixbuf = pixbuf.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
        self.show_pixbuf(pixbuf)

    def show_pixbuf(self, pixbuf: GdkPixbuf.Pixbuf) -> None:
        """Main thread: show a preview image (replaces the placeholder text)."""
        self.preview.set_from_pixbuf(pixbuf)
        self.preview_hint.hide()
        self.preview_frames += 1

    def _set_downloads(self, current: dict | None, pending: list[str]) -> None:
        if self._closed:
            return
        lines = []
        if current and current.get("type") == "download":
            lines.append(ui_text.PROGRESS_DOWNLOAD.format(row=download_row(current)))
        elif current:
            lines.append(f"{current.get('type')}: {current.get('file', '')} {current.get('state', '')}")
        for p in pending:
            lines.append(ui_text.PROGRESS_QUEUED.format(name=p))
        if lines:
            self.downloads_label.set_text("\n".join(lines))
            self.downloads_label.show()
        else:
            self.downloads_label.hide()

    def _finish(self, success: bool, message: str, close_after_ms: int) -> None:
        if self._closed:
            return
        self._finished = True
        self.cancel_button.set_sensitive(False)
        if success:
            self.bar.set_fraction(1.0)
            self.bar.set_text("100%")
            self.step_label.set_text(message or ui_text.PROGRESS_DONE)
            self._close_timer = GLib.timeout_add(max(0, close_after_ms), self._close_timeout)
        else:
            cancelled = self._cancel_requested or message.lower().startswith("cancel")
            heading = ui_text.PROGRESS_CANCELLED if cancelled else ui_text.PROGRESS_FAILED
            self.step_label.set_markup(f"<b>{GLib.markup_escape_text(heading)}</b>")
            if not cancelled:
                message = ui_text.with_next_step(message)
            self.message.set_markup(f"<span foreground='#e0a040'>{GLib.markup_escape_text(message)}</span>")
            self.cancel_button.hide()
            self.close_button.show()
        elapsed = time.monotonic() - self._started
        self.time_label.set_text(ui_text.elapsed_text(format_duration(elapsed)))

    def _tick(self) -> bool:
        if self._finished:
            self._timer = 0  # returning False removes the source; close_now must not remove it again
            return False
        elapsed = time.monotonic() - self._started
        remaining = self._eta.update(elapsed, self.bar.get_fraction())
        self.time_label.set_text(
            ui_text.elapsed_text(
                format_duration(elapsed), format_duration(remaining) if remaining is not None else None
            )
        )
        return True

    def _close_timeout(self) -> bool:
        self._close_timer = 0  # returning False removes the source
        self.close_now()
        return False

    def _cancel_clicked(self, *_: object) -> None:
        self._cancel_requested = True
        self.cancel_button.set_sensitive(False)
        self.step_label.set_text(ui_text.PROGRESS_CANCELLING)
        if self._on_cancel:
            self._on_cancel()

    def close_now(self) -> None:
        """Close the window (idempotent) and leave the nested main loop it runs in.

        Safe to call twice (for example the success timer and a caller's own
        close): only the first call destroys the window and quits the loop, so
        an outer main loop is never quit by mistake.
        """
        if self._closed:
            return
        self._closed = True
        self._finished = True  # idle callbacks still queued must not touch destroyed widgets
        if self._close_timer:
            GLib.source_remove(self._close_timer)
            self._close_timer = 0
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._on_moved and self._last_pos:
            self._on_moved(int(self._last_pos[0]), int(self._last_pos[1]))
        self.destroy()
        if self._quit_main_on_close and Gtk.main_level() > 0:
            Gtk.main_quit()
