# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Run a backend job with the floating progress window (worker thread) or,
when the window is disabled, inline with GIMP's status-bar progress only.

This is a GIMP-side module (like ``gimp_io``): it needs Gimp/GimpUi for the
status bar progress and the window, so it is not imported by the unit tests.

Threading contract: the worker thread talks only to the backend (HTTP and
websocket). Every GIMP or GTK call happens on the main thread, either
before/after the worker runs or inside ``GLib.idle_add`` callbacks.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("GimpUi", "3.0")
from gi.repository import Gimp, GimpUi, GLib  # noqa: E402

from . import gimp_io  # noqa: E402
from .backend import GenerationCancelled, describe_error  # noqa: E402

if TYPE_CHECKING:
    from .app import App

FAIL_WINDOW_TIMEOUT_MS = 20000  # a failed run closes its window after this even if nobody clicks
Job = Callable[[Callable, Callable | None, Callable | None], object]


def _test_cancel_step() -> str:
    """Test hook (OPENFILL_TEST_CANCEL_FILE): the step text at which Cancel is pressed, or ""."""
    path = os.environ.get("OPENFILL_TEST_CANCEL_FILE")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class _StallProbe:
    """Measurement hook (OPENFILL_PERF_STALL=1): a 20 ms GLib timer on the plug-in's main
    loop; the largest gap between two ticks is how long the loop was blocked."""

    def __init__(self) -> None:
        import time

        self._clock = time.perf_counter
        self.last = self._clock()
        self.max_gap = 0.0
        self.ticks = 0
        self.source = GLib.timeout_add(20, self._tick)

    def _tick(self) -> bool:
        now = self._clock()
        self.max_gap = max(self.max_gap, now - self.last)
        self.last = now
        self.ticks += 1
        return True

    def stop(self, log) -> None:
        GLib.source_remove(self.source)
        log.info("perf: plug-in main loop max gap %.1f ms over %d ticks of 20 ms", self.max_gap * 1000, self.ticks)


