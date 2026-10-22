# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Getting a working ComfyUI onto a PC that has none.

Pure Python, no GTK. Four parts:

* the pinned manifest (``models/comfy_setup.json``: release asset, size, SHA-256,
  custom node commit, pip package);
* :func:`detect_comfy`, which finds a ComfyUI the user already has;
* :func:`preflight`, the checks before a multi-gigabyte download (graphics card,
  free disk space, port);
* :func:`run_setup`, the download, verify, unpack and install sequence that the
  supervisor runs as one task.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from . import downloads
from .catalog import NodeSpec, human_size
from .config import comfy_layout

MANIFEST_FILENAME = "comfy_setup.json"
GB = 1024**3
# Free space the whole first-run set needs on the target drive: the 2 GB archive,
# its unpacked copy, the two starting models and some room to work.
NEED_FREE_BYTES = 12 * GB
MIN_VRAM_GB = 4.0
UNPACK_MARKER = ".openfill-unpacking"
STATES = ("downloading", "verifying", "unpacking", "nodes", "packages")
# Share of the overall progress bar each state covers (download and unpacking dominate).
STATE_SPAN = {
    "downloading": (0.0, 0.55),
    "verifying": (0.55, 0.60),
    "unpacking": (0.60, 0.90),
    "nodes": (0.90, 0.95),
    "packages": (0.95, 1.0),
}
_PIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[0-9][A-Za-z0-9.+!_-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class SetupError(RuntimeError):
    """A setup step failed; the message is written for the user."""


# ---------------------------------------------------------------- manifest ----


@dataclass(frozen=True)
class SetupManifest:
    """The validated ``comfy_setup.json``."""

    version: str
    release_page: str
    folder: str
    archive_name: str
    archive_url: str
    archive_size: int
    archive_sha256: str
    unpacked_bytes: int
    amd_instructions: str
    nodes: tuple[NodeSpec, ...]
    pip: tuple[str, ...]
    # The same release built for NVIDIA cards below ``older_below_compute`` (CUDA 12.6 PyTorch):
    # (archive name, url, size, sha256), or None.
    older_asset: tuple[str, str, int, str] | None = None
    older_below_compute: float = 0.0

    def needs_older_build(self, compute_capability: float | None) -> bool:
        """True when the card's compute capability is below what the main build's PyTorch supports."""
        return bool(self.older_asset and compute_capability and compute_capability < self.older_below_compute)

    def for_older_gpus(self) -> SetupManifest:
        """This manifest with the archive for older NVIDIA cards (same folder, nodes and packages)."""
        if not self.older_asset:
            return self
        name, url, size, sha = self.older_asset
        return replace(self, archive_name=name, archive_url=url, archive_size=size, archive_sha256=sha)

    @property
    def download_bytes(self) -> int:
        """The ComfyUI archive."""
        return self.archive_size


def parse_manifest(raw: Mapping[str, Any]) -> SetupManifest:
    """Validate ``raw``; raises ``SetupError`` when anything is missing or unsafe."""
    try:
        c = raw["comfyui"]
        a = c["asset"]
        url = str(a["url"])
        name = str(a["name"])
        folder = str(c["folder"])
        sha = str(a["sha256"]).lower()
        size = int(a["size"])
        nodes = tuple(NodeSpec(n["repo"], n["folder"], n["commit"], n.get("license", "")) for n in raw.get("nodes", []))
        pip = tuple(str(p) for p in raw.get("pip", []))
        older = c.get("older_gpus") or {}
        older_asset = None
        if older:
            oa = older["asset"]
            older_asset = (str(oa["name"]), str(oa["url"]), int(oa["size"]), str(oa["sha256"]).lower())
        older_below = float(older.get("below_compute") or 0.0)
    except (KeyError, TypeError, ValueError) as err:
        raise SetupError(f"comfy_setup.json is incomplete: {err!r}") from err
    for u in (url, older_asset[1] if older_asset else url):
        if not u.startswith("https://github.com/comfyanonymous/ComfyUI/releases/download/"):
            raise SetupError(f"the ComfyUI archive must come from the official GitHub releases: {u}")
    if older_asset and (
        not downloads.is_safe_name(older_asset[0]) or not _SHA_RE.match(older_asset[3]) or older_asset[2] <= 0
    ):
        raise SetupError("comfy_setup.json: the archive for older cards needs a safe name, its size and a SHA-256")
    if not downloads.is_safe_name(name) or not downloads.is_safe_name(folder):
        raise SetupError("comfy_setup.json holds an unsafe file or folder name")
    if not _SHA_RE.match(sha) or size <= 0:
        raise SetupError("comfy_setup.json needs the archive size and a SHA-256")
    for n in nodes:
        try:
            downloads.validate_node_spec(n.repo, n.folder, n.commit)
        except ValueError as err:
            raise SetupError(str(err)) from err
    for p in pip:
        if not _PIP_RE.match(p):
            raise SetupError(f"pip packages must be pinned with ==: {p!r}")
    return SetupManifest(
        version=str(c.get("version", "")),
        release_page=str(c.get("release_page", "")),
        folder=folder,
        archive_name=name,
        archive_url=url,
        archive_size=size,
        archive_sha256=sha,
        unpacked_bytes=int(c.get("unpacked_bytes") or 0),
        amd_instructions=str(c.get("amd_instructions", "")),
        nodes=nodes,
        pip=pip,
        older_asset=older_asset,
        older_below_compute=older_below,
    )


def manifest_candidates() -> list[Path]:
    """Where ``comfy_setup.json`` may live: next to the plug-in (copied install) or in the repo."""
    plugin_dir = Path(__file__).resolve().parent.parent
    return [plugin_dir / "models" / MANIFEST_FILENAME, plugin_dir.parent / "models" / MANIFEST_FILENAME]


def load_manifest(path: Path | None = None) -> SetupManifest:
    """Load the bundled manifest (or ``path``)."""
    for candidate in [path] if path else manifest_candidates():
        if candidate and candidate.is_file():
            with open(candidate, encoding="utf-8") as fh:
                return parse_manifest(json.load(fh))
    raise SetupError("comfy_setup.json was not found next to the plug-in; reinstall OpenFill")


def total_download_bytes(manifest: SetupManifest, model_sizes: list[int]) -> int:
    """What the first run downloads in all: ComfyUI plus the starting models."""
    return manifest.download_bytes + sum(model_sizes)


def download_notice(manifest: SetupManifest) -> str:
    """The size the notice shows, as plain words (``about 1.9 GB``)."""
    return f"about {human_size(manifest.download_bytes)}"


# ------------------------------------------------------------------- paths ----


def default_install_dir(env: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\OpenFill\\ComfyUI`` (``~/.openfill/ComfyUI`` elsewhere)."""
    env = os.environ if env is None else env
    if env.get("OPENFILL_INSTALL_DIR"):  # test hook: install somewhere else than the user's profile
        return Path(env["OPENFILL_INSTALL_DIR"])
    local = env.get("LOCALAPPDATA")
    if local:
        return Path(local) / "OpenFill" / "ComfyUI"
    return Path.home() / ".openfill" / "ComfyUI"


def comfy_dir_for(install_dir: Path, manifest: SetupManifest) -> Path:
    """The ComfyUI folder (the one holding ``python_embeded``) inside ``install_dir``."""
    return Path(install_dir) / manifest.folder


# --------------------------------------------------------------- detection ----


@dataclass(frozen=True)
class Candidate:
    """A ComfyUI install found on this PC."""

    path: Path
    source: str  # plain words: "portable folder", "Stability Matrix", ...


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Fixed places people unpack the portable build to (plus Pinokio's root).
FIXED_ROOTS = (Path("C:/AI/ComfyUI"), Path("C:/"), Path("C:/ComfyUI"))
FIXED_PINOKIO = (Path("C:/pinokio"),)


def _candidate_paths(
    env: Mapping[str, str], home: Path, fixed: tuple[Path, ...], pinokio: tuple[Path, ...]
) -> list[tuple[Path, str]]:
    appdata = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
    local = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
    out: list[tuple[Path, str]] = [(default_install_dir(env) / "ComfyUI_windows_portable", "installed by OpenFill")]
    for root in (*fixed, home, home / "ComfyUI"):
        out.append((root / "ComfyUI_windows_portable", "portable folder"))
    for folder in ("Downloads", "Desktop", "Documents"):
        out.append((home / folder / "ComfyUI_windows_portable", "portable folder"))
        out.append((home / folder / "ComfyUI", "portable folder"))
    # A git checkout with a venv in the home folder (the usual place on macOS and Linux).
    out.append((home / "ComfyUI", "ComfyUI folder"))
    out.append((home / "AI" / "ComfyUI", "ComfyUI folder"))
    # ComfyUI Desktop remembers where the user put its files.
    xdg_config = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    xdg_data = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    for desktop in (appdata, home / "Library/Application Support", xdg_config):
        base = _read_json(desktop / "ComfyUI" / "config.json").get("basePath")
        if isinstance(base, str) and base:
            out.append((Path(base), "ComfyUI Desktop"))
    # Stability Matrix: the library may have been moved; its default is %APPDATA%\StabilityMatrix.
    libs = [
        appdata / "StabilityMatrix",
        local / "StabilityMatrix",
        home / "Library/Application Support/StabilityMatrix",
        xdg_data / "StabilityMatrix",
    ]
    for settings in (appdata / "StabilityMatrix" / "library.json", appdata / "StabilityMatrix" / "settings.json"):
        lib = _read_json(settings).get("LibraryPath") or _read_json(settings).get("libraryPath")
        if isinstance(lib, str) and lib:
            libs.insert(0, Path(lib))
    for lib in libs:
        out.append((lib / "Data" / "Packages" / "ComfyUI", "Stability Matrix"))
        out.append((lib / "Packages" / "ComfyUI", "Stability Matrix"))
    # Pinokio keeps ComfyUI in api/comfy.git/app.
    for root in (home / "pinokio", *pinokio):
        out.append((root / "api" / "comfy.git" / "app", "Pinokio"))
    return out


def detect_comfy(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    usable: Callable[[Path], bool] = lambda p: comfy_layout(p) is not None,
    fixed_roots: tuple[Path, ...] = FIXED_ROOTS,
    pinokio_roots: tuple[Path, ...] = FIXED_PINOKIO,
) -> list[Candidate]:
    """ComfyUI installs this plug-in can run, best first, without duplicates.

    ``usable`` decides whether a folder is a ComfyUI it can start (default: it
    has the portable or checkout layout). A portable folder that was unpacked
    into a folder of the same name (``X/ComfyUI_windows_portable/ComfyUI_windows_portable``)
    is found too. Never raises.
    """
    env = os.environ if env is None else env
    home = home or Path.home()
    if env.get("OPENFILL_NO_FIXED_ROOTS"):  # test hook, same name install.ps1 reads
        fixed_roots, pinokio_roots = (), ()
    found: list[Candidate] = []
    seen: set[str] = set()
    for path, source in _candidate_paths(env, home, fixed_roots, pinokio_roots):
        for p in (path, path / "ComfyUI_windows_portable"):
            try:
                key = os.path.normcase(str(p))
                if key in seen:
                    continue
                if p.is_dir() and usable(p):
                    seen.add(key)
                    found.append(Candidate(p, source))
            except OSError:
                continue
    return found


# ----------------------------------------------------------------- preflight ----

# Where to read how to install ComfyUI by hand where Download ComfyUI cannot help.
MANUAL_INSTALL_MAC = "https://github.com/comfyanonymous/ComfyUI#apple-mac-silicon"
MANUAL_INSTALL_LINUX = "https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux"


@dataclass(frozen=True)
class Check:
    """One line of the pre-flight report. ``level`` is ok, warn or block."""

    key: str
    level: str
    message: str


@dataclass(frozen=True)
class Preflight:
    """Result of :func:`preflight`; ``gpu`` is nvidia, other, none or unknown."""

    checks: tuple[Check, ...]
    gpu: str
    gpu_name: str = ""
    older_gpu: bool = False  # an NVIDIA card the main build's PyTorch cannot run: use the build for older cards

    @property
    def blocked(self) -> bool:
        """A check that stops the install outright (not enough disk space)."""
        return any(c.level == "block" for c in self.checks)

    @property
    def needs_cpu_choice(self) -> bool:
        """No NVIDIA card: the download only makes sense as the very slow CPU route."""
        return self.gpu != "nvidia"

    def text(self) -> str:
        """All messages, one per line."""
        return "\n".join(c.message for c in self.checks)


def gpu_kind(names: list[str] | None, nvidia_found: bool) -> str:
    """``nvidia`` / ``other`` (AMD, Intel) / ``none`` (no discrete card named) / ``unknown`` (could not ask)."""
    if nvidia_found or any("nvidia" in n.lower() for n in names or []):
        return "nvidia"
    if names is None:
        return "unknown"
    for n in names:
        low = n.lower()
        if any(v in low for v in ("amd", "radeon", "intel", "arc")) and "microsoft basic" not in low:
            return "other"
    return "none"


def video_controller_names(runner: Callable[..., Any] = subprocess.run) -> list[str] | None:
    """Names of the PC's graphics adapters (Windows, via PowerShell), or None when they cannot be listed."""
    if sys.platform != "win32":
        return None
    try:
        r = runner(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_VideoController).Name",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def preflight(
    manifest: SetupManifest,
    hw: Any,
    adapters: list[str] | None,
    free_bytes: int | None,
    target: Path,
    port: int,
    port_in_use: bool,
    need_bytes: int = NEED_FREE_BYTES,
    platform: str = sys.platform,
) -> Preflight:
    """Plain-words checks before the big download. ``hw`` is a :class:`hardware.Hardware`."""
    if platform != "win32":
        # The pinned download is the Windows portable build (python_embeded\python.exe).
        system = "macOS" if platform == "darwin" else "Linux"
        manual = MANUAL_INSTALL_MAC if platform == "darwin" else MANUAL_INSTALL_LINUX
        return Preflight(
            (
                Check(
                    "platform",
                    "block",
                    f"Download ComfyUI only sets up the Windows build, which does not run on {system}. Install "
                    f"ComfyUI yourself ({manual}) in a folder with a venv, then choose 'I already have ComfyUI'.",
                ),
            ),
            "unknown",
        )
    checks: list[Check] = []
    kind = gpu_kind(
        adapters, bool(getattr(hw, "gpu_name", None) and getattr(hw, "gpu_source", "") in ("nvidia-smi", "nvml"))
    )
    name = getattr(hw, "gpu_name", None) or ""
    vram = getattr(hw, "vram_gb", None)
    if kind == "nvidia":
        if vram is not None and vram < MIN_VRAM_GB:
            checks.append(
                Check(
                    "gpu",
                    "warn",
                    f"{name} has {vram:.0f} GB of graphics memory. That is below the {MIN_VRAM_GB:.0f} GB the "
                    "smallest model needs, so fills will be slow or fail.",
                )
            )
        else:
            mem = f" ({vram:.0f} GB graphics memory)" if vram else ""
            checks.append(Check("gpu", "ok", f"Graphics card: {name or 'NVIDIA'}{mem}."))
        if manifest.needs_older_build(getattr(hw, "compute_capability", None)):
            checks.append(
                Check(
                    "gpu_build",
                    "ok",
                    f"{name or 'This card'} is older than the RTX 20 series, so OpenFill downloads ComfyUI's "
                    "build for older NVIDIA cards (CUDA 12.6). The standard build would not run on it.",
                )
            )
    elif kind == "other":
        checks.append(
            Check(
                "gpu",
                "warn",
                "This PC has an AMD or Intel graphics card. The download below is the NVIDIA build and would "
                "run on the processor only, which is very slow. For an AMD card follow the AMD instructions "
                f"({manifest.amd_instructions}), then choose 'I already have ComfyUI'.",
            )
        )
    elif kind == "none":
        checks.append(
            Check(
                "gpu",
                "warn",
                "No dedicated graphics card was found. ComfyUI would run on the processor, which is very slow "
                "(minutes per fill instead of seconds).",
            )
        )
    else:
        checks.append(
            Check(
                "gpu",
                "warn",
                "Could not check the graphics card. The download is the NVIDIA build; without an NVIDIA card "
                "it would run on the processor only, which is very slow.",
            )
        )
    if free_bytes is None:
        checks.append(Check("disk", "warn", f"Could not check the free space on the drive of {target}."))
    elif free_bytes < need_bytes:
        checks.append(
            Check(
                "disk",
                "block",
                f"Not enough free disk space on the drive of {target}: about {human_size(need_bytes)} are needed "
                f"(ComfyUI, its unpacked copy and the first models), {human_size(free_bytes)} are free. Free up "
                "space or choose another folder.",
            )
        )
    else:
        checks.append(
            Check("disk", "ok", f"Free disk space: {human_size(free_bytes)} (needs {human_size(need_bytes)}).")
        )
    if port_in_use:
        checks.append(
            Check(
                "port",
                "warn",
                f"Port {port} is already in use. If a ComfyUI is already running, choose 'I already have ComfyUI'; "
                "otherwise close the program using it before generating.",
            )
        )
    else:
        checks.append(Check("port", "ok", f"Port {port} is free."))
    older = kind == "nvidia" and manifest.needs_older_build(getattr(hw, "compute_capability", None))
    return Preflight(tuple(checks), kind, name, older)


# -------------------------------------------------------------------- setup ----


def system_tar() -> str | None:
    """Windows' own ``tar.exe`` (bsdtar, reads .7z). Not the PATH one: Git's GNU tar cannot read .7z."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    p = Path(root) / "System32" / "tar.exe"
    return str(p) if p.is_file() else None


def extract_archive(
    archive: Path,
    dest: Path,
    tar_exe: str | None = None,
    cancel: Callable[[], bool] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    tick: Callable[[], None] | None = None,
) -> None:
    """Unpack ``archive`` into ``dest`` with bsdtar; cancellable. ``tick`` is called about once a second.
    Raises :class:`SetupError`."""
    tar_exe = tar_exe or system_tar()
    if not tar_exe:
        raise SetupError(
            "Windows' built-in tar.exe was not found (it ships with Windows 10 and 11). Unpack "
            f"{archive.name} yourself with 7-Zip, then choose 'I already have ComfyUI'."
        )
    dest.mkdir(parents=True, exist_ok=True)
    kw: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = popen([tar_exe, "-xf", str(archive), "-C", str(dest)], **kw)
    while True:
        try:
            _out, err = proc.communicate(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            if tick:
                tick()
            if cancel and cancel():
                proc.kill()
                proc.communicate()
                raise downloads.DownloadError("cancelled", "setup cancelled") from None
    if proc.returncode != 0:
        raise SetupError(f"Unpacking {archive.name} failed: {(err or '').strip()[-400:]}")


ProgressSetupFn = Callable[[str, int, int], None]  # state, done, total


def folder_bytes(path: Path) -> int:
    """Total size of the files below ``path`` (0 when it does not exist); used to show unpacking progress."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def overall_fraction(state: str, done: int, total: int) -> float:
    """0..1 over the whole setup for a task status (``state``, bytes ``done`` of ``total``)."""
    lo, hi = STATE_SPAN.get(state, (0.0, 0.0))
    inner = min(1.0, done / total) if total and state in ("downloading", "unpacking") else 0.0
    return lo + (hi - lo) * inner


def run_setup(
    manifest: SetupManifest,
    install_dir: Path,
    progress: ProgressSetupFn | None = None,
    cancel: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    opener: Callable[..., Any] = urlopen,
    tar_exe: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Path:
    """Download, verify and unpack the pinned ComfyUI, add the custom nodes and
    packages, and return the ComfyUI folder. Safe to run again: finished parts are skipped.

    Raises :class:`SetupError` or ``downloads.DownloadError`` (code ``cancelled`` included).
    """
    log = log or (lambda _m: None)
    progress = progress or (lambda _s, _d, _t: None)
    install_dir = Path(install_dir)
    comfy = comfy_dir_for(install_dir, manifest)
    install_dir.mkdir(parents=True, exist_ok=True)
    if (install_dir / UNPACK_MARKER).exists():
        shutil.rmtree(comfy, ignore_errors=True)  # an unpack that was cut short: start the folder over
        (install_dir / UNPACK_MARKER).unlink()
    if comfy_layout(comfy) is None:
        archive = install_dir / ".download" / manifest.archive_name
        progress("downloading", 0, manifest.archive_size)
        downloads.download_file(
            manifest.archive_url,
            archive,
            expected_size=manifest.archive_size,
            expected_sha256=manifest.archive_sha256,
            progress=lambda d, t, _s: progress("downloading", d, t or manifest.archive_size),
            cancel=cancel,
            opener=opener,
            min_free_bytes=max(manifest.unpacked_bytes, 256 * 1024 * 1024),
        )
        progress("verifying", 1, 1)  # download_file checked size and SHA-256 before the rename
        log(f"{archive.name} verified")
        # Unpack straight into place: renaming a freshly unpacked folder of ~30,000 files fails on
        # Windows while the virus scanner still holds files in it. A marker file tells a later run
        # that an unpack was cut short, so it starts that folder over.
        marker = install_dir / UNPACK_MARKER
        if comfy.exists():
            shutil.rmtree(comfy, ignore_errors=True)  # a half-finished earlier attempt
        marker.write_text("unpacking\n", encoding="utf-8")
        progress("unpacking", 0, 0)
        log("unpacking with tar.exe")
        ticks = {"n": 0}

        def unpack_tick() -> None:
            ticks["n"] += 1
            if ticks["n"] % 3 == 0 and manifest.unpacked_bytes:  # walking ~60,000 files is not free
                progress("unpacking", folder_bytes(comfy), manifest.unpacked_bytes)

        extract_archive(archive, install_dir, tar_exe, cancel, popen, unpack_tick)
        if comfy_layout(comfy) is None:
            raise SetupError(f"The archive did not hold a working {manifest.folder} folder.")
        marker.unlink()
        try:
            archive.unlink()
        except OSError:
            pass
        shutil.rmtree(archive.parent, ignore_errors=True)
    layout = comfy_layout(comfy)
    if layout is None:
        raise SetupError(f"ComfyUI was unpacked to {comfy} but python_embeded or ComfyUI/main.py is missing.")
    nodes_dir = layout["main"].parent / "custom_nodes"
    for n in manifest.nodes:
        progress("nodes", 0, 0)
        downloads.install_custom_node(
            n.repo, n.folder, n.commit, nodes_dir, layout["python"], log, runner, cancel, opener
        )
    if manifest.pip:
        progress("packages", 0, 0)
        log("installing " + ", ".join(manifest.pip))
        kw: dict[str, Any] = {"capture_output": True, "text": True}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = runner(
            [str(layout["python"]), "-s", "-m", "pip", "install", "--no-warn-script-location", *manifest.pip], **kw
        )
        if r.returncode != 0:
            raise SetupError(f"pip install failed: {(r.stderr or r.stdout or '').strip()[-400:]}")
    progress("packages", 1, 1)
    return comfy
