# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""What this computer can run: graphics card and its memory, system memory,
free disk space on the models drive, operating system.

Detection is best effort and never raises: the graphics card comes from
``nvidia-smi`` when it is installed, otherwise from NVIDIA's management
library (NVML, which the driver ships; inside the GIMP Flatpak it is there
when nvidia-smi is not), otherwise from a running ComfyUI's
``/system_stats``, otherwise from the chip on an Apple silicon Mac,
otherwise it is unknown. :func:`fit` is a pure function
that turns a catalog entry and a :class:`Hardware` into a :class:`FitVerdict`
(runs comfortably, runs with offloading, too big, unknown).

Standard library only; no GIMP imports. Detection takes up to a few seconds
(``nvidia-smi`` start-up), so the Models tab calls it from a worker thread.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

GB = 1024**3

COMFORTABLE = "comfortable"
OFFLOAD = "offload"
TOO_BIG = "too_big"
UNKNOWN = "unknown"

FIT_LABELS = {
    COMFORTABLE: "Runs well on this PC",
    OFFLOAD: "Runs on this PC, slower (uses system memory too)",
    TOO_BIG: "Too big for this PC",
    UNKNOWN: "Could not check this PC",
}

# Graphics memory a model family needs at the least, even with offloading
# (ComfyUI keeps the active blocks and the latent on the card). Qwen Edit Q4
# offloaded on an 8 GB card took 664 s for one job, so it needs more than 8 GB.
OFFLOAD_FLOOR_GB = {"sd15": 2.0, "sdxl": 4.0, "flux": 6.0, "qwen_image": 10.0, "inpaint_model": 1.0}
# Working memory on top of the weights (activations, VAE decode, CUDA context).
OVERHEAD_GB = {"sd15": 1.5, "sdxl": 1.5, "flux": 2.0, "qwen_image": 2.5, "inpaint_model": 0.5}
# Spare free disk space required after a download.
DISK_MARGIN = 1 * GB


@dataclass(frozen=True)
class Hardware:
    """What was detected; ``None`` means unknown."""

    gpu_name: str | None = None
    vram_bytes: int | None = None
    ram_bytes: int | None = None
    disk_free_bytes: int | None = None
    os_name: str = ""
    gpu_source: str = ""  # "nvidia-smi", "nvml", "comfyui", "apple-silicon" or "" (unknown)
    compute_capability: float | None = None  # NVIDIA CUDA compute capability, e.g. 6.1 (GTX 10) or 8.9 (RTX 40)

    @property
    def vram_gb(self) -> float | None:
        """Graphics memory in GiB, or None."""
        return None if self.vram_bytes is None else self.vram_bytes / GB

    @property
    def ram_gb(self) -> float | None:
        """System memory in GiB, or None."""
        return None if self.ram_bytes is None else self.ram_bytes / GB

    def summary(self) -> str:
        """One plain line for the Models tab: ``NVIDIA GeForce RTX 4070 Laptop GPU, 8 GB graphics memory, 32 GB RAM``."""
        parts = []
        if self.gpu_name:
            parts.append(self.gpu_name)
        if self.vram_bytes:
            parts.append(f"{self.vram_bytes / GB:.0f} GB graphics memory")
        elif not self.gpu_name:
            parts.append("graphics card not detected")
        if self.ram_bytes:
            parts.append(f"{self.ram_bytes / GB:.0f} GB memory")
        if self.disk_free_bytes is not None:
            parts.append(f"{self.disk_free_bytes / GB:.0f} GB free disk space")
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        """Plain dict (for logs and evidence; holds nothing personal beyond the hardware)."""
        return {
            "gpu_name": self.gpu_name,
            "vram_bytes": self.vram_bytes,
            "ram_bytes": self.ram_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "os_name": self.os_name,
            "gpu_source": self.gpu_source,
        }


@dataclass(frozen=True)
class FitVerdict:
    """Whether an entry fits: ``level`` is one of comfortable/offload/too_big/unknown."""

    level: str
    reason: str = ""

    @property
    def label(self) -> str:
        """Short text for the grey detail line of a model row."""
        return FIT_LABELS[self.level]

    @property
    def fits(self) -> bool:
        """True for comfortable and offload."""
        return self.level in (COMFORTABLE, OFFLOAD)


# --------------------------------------------------------------- detection ----


def _hidden() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def parse_nvidia_smi(text: str) -> tuple[str, int] | None:
    """``"NVIDIA GeForce RTX 4070 Laptop GPU, 8188"`` -> (name, bytes) of the card with the most memory."""
    best: tuple[str, int] | None = None
    for line in (text or "").splitlines():
        name, _, mem = line.rpartition(",")
        try:
            mib = int(float(mem.strip()))
        except ValueError:
            continue
        name = name.strip()
        if name and (best is None or mib * 1024 * 1024 > best[1]):
            best = (name, mib * 1024 * 1024)
    return best


def gpu_from_nvidia_smi(runner: Callable[..., Any] = subprocess.run) -> tuple[str, int] | None:
    """Name and total memory of the largest NVIDIA card, or None when nvidia-smi is missing or fails."""
    exe = shutil.which("nvidia-smi")
    if exe is None and sys.platform == "win32":
        candidate = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(candidate) if candidate.is_file() else None
    if exe is None:
        return None
    try:
        r = runner(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            **_hidden(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    return parse_nvidia_smi(r.stdout or "")


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def _load_nvml() -> Any:
    if sys.platform == "win32":
        names = ["nvml.dll", str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "nvml.dll")]
    elif sys.platform == "darwin":
        return None
    else:
        names = ["libnvidia-ml.so.1", "libnvidia-ml.so"]
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def gpu_from_nvml(loader: Callable[[], Any] = _load_nvml) -> tuple[str, int] | None:
    """Name and total memory of the largest NVIDIA card from the driver's NVML library, or None.

    Needs no nvidia-smi, so it also works inside the GIMP Flatpak, whose NVIDIA
    GL extension ships ``libnvidia-ml.so.1`` but no ``nvidia-smi``.
    """
    try:
        lib = loader()
        if lib is None or lib.nvmlInit_v2() != 0:
            return None
    except (OSError, AttributeError):
        return None
    best: tuple[str, int] | None = None
    try:
        count = ctypes.c_uint(0)
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
            return None
        for i in range(count.value):
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(handle)) != 0:
                continue
            mem = _NvmlMemory()
            name = ctypes.create_string_buffer(96)
            if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) != 0:
                continue
            if lib.nvmlDeviceGetName(handle, name, ctypes.sizeof(name)) != 0:
                continue
            label = name.value.decode("utf-8", "replace").strip()
            if label and mem.total > 0 and (best is None or mem.total > best[1]):
                best = (label, int(mem.total))
    except (OSError, AttributeError, ValueError):
        return None
    finally:
        try:
            lib.nvmlShutdown()
        except (OSError, AttributeError):
            pass
    return best


def parse_compute_capability(text: str) -> float | None:
    """Lowest ``compute_cap`` in ``nvidia-smi --query-gpu=compute_cap`` output (one card per line)."""
    caps = []
    for line in (text or "").splitlines():
        try:
            caps.append(float(line.strip()))
        except ValueError:
            continue
    return min(caps) if caps else None


def gpu_compute_capability(
    runner: Callable[..., Any] = subprocess.run, loader: Callable[[], Any] = _load_nvml
) -> float | None:
    """CUDA compute capability of the NVIDIA card (the lowest, with several), or None.

    From ``nvidia-smi`` when it is there, else from NVML. Decides which PyTorch
    build can run the card: CUDA 12.8 and newer builds start at 7.5 (RTX 20 / GTX 16).
    """
    exe = shutil.which("nvidia-smi")
    if exe is None and sys.platform == "win32":
        candidate = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(candidate) if candidate.is_file() else None
    if exe is not None:
        try:
            r = runner(
                [exe, "--query-gpu=compute_cap", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
                **_hidden(),
            )
            if getattr(r, "returncode", 1) == 0:
                cap = parse_compute_capability(r.stdout or "")
                if cap is not None:
                    return cap
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        lib = loader()
        if lib is None or lib.nvmlInit_v2() != 0:
            return None
    except (OSError, AttributeError):
        return None
    caps = []
    try:
        count = ctypes.c_uint(0)
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) == 0:
            for i in range(count.value):
                handle, major, minor = ctypes.c_void_p(), ctypes.c_int(0), ctypes.c_int(0)
                if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(handle)) != 0:
                    continue
                if lib.nvmlDeviceGetCudaComputeCapability(handle, ctypes.byref(major), ctypes.byref(minor)) == 0:
                    caps.append(major.value + minor.value / 10)
    except (OSError, AttributeError, ValueError):
        return None
    finally:
        try:
            lib.nvmlShutdown()
        except (OSError, AttributeError):
            pass
    return min(caps) if caps else None


def parse_system_stats(data: dict[str, Any]) -> tuple[str, int, int | None] | None:
    """(gpu name, vram bytes, ram bytes) from ComfyUI's ``/system_stats``; None without a GPU device."""
    devices = [d for d in data.get("devices") or [] if isinstance(d, dict)]
    gpus = [d for d in devices if str(d.get("type", "")).lower() not in ("cpu", "")]
    ram = (data.get("system") or {}).get("ram_total")
    ram_bytes = int(ram) if isinstance(ram, (int, float)) and ram > 0 else None
    if not gpus:
        return None
    best = max(gpus, key=lambda d: d.get("vram_total") or 0)
    vram = best.get("vram_total")
    if not isinstance(vram, (int, float)) or vram <= 0:
        return None
    name = str(best.get("name") or best.get("type") or "graphics card")
    if str(best.get("type", "")).lower() == "mps" or name.startswith("mps"):
        # Apple Silicon: ComfyUI reports the whole unified memory as video memory.
        # Budget about 70% of it for the model and leave the rest as system memory.
        total = int(vram)
        vram = int(total * 0.7)
        ram_bytes = total - vram
    # ComfyUI names CUDA devices "cuda:0 NVIDIA GeForce ... : cudaMallocAsync"
    if name.startswith(("cuda:", "xpu:", "mps")) and " " in name:
        name = name.split(" ", 1)[1]
    name = name.split(" : ", 1)[0].strip()
    return name, int(vram), ram_bytes


def backend_device(data: dict[str, Any]) -> tuple[str, str]:
    """``(kind, description)`` of the device a running ComfyUI computes on, from its ``/system_stats``.

    ``kind`` is ``"gpu"``, ``"cpu"`` (ComfyUI was started with ``--cpu`` or its PyTorch
    cannot use the graphics card: every generation then runs on the processor) or
    ``"unknown"`` (no device listed).
    """
    devices = [d for d in data.get("devices") or [] if isinstance(d, dict)]
    if not devices:
        return "unknown", ""
    gpus = [d for d in devices if str(d.get("type", "")).lower() not in ("cpu", "")]
    best = max(gpus or devices, key=lambda d: d.get("vram_total") or 0)
    name = str(best.get("name") or best.get("type") or "?").split(" : ", 1)[0].strip()
    vram = best.get("vram_total")
    memory = f", {vram / GB:.1f} GB" if isinstance(vram, (int, float)) and vram > 0 else ""
    return ("gpu" if gpus else "cpu"), f"{name} ({best.get('type', '?')}{memory})"


