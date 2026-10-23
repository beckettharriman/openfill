# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""How models and this computer are described in Models and settings (the panel and the
page share these; no Tk here)."""

from __future__ import annotations

from typing import Any

CAPABILITY_WORDS = {"fill": "fill", "expand": "expand", "txt2img": "create", "remove": "remove", "edit": "edit"}
ARCH_NAMES = {"sdxl": "SDXL", "sd15": "SD 1.5", "flux": "FLUX", "qwen_image": "Qwen Image"}


def hardware_line(hw: Any) -> str:
    """One line for the Models view: ``RTX 4070 Laptop · 8 GB VRAM · 32 GB RAM · 328 GB free``."""
    gb = 1024**3
    parts = []
    name = str(getattr(hw, "gpu_name", "") or "")
    for word in ("NVIDIA ", "GeForce ", "(R)", "(TM)"):
        name = name.replace(word, "")
    name = name.removesuffix(" GPU").replace(" with Max-Q Design", " Max-Q").strip()
    if name:
        parts.append(name)
    if getattr(hw, "vram_bytes", 0):
        parts.append(f"{hw.vram_bytes / gb:.0f} GB VRAM")
    elif not name:
        parts.append("No graphics card found")
    if getattr(hw, "ram_bytes", 0):
        parts.append(f"{hw.ram_bytes / gb:.0f} GB RAM")
    if getattr(hw, "disk_free_bytes", None) is not None:
        parts.append(f"{hw.disk_free_bytes / gb:.0f} GB free")
    return " · ".join(parts)


def model_detail(row: dict[str, Any], needed_by: str = "") -> str:
    """The grey line under a model's name: what it does and its size."""
    caps = [CAPABILITY_WORDS.get(c, c) for c in row.get("capabilities") or [] if c != "patch"]
    what = ", ".join(caps)
    what = what[:1].upper() + what[1:]
    if not caps:
        what = needed_by or "Used by other models"
    return " · ".join(x for x in (what, row.get("size") or "") if x)


def needed_by(entries: list[Any], model_id: str) -> str:
    """'Needed by the SDXL models' for a part other models use (the Fooocus patch)."""
    users = [e for e in entries if model_id in getattr(e, "requires_models", ())]
    archs = {getattr(e, "arch", "") for e in users}
    if len(archs) == 1 and next(iter(archs)) in ARCH_NAMES:
        return f"Needed by the {ARCH_NAMES[next(iter(archs))]} models"
    return "Needed by other models" if users else "Used by other models"
