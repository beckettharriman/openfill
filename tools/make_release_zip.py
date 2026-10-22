# SPDX-License-Identifier: Apache-2.0
"""Build the OpenFill release zip: what a user unzips and drops into GIMP.

    python tools/make_release_zip.py [--out dist] [--version 2.0.0-beta.3]

``openfill-<version>.zip`` holds ``gimp-openfill/`` at its top level. That folder
is complete on its own (plug-in code, ``models/catalog.json``,
``models/comfy_setup.json``, README, LICENSE): copying it into GIMP's plug-ins
folder is the whole install. Next to it sit the optional installer scripts and
documents. Tests, screenshots and evidence are left out.
"""

from __future__ import annotations

import argparse
import re
import stat
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "Install OpenFill.cmd",
    "install.ps1",
    "install.sh",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "SECURITY.md",
    "models/catalog.json",
    "models/comfy_setup.json",
    "docs/security.md",
    "docs/models.md",
)
PLUGIN_DIR = "gimp-openfill"
# Copies placed inside the plug-in folder so it works wherever it is dropped.
BUNDLED = ("models/catalog.json", "models/comfy_setup.json", "README.md", "LICENSE")
# Unzipped on Linux or macOS these must stay executable: GIMP skips a plug-in whose entry point is not.
EXECUTABLE = {f"{PLUGIN_DIR}/{PLUGIN_DIR}.py", "install.sh"}
SKIP_PARTS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def read_version(root: Path = ROOT) -> str:
    """``__version__`` from the package."""
    text = (root / PLUGIN_DIR / "openfill" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("no __version__ in openfill/__init__.py")
    return match.group(1)


def release_files(root: Path = ROOT) -> list[tuple[Path, str]]:
    """``(source, path inside the folder)`` for every file that ships."""
    out: list[tuple[Path, str]] = []
    for rel in FILES:
        src = root / rel
        if not src.is_file():
            raise SystemExit(f"missing release file: {rel}")
        out.append((src, rel))
    for src in sorted((root / PLUGIN_DIR).rglob("*")):
        if not src.is_file() or SKIP_PARTS & set(src.parts) or src.suffix in SKIP_SUFFIXES:
            continue
        out.append((src, src.relative_to(root).as_posix()))
    for rel in BUNDLED:
        out.append((root / rel, f"{PLUGIN_DIR}/{rel}"))
    return out


def build(out_dir: Path, version: str | None = None, root: Path = ROOT) -> Path:
    """Write ``openfill-<version>.zip`` into ``out_dir`` and return its path."""
    version = version or read_version(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"openfill-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, rel in release_files(root):
            data = src.read_bytes()
            if rel.endswith((".cmd", ".ps1")):  # Windows scripts want CRLF whatever the checkout used
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            info = zipfile.ZipInfo(rel, date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if rel in EXECUTABLE else 0o644)) << 16
            zf.writestr(info, data)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "dist"), help="folder for the zip (default: dist)")
    parser.add_argument("--version", default=None)
    args = parser.parse_args(argv)
    print(build(Path(args.out), args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
