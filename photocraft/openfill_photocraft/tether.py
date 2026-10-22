# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Tie OpenFill's windows to PhotoCraft's main window (Windows).

Windows has "owned" windows for exactly this: a window owned by PhotoCraft's stays above
PhotoCraft (and only above it), hides when PhotoCraft is minimized and comes back with
it. ``Tether`` finds PhotoCraft's main window, makes the bar and its collapsed strip
owned by it, and reports PhotoCraft's rectangle so the bar can follow it when it moves.

Elsewhere (Wayland does not tell other programs where a window is) ``Tether`` finds
nothing and the bar stays a floating window.
"""

from __future__ import annotations

import sys

GWLP_HWNDPARENT = -8


class Tether:
    def __init__(self) -> None:
        self.hwnd = 0
        self.available = sys.platform == "win32"
        if self.available:
            import ctypes
            from ctypes import wintypes

            self.ctypes, self.wintypes = ctypes, wintypes
            self.user32 = ctypes.windll.user32
            self.kernel32 = ctypes.windll.kernel32
            self.user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            self.user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            self.user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            self.user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

    # finding PhotoCraft -------------------------------------------------------------

    def _exe_of(self, hwnd: int) -> str:
        pid = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = self.ctypes.create_unicode_buffer(1024)
            size = self.wintypes.DWORD(1024)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, self.ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def find(self) -> int:
        """PhotoCraft's main window: its largest visible top-level window without an owner."""
        if not self.available:
            return 0
        if self.hwnd and self.user32.IsWindow(self.hwnd):
            return self.hwnd
        found: list[tuple[int, int]] = []
        proto = self.ctypes.WINFUNCTYPE(self.wintypes.BOOL, self.wintypes.HWND, self.wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd) or self.user32.GetWindow(hwnd, 4):  # GW_OWNER
                return True
            if self._exe_of(hwnd).lower().endswith("photocraft.exe"):
                rect = self.wintypes.RECT()
                self.user32.GetWindowRect(hwnd, self.ctypes.byref(rect))
                found.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd))
            return True

        self.user32.EnumWindows(proto(visit), 0)
        self.hwnd = max(found)[1] if found else 0
        return self.hwnd

    # tying windows to it ----------------------------------------------------------

    def attach(self, toplevel) -> bool:
        """Make a Tk toplevel owned by PhotoCraft's window. False when there is none."""
        owner = self.find()
        if not owner:
            return False
        toplevel.update_idletasks()
        try:
            frame = int(toplevel.wm_frame(), 16)
        except (ValueError, TypeError):
            return False
        self.user32.SetWindowLongPtrW(frame, GWLP_HWNDPARENT, owner)
        return True

    def rect(self) -> tuple[int, int, int, int] | None:
        """PhotoCraft's window (left, top, right, bottom) in screen pixels; None when it is
        minimized or gone."""
        hwnd = self.find()
        if not hwnd or self.user32.IsIconic(hwnd):
            return None
        rect = self.wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, self.ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom
