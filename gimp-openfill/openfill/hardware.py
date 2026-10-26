# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""What this computer can run: graphics card and its memory, system memory,
free disk space on the models drive, operating system.

Detection is best effort and never raises: the graphics card comes from
``nvidia-smi`` when it is installed, otherwise from NVIDIA's management
library (NVML, which the driver ships; inside the GIMP Flatpak it is there
when nvidia-smi is not), otherwise from the operating system's list of
graphics adapters (AMD and Intel cards: DXGI or the display driver's registry
key on Windows, ``/sys/class/drm`` on Linux; no vendor tools needed),
otherwise from a running ComfyUI's ``/system_stats``, otherwise from the chip
on an Apple silicon Mac, otherwise it is unknown. :func:`fit` is a pure function
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
import re
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
    gpu_source: str = ""  # "nvidia-smi", "nvml", "dxgi", "registry", "sysfs", "comfyui", "apple-silicon" or ""
    compute_capability: float | None = None  # NVIDIA CUDA compute capability, e.g. 6.1 (GTX 10) or 8.9 (RTX 40)
    gpu_vendor: str = ""  # "nvidia", "amd", "intel", "apple", "none" (no graphics card found) or "" (unknown)

    @property
    def is_amd(self) -> bool:
        """An AMD Radeon card does the work (ROCm on Linux and Windows)."""
        return self.gpu_vendor == "amd"

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
            "gpu_vendor": self.gpu_vendor,
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


# PCI vendor ids of the graphics cards OpenFill tells apart.
VENDOR_IDS = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel"}
SOFTWARE_VENDOR_ID = 0x1414  # Microsoft Basic Render Driver (no real card)


def vendor_from_name(name: str | None) -> str:
    """``nvidia`` / ``amd`` / ``intel`` / ``apple`` from an adapter name, else ``""``."""
    low = (name or "").lower()
    if "nvidia" in low or "geforce" in low or "quadro" in low:
        return "nvidia"
    if "amd" in low or "radeon" in low or "ati " in low:
        return "amd"
    if "intel" in low or low.startswith("arc ") or " arc " in low:
        return "intel"
    if low.startswith("apple") or low.startswith("mps"):
        return "apple"
    return ""


@dataclass(frozen=True)
class Adapter:
    """A graphics adapter the operating system lists. ``vram_bytes`` is its own memory, None when unknown."""

    vendor: str  # nvidia, amd, intel or ""
    name: str
    vram_bytes: int | None
    source: str  # dxgi, registry or sysfs


def pick_adapter(adapters: list[Adapter] | None) -> Adapter | None:
    """The card that would do the work: the most graphics memory, AMD and NVIDIA before Intel
    (a laptop's Intel chip next to a Radeon). Adapters of unknown vendor are skipped."""
    known = [a for a in adapters or [] if a.vendor in ("nvidia", "amd", "intel")]
    if not known:
        return None
    return max(known, key=lambda a: (a.vendor != "intel", a.vram_bytes or 0))


