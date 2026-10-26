# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""How models and this computer are described in Models and settings (the panel and the
page share these; no Tk here)."""

from __future__ import annotations

from typing import Any

ARCH_NAMES = {"sdxl": "SDXL", "sd15": "SD 1.5", "flux": "FLUX", "qwen_image": "Qwen Image"}


def hardware_line(hw: Any) -> str:
    """One line for the Models view: ``RTX 4070 Laptop · 8 GB VRAM · 32 GB RAM · 328 GB free``
    (an AMD card: ``RX 7900 XT · 20 GB VRAM · ...``)."""
    gb = 1024**3
    parts = []
    name = str(getattr(hw, "gpu_name", "") or "")
    for word in ("NVIDIA ", "GeForce ", "(R)", "(TM)"):
        name = name.replace(word, "")
    name = name.removeprefix("AMD ")
    if name.startswith("Radeon RX "):
        name = name.removeprefix("Radeon ")
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
    """The grey line under a model's name: the jobs it can run (any fill model can also
    remove; erasers are marked erase-trained), its size, and on an AMD card what may not work there."""
    from openfill.catalog import job_hint

    what = job_hint(row) or needed_by or "Used by other models"
    return " · ".join(x for x in (what, row.get("size") or "", row.get("amd_note") or "") if x)


def needed_by(entries: list[Any], model_id: str) -> str:
    """'Needed by the SDXL models' for a part other models use (the Fooocus patch)."""
    users = [e for e in entries if model_id in getattr(e, "requires_models", ())]
    archs = {getattr(e, "arch", "") for e in users}
    if len(archs) == 1 and next(iter(archs)) in ARCH_NAMES:
        return f"Needed by the {ARCH_NAMES[next(iter(archs))]} models"
    return "Needed by other models" if users else "Used by other models"


def download_card_line(pre: dict[str, Any]) -> str:
    """The setup card's text when ComfyUI can be downloaded (Windows); ``pre`` is
    ``Service.comfy_preflight()``: an AMD card gets ComfyUI's version for AMD cards."""
    if pre.get("amd"):
        return (
            f"OpenFill runs on ComfyUI. Download ComfyUI's version for AMD cards ({pre['size']}, into "
            f"{pre['folder']}), or choose the folder of one you have. AMD support is new and not tested on an "
            "AMD card yet."
        )
    line = (
        f"OpenFill runs on ComfyUI. Download it ({pre['size']}, into {pre['folder']}), "
        "or choose the folder of one you have."
    )
    if pre.get("cpu") and pre.get("gpu") == "amd":
        line += (
            " ComfyUI's download for AMD cards needs a Radeon RX 7000 or newer, so on this card it would run on "
            "the processor, which is very slow. For an older Radeon, follow the AMD steps in the install guide, "
            "then choose its folder."
        )
    elif pre.get("cpu"):
        line += " No NVIDIA graphics card was found: it would run on the processor, which is very slow."
    return line


def choose_card_line(amd: bool = False) -> str:
    """The setup card's text where ComfyUI cannot be downloaded (Linux); ``amd``: the card is AMD."""
    if amd:
        return (
            "OpenFill runs on ComfyUI. Choose its folder (the one with main.py), or install it with "
            "PyTorch for ROCm, which your AMD card needs (How to install)."
        )
    return "OpenFill runs on ComfyUI. Choose its folder (the one with main.py), or install it."
