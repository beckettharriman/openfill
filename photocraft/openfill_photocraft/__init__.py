# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""OpenFill for PhotoCraft: a companion app that drives a running PhotoCraft over its
control channel and runs OpenFill's ComfyUI pipeline (the ``openfill`` package)."""

# OpenFill for PhotoCraft's own version. It is not a PhotoCraft version.
__version__ = "0.2.6"

# The oldest and newest PhotoCraft releases this version was tested on.
PHOTOCRAFT_TESTED = ("0.12", "0.20")


def about() -> str:
    """One line that can't be read as a PhotoCraft version."""
    oldest, newest = PHOTOCRAFT_TESTED
    return f"OpenFill for PhotoCraft {__version__} · works with PhotoCraft {oldest} to {newest}"
