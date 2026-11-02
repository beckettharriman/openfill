# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Tie OpenFill's windows to Patchy's main window (Windows).

The PhotoCraft companion's ``Tether`` finds its editor by walking the top-level windows.
Patchy says which window is its own (``get_info.mainWindow.nativeId``, the HWND), so this
one only checks that the handle is still a window. The Tk thread never waits for Patchy:
``source`` returns the last known handle.
"""

from __future__ import annotations

from collections.abc import Callable

from openfill_photocraft.tether import Tether


class PatchyTether(Tether):
    def __init__(self, source: Callable[[], int]) -> None:
        super().__init__()
        self.source = source

    def find(self) -> int:
        if not self.available:
            return 0
        if self.hwnd and self.user32.IsWindow(self.hwnd):
            return self.hwnd
        hwnd = int(self.source() or 0)
        self.hwnd = hwnd if hwnd and self.user32.IsWindow(hwnd) else 0
        return self.hwnd
