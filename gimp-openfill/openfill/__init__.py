# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""OpenFill: generative fill, expand and text-to-image for GIMP 3.

Everything in this package except ``gimp_io`` is plain Python with no GIMP or
GTK dependency, so it can be unit-tested and reused outside GIMP.
"""

import subprocess as _subprocess
import sys as _sys

__version__ = "2.0.0-beta.6"
__all__ = ["__version__"]


def _hide_child_consoles() -> None:
    """On Windows, start every child process without a console window.

    GIMP's Python and the supervisor run without a console, so a console
    program started from them (by us or by a library) would otherwise open a
    blank terminal window. Explicit creationflags are kept as given."""
    if _sys.platform != "win32" or getattr(_subprocess.Popen, "_openfill_hidden", False):
        return
    original = _subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, N807
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    _subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    _subprocess.Popen._openfill_hidden = True  # type: ignore[attr-defined]


_hide_child_consoles()
