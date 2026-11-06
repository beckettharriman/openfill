# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Build the PhotoCraft companion's release zip.

    python tools/make_photocraft_zip.py [--out dist] [--title]

``openfill-photocraft-<version>.zip`` holds one folder with the companion package,
the shared OpenFill core, the model catalog and the launchers. ``--title`` prints the
release title instead: "OpenFill for PhotoCraft <version> (works with PhotoCraft A to B)".
"""

from __future__ import annotations

import argparse
import re
import stat
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {"__pycache__", "e2e", "tests"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
# GIMP-only modules of the core are left out: the companion never imports them.
GIMP_ONLY = {
    "gimp_io.py",
    "dialogs.py",
    "runner.py",
    "progress_window.py",
    "models_ui.py",
    "ui_setup.py",
    "ui_picker.py",
}
EXECUTABLE = {"openfill-photocraft.sh", "install.sh"}


def version(root: Path = ROOT) -> str:
    text = (root / "photocraft" / "openfill_photocraft" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def photocraft_range(root: Path = ROOT) -> tuple[str, str]:
    """The oldest and newest PhotoCraft versions this release was tested on."""
    text = (root / "photocraft" / "openfill_photocraft" / "__init__.py").read_text(encoding="utf-8")
    oldest, newest = re.search(r'PHOTOCRAFT_TESTED\s*=\s*\("([^"]+)",\s*"([^"]+)"\)', text).groups()
    return oldest, newest


def release_title(root: Path = ROOT) -> str:
    """The GitHub release title: OpenFill's version and the PhotoCraft range, never a bare 0.2.x."""
    oldest, newest = photocraft_range(root)
    return f"OpenFill for PhotoCraft {version(root)} (works with PhotoCraft {oldest} to {newest})"


def files(root: Path = ROOT) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []

    def add_tree(src: Path, dest: str, skip: set[str] = frozenset()) -> None:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if p.is_dir() or SKIP_PARTS & set(rel.parts) or p.suffix in SKIP_SUFFIXES or p.name in skip:
                continue
            out.append((p, f"{dest}/{rel.as_posix()}"))

    add_tree(root / "photocraft" / "openfill_photocraft", "openfill_photocraft")
    add_tree(root / "gimp-openfill" / "openfill", "openfill", GIMP_ONLY)
    for name in ("catalog.json", "comfy_setup.json"):
        out.append((root / "models" / name, f"models/{name}"))
    for src, dest in (
        ("photocraft/openfill-photocraft.sh", "openfill-photocraft.sh"),
        ("photocraft/install.sh", "install.sh"),
        ("photocraft/Install OpenFill for PhotoCraft.cmd", "Install OpenFill for PhotoCraft.cmd"),
        ("photocraft/README.md", "README.md"),
        ("photocraft/CHANGELOG.md", "CHANGELOG.md"),
    ):
        if (root / src).is_file():
            out.append((root / src, dest))
    for name in ("LICENSE", "NOTICE"):
        if (root / name).is_file():
            out.append((root / name, name))
    return out


def build(out_dir: Path, root: Path = ROOT) -> Path:
    ver = version(root)
    folder = f"openfill-photocraft-{ver}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{folder}.zip"
    stamp = time.localtime()[:6]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, rel in files(root):
            info = zipfile.ZipInfo(f"{folder}/{rel}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if Path(rel).name in EXECUTABLE else 0o644)) << 16
            zf.writestr(info, src.read_bytes())
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--title", action="store_true", help="print the release title and build nothing")
    args = parser.parse_args()
    print(release_title() if args.title else build(Path(args.out)))
