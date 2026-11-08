# SPDX-License-Identifier: Apache-2.0
"""Fake model files of the catalog's real sizes (gigabytes) that cost no disk and no time.

``truncate`` makes a sparse file on Linux and macOS. On Windows it does not: Python's
``truncate`` there writes every zero byte (the C runtime's ``_chsize_s``), and NTFS fills
a gap after a seek with zeros too unless the file is marked sparse. A test that laid out a
few catalog models wrote ~10 GB that way and took 80 s or more on a Windows runner."""

import os
from pathlib import Path

FSCTL_SET_SPARSE = 0x000900C4


def _set_sparse(fh) -> bool:
    """Mark an open file sparse (NTFS); False when the file system says no."""
    import ctypes  # noqa: PLC0415 - Windows only
    import msvcrt  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = kernel32.DeviceIoControl
    call.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    call.restype = wintypes.BOOL
    returned = wintypes.DWORD()
    handle = msvcrt.get_osfhandle(fh.fileno())
    return bool(call(handle, FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(returned), None))


def grow_open(fh, size: int) -> None:
    """Make the open binary file ``fh`` exactly ``size`` bytes long; new bytes read as zeros."""
    if os.name != "nt":
        fh.truncate(size)
        return
    fh.flush()
    end = fh.seek(0, os.SEEK_END)
    if size <= end:
        fh.truncate(size)  # shrinking writes nothing
        return
    _set_sparse(fh)
    fh.seek(size - 1)
    fh.write(b"\0")
    fh.flush()


def grow(path, size: int) -> Path:
    """``path`` (made when missing, kept otherwise) padded to ``size`` bytes, sparse."""
    path = Path(path)
    with open(path, "r+b" if path.exists() else "w+b") as fh:
        grow_open(fh, size)
    return path
