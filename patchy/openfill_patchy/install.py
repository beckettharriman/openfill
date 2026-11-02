# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Add OpenFill to Patchy's Plugins > Companions menu for this user, or take it out again.

    python -m openfill_patchy.install [--python PATH] [--uninstall]

Writes ``openfill.json`` into Patchy's companions folder (see Patchy's docs/companions.md):
``<per-user app data>/companions``, which is ``%APPDATA%\\RTsoft\\Patchy\\companions`` on
Windows, ``~/Library/Application Support/RTsoft/Patchy/companions`` on macOS and
``~/.local/share/RTsoft/Patchy/companions`` on Linux, plus
``~/.var/app/com.rtsoft.patchy/data/RTsoft/Patchy/companions`` when Patchy's Flatpak is
installed (a Flatpak Patchy lists the entry but cannot start it: start OpenFill yourself and
it attaches). The entry runs this copy of OpenFill
with the given Python (by default the one running the installer; ``pythonw.exe`` on Windows
when it is there, so no console window opens). Patchy reads the folder each time the menu
opens, so no restart is needed. Settings, models and ComfyUI stay where they are on
``--uninstall``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .client import COMPANION_ID, COMPANION_PROTOCOL_MAX, COMPANION_PROTOCOL_MIN, FLATPAK_ID, flatpak_patchy_installed

MANIFEST = "openfill.json"


def companions_dir() -> Path:
    """Patchy's per-user companions folder (Qt's AppDataLocation for RTsoft/Patchy)."""
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / "RTsoft" / "Patchy" / "companions"


def flatpak_companions_dir() -> Path:
    """The companions folder of Patchy's Flatpak (its sandbox's own app data)."""
    return Path.home() / ".var" / "app" / FLATPAK_ID / "data" / "RTsoft" / "Patchy" / "companions"


def target_dirs() -> list[Path]:
    """Every companions folder a Patchy on this computer reads."""
    folders = [companions_dir()]
    if flatpak_patchy_installed():
        folders.append(flatpak_companions_dir())
    return folders


def has_pillow(python: str) -> bool:
    """``python`` can import Pillow (the Python OpenFill runs with, not necessarily this one)."""
    if Path(python).resolve() == Path(sys.executable).resolve():
        try:
            import PIL  # noqa: F401
        except ImportError:
            return False
        return True
    try:
        return subprocess.run([python, "-c", "import PIL"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def no_pillow_advice(python: str) -> str:
    """What to do when ``python`` has no Pillow. Homebrew's Python and the system Python of
    recent Linux releases refuse ``pip install`` (PEP 668), so those get another way."""
    probe = (
        "import os, sysconfig; print(os.path.exists(os.path.join(sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED')))"
    )
    try:
        managed = subprocess.run([python, "-c", probe], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        managed = ""
    if managed != "True":
        return f"{python} has no Pillow; install it first: {python} -m pip install Pillow"
    comfy_python = r"ComfyUI\venv\Scripts\python.exe" if sys.platform == "win32" else "~/ComfyUI/venv/bin/python"
    return (
        f"{python} has no Pillow, and its packages are managed by the system, so pip cannot add it. "
        f"Use ComfyUI's Python, which has Pillow: python3 -m openfill_patchy.install --python {comfy_python} "
        "(the venv in your ComfyUI folder), or a venv of your own with Pillow."
    )


def package_root() -> Path:
    """The folder that holds ``openfill_patchy`` (``python -m`` runs from there)."""
    return Path(__file__).resolve().parent.parent


def windowless(python: str) -> str:
    """``pythonw.exe`` beside ``python.exe`` on Windows, when there is one."""
    path = Path(python)
    if sys.platform == "win32" and path.name.lower() == "python.exe":
        quiet = path.with_name("pythonw.exe")
        if quiet.is_file():
            return str(quiet)
    return python


def manifest(python: str) -> dict:
    """The Companions entry (its keys are Patchy's file format: never renamed)."""
    return {
        "id": COMPANION_ID,
        "name": "OpenFill",
        "menuLabel": "OpenFill",
        "command": windowless(python),
        "args": ["-m", "openfill_patchy"],
        "workingDirectory": str(package_root()),
        # Patchy greys the entry out when it serves none of these protocol versions.
        "minProtocol": COMPANION_PROTOCOL_MIN,
        "maxProtocol": COMPANION_PROTOCOL_MAX,
    }


def install(python: str, folder: Path | None = None) -> Path:
    """Write the manifest; its path."""
    folder = folder or companions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / MANIFEST
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest(python), indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)  # a reader never sees half a file
    return path


def uninstall(folder: Path | None = None) -> bool:
    """Remove the manifest if it is OpenFill's; True when it was there."""
    path = (folder or companions_dir()) / MANIFEST
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("id") != COMPANION_ID:
            return False  # another companion's file under this name: leave it
        path.unlink()
        return True
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfill-patchy-install", description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", default=sys.executable, help="the Python that runs OpenFill (needs Pillow)")
    parser.add_argument("--uninstall", action="store_true", help="take OpenFill out of Plugins > Companions")
    args = parser.parse_args(argv)
    if args.uninstall:
        removed = [folder for folder in target_dirs() if uninstall(folder)]
        print("Removed." if removed else "OpenFill was not in Patchy's Companions menu.")
        return 0
    if not has_pillow(args.python):
        print(no_pillow_advice(args.python))
        return 2
    for folder in target_dirs():
        path = install(args.python, folder)
        print(f"Added OpenFill to Plugins > Companions in Patchy ({path}).")
        if folder != companions_dir():
            print("Patchy's Flatpak cannot start it: start OpenFill yourself (python -m openfill_patchy).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
