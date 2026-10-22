# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Concrete backend implementations."""

from .a1111 import A1111Backend
from .comfy import ComfyBackend

__all__ = ["A1111Backend", "ComfyBackend"]
