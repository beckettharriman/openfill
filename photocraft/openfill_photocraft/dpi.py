# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Sharp text on high-DPI screens (Windows).

python.exe and pythonw.exe do not say in their manifest that they handle DPI scaling, so
Windows draws a Tk window at 96 DPI and stretches the bitmap: at 150 % the panel is blurry.
``enable()`` tells Windows that this process scales itself, before Tk makes a window, and
``scale()`` says by how much: 1.5 at 150 %. The panel's sizes are design pixels multiplied
by it (``ui.px``), so it keeps its size and its text is drawn at the screen's real pixels.

The process is made *system* DPI aware, as IDLE does: one scale for the whole session, the
primary monitor's. A Tk 8.6 window cannot lay itself out again when it moves to a monitor
with another scale; there Windows stretches it as before, so it keeps its size. The same
mode is asked for from Windows 10 1703 on (SetProcessDpiAwarenessContext), on Windows 8.1
(shcore SetProcessDpiAwareness) and before (SetProcessDPIAware).

Nothing here does anything elsewhere, and nothing here raises: a failure leaves the panel
as it was (scale 1). ``OPENFILL_DPI=off`` turns it off; ``OPENFILL_UI_SCALE`` sets the scale.
"""

from __future__ import annotations

import os
import sys
from typing import Any

SYSTEM_AWARE_CONTEXT = -2  # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE
PROCESS_SYSTEM_DPI_AWARE = 1
LOGPIXELSY = 90
ERROR_ACCESS_DENIED = 5  # SetProcessDpiAwarenessContext: the awareness was already set
E_ACCESSDENIED = -2147024891  # 0x80070005 as a signed HRESULT: the same from shcore
UNAWARE, SYSTEM, PER_MONITOR = 0, 1, 2

_state: dict[str, Any] | None = None


def _off() -> bool:
    return os.environ.get("OPENFILL_DPI", "").strip().lower() in ("0", "off", "no", "false")


def _dlls() -> tuple[Any, Any, Any]:
    """user32, shcore (None before Windows 8.1) and gdi32, as private ctypes handles so the
    argument types set here do not change ``ctypes.windll``'s for other code."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:
        shcore = None
    for name, restype, argtypes in (
        ("SetProcessDpiAwarenessContext", wintypes.BOOL, [ctypes.c_void_p]),
        ("GetThreadDpiAwarenessContext", ctypes.c_void_p, []),
        ("GetAwarenessFromDpiAwarenessContext", ctypes.c_int, [ctypes.c_void_p]),
        ("GetDpiForSystem", wintypes.UINT, []),
        ("GetDC", ctypes.c_void_p, [ctypes.c_void_p]),
        ("ReleaseDC", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
    ):
        fn = getattr(user32, name, None)  # None on a Windows older than the call
        if fn is not None:
            fn.restype, fn.argtypes = restype, argtypes
    for name in ("SetProcessDpiAwareness", "GetProcessDpiAwareness"):
        fn = getattr(shcore, name, None)
        if fn is not None:
            fn.restype = ctypes.c_long  # an HRESULT, signed
    gdi32.GetDeviceCaps.restype, gdi32.GetDeviceCaps.argtypes = ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]
    return user32, shcore, gdi32


def _set_aware(user32: Any, shcore: Any) -> str:
    """Ask for system DPI awareness, newest API first. Returns the call that did it, or
    "already" when the process had its awareness set before (a manifest, a compatibility
    setting, an earlier call), or "" when no call worked."""
    import ctypes

    set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)  # Windows 10 1703 on
    if set_context is not None:
        if set_context(ctypes.c_void_p(SYSTEM_AWARE_CONTEXT)):
            return "SetProcessDpiAwarenessContext"
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            return "already"
    set_awareness = getattr(shcore, "SetProcessDpiAwareness", None)  # Windows 8.1 on
    if set_awareness is not None:
        result = set_awareness(PROCESS_SYSTEM_DPI_AWARE)
        if result == 0:
            return "SetProcessDpiAwareness"
        if result == E_ACCESSDENIED:
            return "already"
    set_aware = getattr(user32, "SetProcessDPIAware", None)  # Windows Vista on
    if set_aware is not None and set_aware():
        return "SetProcessDPIAware"
    return ""


def _awareness(user32: Any, shcore: Any) -> int:
    """What Windows now treats this process (its UI thread) as: UNAWARE, SYSTEM or PER_MONITOR."""
    import ctypes

    of_context = getattr(user32, "GetAwarenessFromDpiAwarenessContext", None)
    thread_context = getattr(user32, "GetThreadDpiAwarenessContext", None)
    if of_context is not None and thread_context is not None:
        value = of_context(thread_context())
        if value in (UNAWARE, SYSTEM, PER_MONITOR):
            return value
    get_awareness = getattr(shcore, "GetProcessDpiAwareness", None)
    if get_awareness is not None:
        value = ctypes.c_int(-1)
        if get_awareness(None, ctypes.byref(value)) == 0 and value.value in (UNAWARE, SYSTEM, PER_MONITOR):
            return value.value
    is_aware = getattr(user32, "IsProcessDPIAware", None)
    return SYSTEM if is_aware is not None and is_aware() else UNAWARE


def _system_dpi(user32: Any, gdi32: Any) -> int:
    for_system = getattr(user32, "GetDpiForSystem", None)  # Windows 10 1607 on
    if for_system is not None:
        dpi = int(for_system())
        if dpi > 0:
            return dpi
    dc = user32.GetDC(None)
    if not dc:
        return 96
    try:
        return int(gdi32.GetDeviceCaps(dc, LOGPIXELSY)) or 96
    finally:
        user32.ReleaseDC(None, dc)


def enable() -> dict[str, Any]:
    """Make this process DPI aware (Windows, once; call it before the first Tk window).

    Returns what happened: ``how`` (the call that worked, "already", "off" or ""),
    ``awareness`` (0 unaware, 1 system, 2 per monitor) and ``dpi`` (the system DPI, 96 when
    unaware). Never raises."""
    global _state
    if _state is not None:
        return _state
    state: dict[str, Any] = {"how": "", "awareness": UNAWARE, "dpi": 96}
    if sys.platform == "win32" and not _off():
        try:
            user32, shcore, gdi32 = _dlls()
            state["how"] = _set_aware(user32, shcore)
            state["awareness"] = _awareness(user32, shcore)
            state["dpi"] = _system_dpi(user32, gdi32) if state["awareness"] != UNAWARE else 96
        except Exception as err:  # noqa: BLE001 - never stop the panel over this
            state["how"] = f"failed: {err}"
    elif sys.platform == "win32":
        state["how"] = "off"
    _state = state
    return state


def scale() -> float:
    """How many screen pixels one design pixel is: the system DPI / 96 on Windows when the
    process is DPI aware, else 1. ``OPENFILL_UI_SCALE`` (0.5 to 4) overrides it."""
    state = enable()
    wanted = os.environ.get("OPENFILL_UI_SCALE", "").strip()
    if wanted:
        try:
            return min(4.0, max(0.5, float(wanted)))
        except ValueError:
            pass
    if state["awareness"] == UNAWARE:
        return 1.0
    return min(4.0, max(1.0, round(state["dpi"] / 96, 2)))


def describe() -> str:
    """One line for the log."""
    state = enable()
    return f"display: {state['dpi']} DPI, scale {scale():g} (awareness {state['awareness']}, {state['how'] or 'none'})"