# Windows: DXGI lists the active adapters with their own memory as a 64-bit number.
# (Win32_VideoController.AdapterRAM is 32 bits and stops at 4 GB, so it is not used.)
class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _DxgiAdapterDesc1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint),
        ("DeviceId", ctypes.c_uint),
        ("SubSysId", ctypes.c_uint),
        ("Revision", ctypes.c_uint),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _Luid),
        ("Flags", ctypes.c_uint),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_IID_IDXGIFACTORY1 = (0x770AAE78, 0xF26F, 0x4DBA, (0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 2


def _com_call(obj: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
    """Method ``index`` of the COM object ``obj``'s vtable, as a callable taking the remaining arguments."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    fn = proto(vtable[index])
    return lambda *args: fn(obj, *args)


def dxgi_descs() -> list[tuple[str, int, int, int]]:
    """``(description, vendor id, dedicated video memory, flags)`` of each DXGI adapter (Windows only)."""
    if sys.platform != "win32":
        return []
    dxgi = ctypes.WinDLL("dxgi")  # type: ignore[attr-defined]
    d1, d2, d3, d4 = _IID_IDXGIFACTORY1
    iid = _Guid(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0 or not factory.value:
        return []
    out: list[tuple[str, int, int, int]] = []
    try:
        enum_adapters1 = _com_call(factory, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        for i in range(16):
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(i, ctypes.byref(adapter)) & 0xFFFFFFFF
            if hr == _DXGI_ERROR_NOT_FOUND or hr != 0 or not adapter.value:
                break
            try:
                desc = _DxgiAdapterDesc1()
                if _com_call(adapter, 10, ctypes.c_long, ctypes.POINTER(_DxgiAdapterDesc1))(ctypes.byref(desc)) == 0:
                    out.append((desc.Description, int(desc.VendorId), int(desc.DedicatedVideoMemory), int(desc.Flags)))
            finally:
                _com_call(adapter, 2, ctypes.c_ulong)()  # Release
    finally:
        _com_call(factory, 2, ctypes.c_ulong)()
    return out


def adapters_from_dxgi(descs: Callable[[], list[tuple[str, int, int, int]]] = dxgi_descs) -> list[Adapter]:
    """The real graphics adapters DXGI lists (the software renderer left out)."""
    out = []
    for name, vendor_id, vram, flags in descs() or []:
        if vendor_id == SOFTWARE_VENDOR_ID or flags & _DXGI_ADAPTER_FLAG_SOFTWARE:
            continue
        vendor = VENDOR_IDS.get(vendor_id, "") or vendor_from_name(name)
        out.append(Adapter(vendor, str(name).strip(), int(vram) if vram > 0 else None, "dxgi"))
    return out


DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def registry_display_keys() -> list[dict[str, Any]]:
    """The values of each display adapter's driver key (``0000``, ``0001``...) under
    :data:`DISPLAY_CLASS_KEY` (Windows only): DriverDesc, MatchingDeviceId,
    HardwareInformation.qwMemorySize, HardwareInformation.MemorySize, HardwareInformation.AdapterString."""
    if sys.platform != "win32":
        return []
    import winreg

    wanted = (
        "DriverDesc",
        "MatchingDeviceId",
        "HardwareInformation.qwMemorySize",
        "HardwareInformation.MemorySize",
        "HardwareInformation.AdapterString",
    )
    out = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS_KEY) as root:
        for i in range(64):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    values = {}
                    for name in wanted:
                        try:
                            values[name] = winreg.QueryValueEx(key, name)[0]
                        except OSError:
                            continue
                    out.append(values)
            except OSError:
                continue
    return out


def _reg_text(value: Any) -> str:
    if isinstance(value, bytes):  # AdapterString is often REG_BINARY UTF-16
        return value.decode("utf-16-le", "replace").rstrip("\x00").strip()
    return str(value or "").strip()


def _reg_int(value: Any) -> int | None:
    if isinstance(value, bytes):
        value = int.from_bytes(value[:8], "little") if value else None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def adapters_from_registry(keys: Callable[[], list[dict[str, Any]]] = registry_display_keys) -> list[Adapter]:
    """Graphics adapters from the display drivers' registry keys. The 64-bit
    ``qwMemorySize`` first; the 32-bit ``MemorySize`` stops at 4 GB."""
    out = []
    for values in keys() or []:
        name = _reg_text(values.get("DriverDesc")) or _reg_text(values.get("HardwareInformation.AdapterString"))
        if not name or "microsoft basic" in name.lower():
            continue
        match = str(values.get("MatchingDeviceId") or "").lower()
        vendor = ""
        for vid, v in VENDOR_IDS.items():
            if f"ven_{vid:04x}" in match:
                vendor = v
                break
        vendor = vendor or vendor_from_name(name)
        vram = _reg_int(values.get("HardwareInformation.qwMemorySize"))
        if vram is None:
            vram = _reg_int(values.get("HardwareInformation.MemorySize"))
        out.append(Adapter(vendor, name, vram, "registry"))
    return out


# Linux: the kernel's DRM devices. amdgpu writes the card's memory to mem_info_vram_total.
SYSFS_DRM = Path("/sys/class/drm")
PCI_IDS = (Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"), Path("/usr/share/pci.ids"))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def pci_ids_name(vendor_id: int, device_id: int, files: tuple[Path, ...] = PCI_IDS) -> str:
    """The device's name in the pci.ids database (``Navi 31 [Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M]``
    becomes ``Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M``), or ``""``."""
    vend, dev = f"{vendor_id:04x}", f"\t{device_id:04x}  "
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                inside = False
                for line in fh:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        if inside:
                            break
                        inside = line.startswith(vend + "  ")
                    elif inside and line.lower().startswith(dev):
                        name = line[len(dev) :].strip()
                        if "[" in name and name.endswith("]"):
                            name = name[name.index("[") + 1 : -1]
                        return name
        except OSError:
            continue
    return ""


def parse_rocm_smi(text: str) -> dict[str, tuple[str, int | None]]:
    """``{card: (name, vram bytes)}`` from ``rocm-smi --showproductname --showmeminfo vram --json``."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    out: dict[str, tuple[str, int | None]] = {}
    for card, info in data.items() if isinstance(data, dict) else []:
        if not isinstance(info, dict) or not str(card).startswith("card"):
            continue
        name = ""
        for key in ("Card Series", "Card series", "Card SKU", "Card Model", "Card model"):
            value = str(info.get(key) or "").strip()
            if value and not value.lower().startswith(("0x", "n/a")):
                name = value
                break
        vram = _reg_int(info.get("VRAM Total Memory (B)"))
        out[str(card)] = (name, vram)
    return out


def rocm_smi_cards(runner: Callable[..., Any] = subprocess.run) -> dict[str, tuple[str, int | None]]:
    """What ``rocm-smi`` says about each AMD card, or ``{}`` when it is not installed."""
    exe = shutil.which("rocm-smi")
    if exe is None:
        return {}
    try:
        r = runner(
            [exe, "--showproductname", "--showmeminfo", "vram", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_rocm_smi(r.stdout or "") if getattr(r, "returncode", 1) == 0 else {}


def adapters_from_sysfs(
    root: Path = SYSFS_DRM,
    pci_name: Callable[[int, int], str] = pci_ids_name,
    rocm: Callable[[], dict[str, tuple[str, int | None]]] = rocm_smi_cards,
) -> list[Adapter]:
    """Graphics cards under ``/sys/class/drm/card*/device`` (Linux). The name is the
    driver's ``product_name`` when it has one, else rocm-smi's, else the pci.ids name."""
    out = []
    try:
        cards = sorted(p for p in root.iterdir() if p.name.startswith("card") and p.name[4:].isdigit())
    except OSError:
        return []
    smi: dict[str, tuple[str, int | None]] | None = None
    for card in cards:
        dev = card / "device"
        try:
            vendor_id = int(_read(dev / "vendor") or "0", 16)
            device_id = int(_read(dev / "device") or "0", 16)
        except ValueError:
            continue
        vendor = VENDOR_IDS.get(vendor_id, "")
        if not vendor:
            continue
        vram = _reg_int(_read(dev / "mem_info_vram_total"))
        name = _read(dev / "product_name")
        if not name and vendor == "amd":
            if smi is None:
                try:
                    smi = rocm() or {}
                except Exception:  # noqa: BLE001 - rocm-smi is optional
                    smi = {}
            name = (smi.get(card.name) or ("", None))[0]
        if not name:
            name = pci_name(vendor_id, device_id)
        prefix = {"amd": "AMD", "nvidia": "NVIDIA", "intel": "Intel"}[vendor]
        if not name:
            name = f"{prefix} graphics card"
        elif not name.lower().startswith(prefix.lower()):
            name = f"{prefix} {name}"
        out.append(Adapter(vendor, name, vram, "sysfs"))
    return out


def os_adapters(platform_name: str = sys.platform) -> list[Adapter] | None:
    """The graphics adapters the operating system lists: DXGI, else the registry (Windows);
    sysfs (Linux). None when they could not be listed (and on macOS, where Apple silicon
    has its own path)."""
    if platform_name == "win32":
        asked = False
        for source in (adapters_from_dxgi, adapters_from_registry):
            try:
                found = source()
                asked = True
            except Exception:  # noqa: BLE001 - try the next source
                continue
            if found:
                return found
        return [] if asked else None
    if platform_name.startswith("linux"):
        return adapters_from_sysfs() if SYSFS_DRM.is_dir() else None
    return None


# ---------------------------------------------------------------- AMD ----
# ComfyUI's Windows build for AMD cards (ROCm 7.2) carries code for RDNA 3 (RX 7000),
# RDNA 3.5 (Ryzen AI 300 and Ryzen AI Max) and RDNA 4 (RX 9000) only: gfx1100-1102,
# gfx1150-1151 and gfx1200-1201.
_AMD_BUILD_YES = re.compile(
    r"\brx\s*[79]\d{3}|\bpro\s*w7\d{3}|\bai\s*pro\s*r9\d{3}|\br9[67]00\b|\b(?:890m|880m|8060s|8050s|8040s)\b"
)
_AMD_BUILD_NO = re.compile(
    r"\brx\s*[56]\d{3}|\brx\s*[45]\d0\b|\bvega\b|\bradeon\s*vii\b|\bpro\s*w[56]\d{3}|\b(?:r[579]|hd)\s*\d"
    r"|\b(?:780m|760m|740m|680m|660m|610m)\b"
)


def amd_build_support(name: str | None) -> str:
    """Whether ComfyUI's build for AMD cards runs on the Radeon called ``name``:
    ``yes`` (RX 7000, RX 9000, Ryzen AI 300 and Max), ``no`` (older Radeons) or
    ``unknown`` (a name that does not say, such as "AMD Radeon(TM) Graphics")."""
    low = (name or "").lower()
    if _AMD_BUILD_YES.search(low):
        return "yes"
    if _AMD_BUILD_NO.search(low):
        return "no"
    return "unknown"


KFD_NODES = Path("/sys/class/kfd/kfd/topology/nodes")


def amd_gfx_targets(root: Path = KFD_NODES) -> list[int]:
    """The ``gfx_target_version`` of each AMD GPU the ROCm kernel driver lists (Linux),
    such as 110000 for gfx1100 or 100301 for gfx1031. The processor's node (0) is left out."""
    out = []
    try:
        nodes = sorted(root.iterdir())
    except OSError:
        return []
    for node in nodes:
        for line in _read(node / "properties").splitlines():
            key, _, value = line.partition(" ")
            if key == "gfx_target_version":
                try:
                    target = int(value.strip())
                except ValueError:
                    continue
                if target > 0:
                    out.append(target)
    return out


def rocm_env(
    targets: list[int] | None = None, environ: dict[str, str] | None = None, platform_name: str = sys.platform
) -> dict[str, str]:
    """Environment ComfyUI needs on Linux for an AMD card that PyTorch's ROCm build has no
    code for, as ComfyUI's README says: ``HSA_OVERRIDE_GFX_VERSION=10.3.0`` for the RDNA 2
    cards other than the RX 6800 and 6900 (gfx1031 to gfx1036, such as the RX 6700 and 6600),
    ``11.0.0`` for the Radeon 780M and 760M (gfx1103). Empty when it is not needed, when the
    user set it already, or when the cards would need different values."""
    if not platform_name.startswith("linux"):
        return {}
    environ = os.environ if environ is None else environ
    if environ.get("HSA_OVERRIDE_GFX_VERSION"):
        return {}
    targets = amd_gfx_targets() if targets is None else targets
    if not targets:
        return {}
    families = {t // 100 for t in targets}  # 1003 (gfx103x), 1100 (gfx110x)...
    if len(families) != 1:
        return {}
    family = families.pop()
    if family == 1003 and any(t != 100300 for t in targets):
        return {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}
    if family == 1100 and any(t == 110003 for t in targets):
        return {"HSA_OVERRIDE_GFX_VERSION": "11.0.0"}
    return {}


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
    adapters: Callable[[], list[Adapter] | None] | None = None,
) -> Hardware:
    """Detect the hardware (see the module docstring). Never raises."""
    ram = system_ram()
    name: str | None = None
    vram: int | None = None
    source = ""
    vendor = ""
    try:
        found = nvidia()
    except Exception:  # noqa: BLE001
        found = None
    if found:
        name, vram = found
        source, vendor = "nvidia-smi", "nvidia"
    else:
        try:
            found = nvml()
        except Exception:  # noqa: BLE001
            found = None
        if found:
            name, vram = found
            source, vendor = "nvml", "nvidia"
    listed: list[Adapter] | None = None  # None: not asked, or the system could not be asked
    if not source:
        try:
            got = (adapters or os_adapters)()
            listed = None if got is None else list(got)
        except Exception:  # noqa: BLE001
            listed = None
        best = pick_adapter(listed)
        if best is not None and best.vram_bytes:
            name, vram, source, vendor = best.name, best.vram_bytes, best.source, best.vendor
    if not source and comfy_url:
        try:
            stats = comfy(comfy_url)
        except Exception:  # noqa: BLE001
            stats = None
        if stats:
            name, vram, comfy_ram = stats
            ram = ram or comfy_ram
            source = "comfyui"
            vendor = vendor_from_name(name)
    if not source:
        try:
            chip = apple()
        except Exception:  # noqa: BLE001
            chip = None
        if chip:
            name, vram, ram = chip
            source, vendor = "apple-silicon", "apple"
    if not source:
        best = pick_adapter(listed)
        if best is not None:  # a card whose memory could not be read: its name and maker still help
            name, source, vendor = best.name, best.source, best.vendor
        elif listed is not None:
            vendor = "none"  # the system was asked and lists no graphics card
    cap = None
    if source in ("nvidia-smi", "nvml"):
        try:
            cap = compute()
        except Exception:  # noqa: BLE001
            cap = None
    return Hardware(name, vram, ram, disk_free(models_root), os_name(), source, cap, vendor)


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
