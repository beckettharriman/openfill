# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Hand the keyboard back to Patchy after a button in OpenFill's panel (Windows).

Generate, Keep, Keep all and Discard are clicks in OpenFill's panel, which leaves the panel
with the keyboard, so Patchy's shortcuts would need a click on Patchy first. ``FocusReturn``
makes Patchy's main window (``get_info.mainWindow.nativeId``) the foreground window again,
a moment after the click, and only when that is safe:

- the panel itself had the keyboard at the click and still has it (a click in the page in a
  browser, a dialog of OpenFill's in front, or a switch to another program meanwhile leaves
  the keyboard where it is);
- Patchy's window is still there and not minimized.

Opening the panel from Patchy's menu to type a prompt never calls it. macOS and Linux have
no equivalent yet: there it does nothing.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from typing import Any


class Win32:
    """The few user32 calls ``FocusReturn`` makes (a fake stands in for it in the tests)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        self._ctypes, self._wintypes = ctypes, wintypes

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def owner(self, hwnd: int) -> tuple[int, int]:
        """(process id, thread id) of the window's owner."""
        pid = self._wintypes.DWORD(0)
        thread = self.user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        return int(pid.value), int(thread)

    def usable(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.user32.IsWindow(hwnd)) and not self.user32.IsIconic(hwnd)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(self.user32.SetForegroundWindow(hwnd))

    def set_foreground_attached(self, hwnd: int) -> bool:
        """The fallback when Windows refuses: share input state with the foreground window's
        thread for the call, then detach."""
        current = self.kernel32.GetCurrentThreadId()
        _pid, theirs = self.owner(self.foreground())
        if not theirs or theirs == current:
            return False
        if not self.user32.AttachThreadInput(current, theirs, True):
            return False
        try:
            return bool(self.user32.SetForegroundWindow(hwnd))
        finally:
            self.user32.AttachThreadInput(current, theirs, False)


class FocusReturn:
    """``request()`` after a click in the panel; Patchy gets the keyboard ``delay`` seconds
    later if the panel still has it (see the module docstring)."""

    def __init__(
        self,
        target: Callable[[], int],
        panel: Callable[[], int],
        win32: Any = None,
        delay: float = 0.15,
        later: Callable[[float, Callable[[], None]], Any] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.target = target  # Patchy's main window
        self.panel = panel  # OpenFill's panel (its top-level window)
        self.win32 = win32
        self.delay = delay
        self.later = later or _timer
        self.log = log or (lambda _text: None)
        if self.win32 is None and sys.platform == "win32":
            try:
                self.win32 = Win32()
            except (OSError, AttributeError):
                self.win32 = None
        # macOS and Linux: no win32, so request() does nothing. Activating another
        # program's window there needs its own route (NSRunningApplication on macOS, the
        # window manager on X11; Wayland does not allow it at all).

    def request(self) -> bool:
        """Called on a click; True when the hand-back is scheduled. Never waits."""
        w = self.win32
        if w is None:
            return False
        try:
            clicked_in = w.foreground()
            if not self._in_panel(clicked_in):
                return False
        except Exception as err:  # noqa: BLE001 - focus is a convenience: never fail a click
            self.log(f"focus: {err}")
            return False
        self.later(self.delay, lambda: self._hand_back(clicked_in))
        return True

    def _in_panel(self, hwnd: int) -> bool:
        """The panel's own window has the keyboard (not a dialog of OpenFill's, not another
        program)."""
        panel = int(self.panel() or 0)
        return bool(hwnd) and hwnd == panel and self.win32.owner(hwnd)[0] == os.getpid()

    def _hand_back(self, clicked_in: int) -> bool:
        w = self.win32
        try:
            if w.foreground() != clicked_in:
                return False  # the artist moved on: a dialog, another program, or Patchy already
            target = int(self.target() or 0)
            if not w.usable(target):
                return False
            if w.set_foreground(target) or w.set_foreground_attached(target):
                return True
            self.log("focus: Windows did not let Patchy's window come to the front")
        except Exception as err:  # noqa: BLE001
            self.log(f"focus: {err}")
        return False


def _timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer
