# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Install OpenFill for PhotoCraft for this user (Linux), so PhotoCraft opens with OpenFill
from its usual icon and from images opened in the file manager.

    python -m openfill_photocraft.install [--get-photocraft] [--separate-icon] [--uninstall]

- copies OpenFill to ~/.local/share/openfill-photocraft/app and writes the command
  ~/.local/bin/openfill-photocraft;
- installs PhotoCraft for this user if it is missing and you agree (the official Linux
  build from GitHub, its SHA-256 checked), no administrator rights needed;
- gives PhotoCraft's menu entry to OpenFill: a copy of PhotoCraft's own entry in
  ~/.local/share/applications whose command is OpenFill's launcher (it starts PhotoCraft).
  ``--separate-icon`` adds "PhotoCraft with OpenFill" instead and leaves PhotoCraft's entry
  alone. ``--uninstall`` takes all of it back out.

Nothing outside your home folder is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DESKTOP_ID = "ai.storyteller.photocraft.desktop"
SEPARATE_ID = "openfill-photocraft.desktop"
MARKER = "X-OpenFill-Launcher=true"
RELEASES = "https://api.github.com/repos/storytold/photocraft/releases/latest"


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def share_dir() -> Path:
    return data_home() / "openfill-photocraft"


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def source_root() -> Path:
    """The unpacked release folder (or the repo's photocraft/ folder with the core beside it)."""
    return Path(__file__).resolve().parent.parent


# copying OpenFill ---------------------------------------------------------------------------


def copy_app(dest: Path) -> None:
    """Copy the companion, the OpenFill core and the model lists to ``dest`` (replacing it)."""
    src = source_root()
    core = src / "openfill" if (src / "openfill").is_dir() else src.parent / "gimp-openfill" / "openfill"
    models = src / "models" if (src / "models").is_dir() else src.parent / "models"
    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "e2e")
    shutil.copytree(src / "openfill_photocraft", staging / "openfill_photocraft", ignore=ignore)
    shutil.copytree(core, staging / "openfill", ignore=ignore)
    staging.joinpath("models").mkdir()
    for name in ("catalog.json", "comfy_setup.json"):
        shutil.copy2(models / name, staging / "models" / name)
    for name in ("README.md", "LICENSE", "openfill-photocraft.sh"):
        for folder in (src, src.parent):
            if (folder / name).is_file():
                shutil.copy2(folder / name, staging / name)
                break
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    staging.rename(dest)
    shutil.rmtree(old, ignore_errors=True)


def write_command(app: Path, python: str) -> Path:
    """~/.local/bin/openfill-photocraft: starts the installed copy with this Python."""
    path = bin_dir() / "openfill-photocraft"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "# OpenFill for PhotoCraft (written by its installer; remove with: openfill-photocraft-uninstall)\n"
        f'export PYTHONPATH="{app}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec "{python}" -m openfill_photocraft "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    uninstall = bin_dir() / "openfill-photocraft-uninstall"
    uninstall.write_text(
        "#!/bin/sh\n"
        f'export PYTHONPATH="{app}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec "{python}" -m openfill_photocraft.install --uninstall "$@"\n',
        encoding="utf-8",
    )
    uninstall.chmod(0o755)
    return path


# PhotoCraft ---------------------------------------------------------------------------------


def latest_release() -> dict:
    req = urllib.request.Request(RELEASES, headers={"Accept": "application/vnd.github+json", "User-Agent": "OpenFill"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def linux_asset(release: dict) -> tuple[str, str, str]:
    """(tarball name, its URL, SHA256SUMS URL) for this machine."""
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    name = f"photocraft-{release['tag_name'].lstrip('v')}-linux-{arch}.tar.gz"
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets") or []}
    sums = next((url for n, url in assets.items() if n.startswith("SHA256SUMS")), "")
    if name not in assets or not sums:
        raise RuntimeError(f"PhotoCraft {release['tag_name']} has no Linux build for {arch}.")
    return name, assets[name], sums


def download(url: str, dest: Path, label: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "OpenFill"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total and sys.stdout.isatty():
                print(f"\r  {label}: {done * 100 // total}%", end="", flush=True)
    if sys.stdout.isatty():
        print()


def install_photocraft() -> Path:
    """Download and unpack the latest PhotoCraft for Linux into the share folder; its binary."""
    release = latest_release()
    name, url, sums_url = linux_asset(release)
    target = share_dir() / name.removesuffix(".tar.gz")
    if (target / "bin" / "photocraft").is_file():
        return target / "bin" / "photocraft"
    share_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=share_dir()) as tmp:
        tarball, sums = Path(tmp) / name, Path(tmp) / "SHA256SUMS"
        print(f"Downloading PhotoCraft {release['tag_name']} ...")
        download(sums_url, sums, "checksums")
        download(url, tarball, name)
        want = next((line.split()[0] for line in sums.read_text().splitlines() if line.strip().endswith(name)), "")
        got = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if not want or want.lower() != got:
            raise RuntimeError(f"The PhotoCraft download did not match its published SHA-256 ({got}).")
        unpack = Path(tmp) / "unpack"
        with tarfile.open(tarball) as tf:
            tf.extractall(unpack, filter="data")
        top = next(unpack.iterdir())
        top.rename(target)
    return target / "bin" / "photocraft"


# the menu entry -----------------------------------------------------------------------------


def desktop_sources(photocraft: str | None) -> list[Path]:
    """Where PhotoCraft's own menu entry may be, best first."""
    dirs = [
        Path(d) / "applications" for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    ]
    dirs += [
        data_home() / "flatpak" / "exports" / "share" / "applications",
        Path("/var/lib/flatpak/exports/share/applications"),
    ]
    if photocraft:
        dirs.insert(0, Path(photocraft).resolve().parent.parent / "share" / "applications")
    return [d / DESKTOP_ID for d in dirs if (d / DESKTOP_ID).is_file()]


def entry_for(command: Path, original: str | None, separate: bool, icon: str) -> str:
    """A menu entry that starts ``command`` (OpenFill's launcher) with the images it is given."""
    if original is None:
        original = (
            "[Desktop Entry]\nType=Application\nName=PhotoCraft\nGenericName=Image Editor\n"
            f"Icon={icon}\nTerminal=false\nCategories=Graphics;RasterGraphics;2DGraphics;\n"
            "MimeType=image/png;image/jpeg;image/webp;image/tiff;image/bmp;image/gif;"
            "image/vnd.adobe.photoshop;image/x-photoshop;\nExec=photocraft %F\n"
        )
    out = []
    section = ""
    for line in original.splitlines():
        if line.startswith("["):
            section = line
            if section != "[Desktop Entry]":
                break  # the entry's actions start PhotoCraft without OpenFill: leave them out
        key = line.split("=", 1)[0]
        if key in ("TryExec", "DBusActivatable", "Actions", "X-Flatpak", "X-Flatpak-RenamedFrom"):
            continue
        if key == "Exec":
            line = f'Exec="{command}" %F'
        if separate and key == "Name":
            line = "Name=PhotoCraft with OpenFill"
        if separate and key == "MimeType":
            continue  # the separate icon does not take over opening images
        out.append(line)
    out.append(MARKER)
    return "\n".join(out) + "\n"


def copy_icons(photocraft: str | None) -> str:
    """The icon name the entry can use: PhotoCraft's, copied from a tarball when needed."""
    if not photocraft:
        return "image-x-generic"
    share = Path(photocraft).resolve().parent.parent / "share" / "icons"
    if share.is_dir() and not Path("/usr/share/icons/hicolor/256x256/apps/ai.storyteller.photocraft.png").is_file():
        for png in share.rglob("ai.storyteller.photocraft.*"):
            dest = data_home() / "icons" / png.relative_to(share)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(png, dest)
    return "ai.storyteller.photocraft"


def refresh_menus(folder: Path) -> None:
    for tool in (["update-desktop-database", str(folder)], ["kbuildsycoca6", "--noincremental"], ["kbuildsycoca5"]):
        if shutil.which(tool[0]):
            subprocess.run(tool, capture_output=True, timeout=60)


def install_entry(command: Path, photocraft: str | None, separate: bool) -> Path:
    folder = data_home() / "applications"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (SEPARATE_ID if separate else DESKTOP_ID)
    if target.is_file() and MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        backup = target.with_name(target.name + ".openfill-backup")
        if not backup.exists():
            target.rename(backup)  # a PhotoCraft entry of the user's own: put back on uninstall
    sources = [p for p in desktop_sources(photocraft) if p != target]
    original = sources[0].read_text(encoding="utf-8", errors="replace") if sources else None
    icon = copy_icons(photocraft)
    target.write_text(entry_for(command, original, separate, icon), encoding="utf-8")
    refresh_menus(folder)
    return target


# install / uninstall -------------------------------------------------------------------------


def ask(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def install(get_photocraft: bool, separate: bool, python: str) -> int:
    if sys.platform != "linux":
        print("This installer is for Linux. On Windows run 'Install OpenFill for PhotoCraft.cmd'.")
        return 2
    sys.path.insert(0, str(source_root()))
    from openfill_photocraft.__main__ import find_photocraft

    found = find_photocraft(None)
    photocraft = found[0] if found and found[0] != "flatpak" else None
    if not found:
        print("PhotoCraft is not installed.")
        if get_photocraft or ask("Download PhotoCraft (about 85 MB) and install it for you, no admin rights needed?"):
            photocraft = str(install_photocraft())
            print(f"PhotoCraft installed: {photocraft}")
        else:
            print("Install PhotoCraft (https://github.com/storytold/photocraft/releases), then run this again.")
            return 1
    app = share_dir() / "app"
    copy_app(app)
    command = write_command(app, python)
    entry = install_entry(command, photocraft, separate)
    print(f"OpenFill installed in {app}")
    print(f"Menu entry: {entry}")
    if separate:
        print("Open 'PhotoCraft with OpenFill' from your app menu.")
    else:
        print("Open PhotoCraft as usual (app menu, or an image from the file manager): OpenFill comes with it.")
    print("To remove OpenFill: openfill-photocraft-uninstall")
    return 0


def uninstall() -> int:
    folder = data_home() / "applications"
    for name in (DESKTOP_ID, SEPARATE_ID):
        entry = folder / name
        if entry.is_file() and MARKER in entry.read_text(encoding="utf-8", errors="replace"):
            entry.unlink()
            print(f"removed {entry}")
        backup = entry.with_name(entry.name + ".openfill-backup")
        if backup.is_file() and not entry.exists():
            backup.rename(entry)
            print(f"put back {entry}")
    for name in ("openfill-photocraft", "openfill-photocraft-uninstall"):
        path = bin_dir() / name
        if path.is_file() and "OpenFill" in path.read_text(encoding="utf-8", errors="replace"):
            path.unlink()
    shutil.rmtree(share_dir() / "app", ignore_errors=True)
    downloaded = sorted(share_dir().glob("photocraft-*"))
    for folder_ in downloaded:
        shutil.rmtree(folder_, ignore_errors=True)
        print(f"removed {folder_} (the PhotoCraft OpenFill installed)")
    try:
        share_dir().rmdir()
    except OSError:
        pass
    refresh_menus(folder)
    print("OpenFill for PhotoCraft is removed. Your settings stay in ~/.config/openfill-photocraft;")
    print("models stay in your ComfyUI folder.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install", description=__doc__.split("\n\n")[0])
    parser.add_argument("--get-photocraft", action="store_true", help="download PhotoCraft without asking")
    parser.add_argument("--separate-icon", action="store_true", help="add a separate menu entry instead")
    parser.add_argument("--uninstall", action="store_true", help="remove OpenFill for PhotoCraft")
    args = parser.parse_args(argv)
    if args.uninstall:
        return uninstall()
    return install(args.get_photocraft, args.separate_icon, sys.executable)


if __name__ == "__main__":
    raise SystemExit(main())