class Runner:
    """One backend job with its progress UI: the floating window (job in a worker
    thread, GTK main loop on the main thread) or GIMP's status bar only."""

    def __init__(self, app: App, title: str, model_name: str, interactive: bool = True) -> None:
        self.app = app
        self.title = title
        self.model_name = model_name
        self.window = None
        self.status = gimp_io.GimpProgress(title)
        self._downloads_timer = 0
        # Non-interactive runs (scripts, batch) never open the window: nobody is there to close it.
        self.use_window = bool(app.settings.progress_window) and bool(interactive)
        # Test hooks for cancel tests in batch runs: OPENFILL_TEST_CANCEL_FILE names a file holding the text of the
        # progress step at which Cancel is pressed (read per run, so one GIMP can warm up first);
        # a run with such a step opens the window even when non-interactive.
        self._cancel_on = _test_cancel_step()
        if self._cancel_on:
            self.use_window = True
        self._closed = False
        self._window_gone = False
        self._fallback = 0
        self._cancel_close = 0
        if self.use_window:
            try:
                GimpUi.init("gimp-openfill")
                from .progress_window import ProgressWindow

                handle = None
                try:
                    data = Gimp.progress_get_window_handle()
                    handle = data.get_data() if data is not None else None
                except Exception:  # noqa: BLE001
                    handle = None
                pos = (app.settings.progress_window_x, app.settings.progress_window_y)
                self.window = ProgressWindow(
                    title,
                    model_name,
                    on_cancel=self._cancel_clicked,
                    position=pos,
                    anchor_handle=handle,
                    on_moved=self._save_position,
                )
                self._downloads_timer = GLib.timeout_add(1000, self._poll_downloads)
            except Exception as err:  # noqa: BLE001 - the window is optional
                app.log.warning("progress window unavailable: %s", err)
                self.window = None
                self.use_window = False

    # callbacks ------------------------------------------------------------

    def _cancel_clicked(self) -> None:
        self.app.log.info("cancel requested from the progress window")

    def _save_position(self, x: int, y: int) -> None:
        try:
            self.app.settings.progress_window_x = int(x)
            self.app.settings.progress_window_y = int(y)
            self.app.settings.save(self.app.paths.config)
        except OSError as err:  # the position is a convenience; the run goes on
            self.app.log.warning("progress window position not saved: %s", err)

    def _poll_downloads(self) -> bool:
        if self.window is None:
            return False
        try:
            status = self.app.task_status()
            pending = [t.get("model_id") or t.get("type", "task") for t in self.app.server.pending_tasks()]
        except Exception as err:  # noqa: BLE001 - a raising GLib callback would stop polling for good
            self.app.log.debug("download status poll failed: %s", err)
            return True
        self.window.set_downloads(status.get("current"), pending)
        return True

    def _progress(self, fraction: float, text: str) -> None:
        """Called from the worker thread."""
        if self.window is not None:
            self.window.set_step(fraction, text)
            GLib.idle_add(lambda: (self.status(fraction, text), False)[1])
            # Test hook: OPENFILL_TEST_CANCEL_AT=0.3 presses Cancel once sampling passes 30 %.
            auto = os.environ.get("OPENFILL_TEST_CANCEL_AT")
            if auto and fraction >= float(auto) and "step" in text and not self.window.cancelled:
                GLib.idle_add(lambda: (self.window._cancel_clicked(), False)[1])
            phase = self._cancel_on
            if phase and phase in text and not self.window.cancelled:
                self.app.log.info("test hook: cancel at '%s'", text)
                GLib.idle_add(lambda: (self.window._cancel_clicked(), False)[1])
        else:
            self.status(fraction, text)

    def _cancelled(self) -> bool:
        return self.window is not None and self.window.cancelled

    # run ------------------------------------------------------------------

    def next_round(self) -> None:
        """Between the rounds of one operation: the window and the preview stay
        as they are; only GIMP's own progress bar is started again."""
        self.status.restart()

    def run(self, job: Job) -> object:
        """Execute ``job(progress, preview, cancel)``; return its result or raise."""
        if self.window is None:
            try:
                return job(self.status, None, None)
            finally:
                self.status.done()
        box: dict = {}

        def worker() -> None:
            try:
                box["result"] = job(self._progress, self.window.preview_frame, self._cancelled)
            except BaseException as err:  # noqa: BLE001 - re-raised on the main thread
                box["error"] = err
            finally:
                GLib.idle_add(lambda: (self._quit_loop(), False)[1])

        thread = threading.Thread(target=worker, name="openfill-job", daemon=True)
        thread.start()
        from gi.repository import Gtk

        stall = _StallProbe() if os.environ.get("OPENFILL_PERF_STALL") == "1" else None
        Gtk.main()
        if stall is not None:
            stall.stop(self.app.log)
        thread.join(5)
        self.status.done()
        if "error" in box:
            raise box["error"]  # the caller reports it through fail()
        if self._cancelled():
            # Cancel pressed after the last backend check (while the result was downloading): the window
            # promised an unchanged image, so the result is dropped.
            raise GenerationCancelled("cancelled by the user after the result arrived")
        return box["result"]

    @staticmethod
    def _quit_loop() -> None:
        from gi.repository import Gtk

        if Gtk.main_level() > 0:
            Gtk.main_quit()

    def done(self, message: str = "Done", close_after_ms: int = 1000) -> None:
        """Success: show the final text, close the window after a moment."""
        self._stop_downloads()
        if self.window is None or self._closed:
            return
        self._closed = True
        self.app.log.info(
            "progress window: %d preview frame(s) shown, closing after success%s",
            self.window.preview_frames,
            " (native window had to be shown explicitly)" if self.window._no_activate.forced_show else "",
        )
        self.window.finish(True, message, close_after_ms)
        from gi.repository import Gtk

        Gtk.main()  # until close_now() quits it

    def fail(self, error: BaseException) -> None:
        """Failure or cancel: keep the window open until Close is pressed
        (unless the run was cancelled, which closes after a short delay)."""
        self._stop_downloads()
        if self.window is None or self._closed:
            return
        self._closed = True
        self.app.log.info(
            "progress window: %d preview frame(s) shown, finishing with %s",
            self.window.preview_frames,
            type(error).__name__,
        )
        from gi.repository import Gtk

        if isinstance(error, GenerationCancelled):
            self.window.finish(False, "Cancelled - the image was left unchanged.", 0)
            self._cancel_close = GLib.timeout_add(1500, self._cancel_close_timeout)
        else:
            self.window.finish(False, describe_error(error), 0)
        # Never wait forever for Close: the procedure must return (the error is also
        # reported through the PDB and Gimp.message).
        self._fallback = GLib.timeout_add(FAIL_WINDOW_TIMEOUT_MS, self._fallback_close)
        Gtk.main()
        if self._fallback:  # closed by the user (or the cancel timer) first
            GLib.source_remove(self._fallback)
            self._fallback = 0
        if self._cancel_close:  # closed by the user before the cancel timer fired
            GLib.source_remove(self._cancel_close)
            self._cancel_close = 0

    def _cancel_close_timeout(self) -> bool:
        self._cancel_close = 0
        return self._close_window_once()

    def _fallback_close(self) -> bool:
        self._fallback = 0
        return self._close_window_once()

    def _close_window_once(self) -> bool:
        if not self._window_gone:
            self._window_gone = True
            self.window.close_now()
        return False

    def _stop_downloads(self) -> None:
        if self._downloads_timer:
            GLib.source_remove(self._downloads_timer)
            self._downloads_timer = 0
