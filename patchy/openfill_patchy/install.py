# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Add OpenFill to Patchy's Plugins > Companions menu for this user, or take it out again.

    python -m openfill_patchy.install [--python PATH] [--uninstall]

Writes ``openfill.json`` into Patchy's companions folder (see Patchy's docs/companions.md):
``<per-user app data>/companions``, which is ``%APPDATA%\\RTsoft\\Patchy\\companions`` on
Windows, ``~/Library/Application Support/RTsoft/Patchy/companions`` on macOS and
``~/.local/share/RTsoft/Patchy/companions`` on Linux. The entry runs this copy of OpenFill
with the given Python (by default the one running the installer; ``pythonw.exe`` on Windows
when it is there, so no console window opens). Patchy reads the folder each time the menu
opens, so no restart is needed. Settings, models and ComfyUI stay where they are on
``--uninstall``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .client import COMPANION_ID

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
        print("Removed." if uninstall() else "OpenFill was not in Patchy's Companions menu.")
        return 0
    try:
        import PIL  # noqa: F401
    except ImportError:
        print(f"{args.python} has no Pillow; install it first: {args.python} -m pip install Pillow")
        return 2
    path = install(args.python)
    print(f"Added OpenFill to Plugins > Companions in Patchy ({path}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