def gpu_from_apple_silicon(
    runner: Callable[..., Any] = subprocess.run, platform_name: str = sys.platform, machine: str | None = None
) -> tuple[str, int, int] | None:
    """(chip name, graphics budget, system memory) on an Apple silicon Mac, else None.

    The GPU shares the unified memory; like :func:`parse_system_stats` for an MPS
    device, about 70% of it is budgeted for the model and the rest left as system memory.
    """
    if platform_name != "darwin" or (machine or platform.machine()) != "arm64":
        return None
    try:
        r = runner(
            ["sysctl", "-n", "machdep.cpu.brand_string", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        brand, mem = (r.stdout or "").strip().splitlines()[:2]
        total = int(mem)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(r, "returncode", 1) != 0 or total <= 0:
        return None
    vram = int(total * 0.7)
    return brand.strip() or "Apple silicon", vram, total - vram


def gpu_from_comfyui(
    url: str, opener: Callable[..., Any] = urlopen, timeout: float = 2.0
) -> tuple[str, int, int | None] | None:
    """``parse_system_stats`` of the ComfyUI at ``url``, or None when it does not answer."""
    if not url:
        return None
    try:
        req = Request(url.rstrip("/") + "/system_stats", headers={"User-Agent": "gimp-openfill"})
        with opener(req, timeout=timeout) as resp:
            return parse_system_stats(json.loads(resp.read(1 << 20).decode("utf-8")))
    except Exception:  # noqa: BLE001 - a backend that is down or answers garbage is "unknown"
        return None


def system_ram() -> int | None:
    """Total physical memory in bytes, or None."""
    try:
        if sys.platform == "win32":

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [  # noqa: RUF012 - ctypes layout
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
                return int(stat.ullTotalPhys)
            return None
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) if out.returncode == 0 else None
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return int(pages * size) if pages > 0 and size > 0 else None
    except Exception:  # noqa: BLE001 - unknown is a valid answer
        return None


def disk_free(path: str | Path | None) -> int | None:
    """Free bytes on the drive of ``path`` (its nearest existing parent), or None."""
    if not path:
        return None
    p = Path(path)
    for candidate in (p, *p.parents):
        if candidate.exists():
            try:
                return int(shutil.disk_usage(candidate).free)
            except OSError:
                return None
    return None


def os_name() -> str:
    """``Windows 11``, ``Linux 6.8``, ``macOS 15.1``..."""
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if system == "Windows":
        build = sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else 0
        return "Windows 11" if build >= 22000 else f"Windows {platform.release()}"
    return f"{system} {platform.release()}".strip()


def detect(
    models_root: str | Path | None = None,
    comfy_url: str | None = None,
    nvidia: Callable[[], tuple[str, int] | None] = gpu_from_nvidia_smi,
    nvml: Callable[[], tuple[str, int] | None] = gpu_from_nvml,
    compute: Callable[[], float | None] = gpu_compute_capability,
    comfy: Callable[[str], Any] = gpu_from_comfyui,
    apple: Callable[[], tuple[str, int, int] | None] = gpu_from_apple_silicon,
) -> Hardware:
    """Detect the hardware (see the module docstring). Never raises."""
    ram = system_ram()
    name: str | None = None
    vram: int | None = None
    source = ""
    try:
        found = nvidia()
    except Exception:  # noqa: BLE001
        found = None
    if found:
        name, vram = found
        source = "nvidia-smi"
    else:
        try:
            found = nvml()
        except Exception:  # noqa: BLE001
            found = None
        if found:
            name, vram = found
            source = "nvml"
    if not source and comfy_url:
        try:
            stats = comfy(comfy_url)
        except Exception:  # noqa: BLE001
            stats = None
        if stats:
            name, vram, comfy_ram = stats
            ram = ram or comfy_ram
            source = "comfyui"
    if not source:
        try:
            chip = apple()
        except Exception:  # noqa: BLE001
            chip = None
        if chip:
            name, vram, ram = chip
            source = "apple-silicon"
    cap = None
    if source in ("nvidia-smi", "nvml"):
        try:
            cap = compute()
        except Exception:  # noqa: BLE001
            cap = None
    return Hardware(name, vram, ram, disk_free(models_root), os_name(), source, cap)


# --------------------------------------------------------------------- fit ----


def _family_arch(entry: Any) -> str:
    return getattr(entry, "arch", "") or "sd15"


def weights_bytes(entry: Any) -> int:
    """Bytes of the files that sit in graphics memory while sampling (the
    diffusion model; text encoders and VAE are loaded one after the other)."""
    files = list(getattr(entry, "files", ()) or ())
    main = [f for f in files if getattr(f, "role", "") in ("checkpoint", "unet", "inpaint_model")]
    return sum(int(getattr(f, "size", 0) or 0) for f in (main or files))


def vram_needed_gb(entry: Any) -> float:
    """Graphics memory for comfortable use: the catalog's ``min_vram_gb`` when it
    has one, else the diffusion weights plus a per-family working margin."""
    declared = float(getattr(entry, "min_vram_gb", 0) or 0)
    if declared > 0:
        return declared
    arch = _family_arch(entry)
    weights = weights_bytes(entry) / GB
    if arch in ("sd15", "sdxl"):
        weights *= 0.8  # single-file checkpoints also hold the text encoders and VAE
    return round(weights + OVERHEAD_GB.get(arch, 1.5), 1)


def fit(entry: Any, hw: Hardware, already_on_disk: int = 0) -> FitVerdict:
    """Can ``entry`` run on ``hw``?

    - comfortable: graphics memory >= what the model needs;
    - offload: less graphics memory, but at least the family's floor, and enough
      system memory to hold the rest of the weights (ComfyUI offloads automatically);
    - too_big: below the floor, too little system memory, or not enough free disk
      space for the files still to download (``total_size - already_on_disk``);
    - unknown: the graphics memory could not be detected.
    """
    total = int(getattr(entry, "total_size", 0) or 0)
    to_download = max(0, total - int(already_on_disk or 0))
    if hw.disk_free_bytes is not None and to_download and hw.disk_free_bytes < to_download + DISK_MARGIN:
        return FitVerdict(
            TOO_BIG,
            f"needs {to_download / GB:.1f} GB of disk space, {hw.disk_free_bytes / GB:.1f} GB is free",
        )
    need = vram_needed_gb(entry)
    if hw.vram_bytes is None:
        return FitVerdict(UNKNOWN, f"needs about {need:g} GB of graphics memory; this PC's could not be detected")
    vram = hw.vram_bytes / GB
    if vram + 0.25 >= need:  # 8 GB cards report 7.9-8.0 GB
        return FitVerdict(COMFORTABLE, f"needs about {need:g} GB of graphics memory, this PC has {vram:.0f} GB")
    floor = OFFLOAD_FLOOR_GB.get(_family_arch(entry), 2.0)
    spill = max(0.0, weights_bytes(entry) / GB - vram) + 4.0  # the rest of the weights + the OS and GIMP
    ram = hw.ram_bytes / GB if hw.ram_bytes else None
    if vram + 0.25 >= floor and (ram is None or ram >= spill):
        return FitVerdict(
            OFFLOAD,
            f"needs about {need:g} GB of graphics memory, this PC has {vram:.0f} GB; "
            "the rest is kept in system memory, which is slower",
        )
    why = f"needs about {need:g} GB of graphics memory, this PC has {vram:.0f} GB"
    if ram is not None and ram < spill and vram + 0.25 >= floor:
        why += f" and {ram:.0f} GB of system memory is not enough to make up for it"
    return FitVerdict(TOO_BIG, why)


if __name__ == "__main__":  # python -m openfill.hardware [models folder] [comfy url]
    hw = detect(sys.argv[1] if len(sys.argv) > 1 else None, sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(hw.to_json(), indent=2))
    print(hw.summary())
