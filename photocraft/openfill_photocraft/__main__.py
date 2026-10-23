# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Start PhotoCraft with OpenFill: launch PhotoCraft with its control channel (or attach to
one this launcher started earlier), add OpenFill to its Filter > Plug-ins menu and show the
OpenFill bar. Run again while it is running, it opens the given images in that PhotoCraft
and brings the bar back.

    python -m openfill_photocraft [IMAGE ...] [--photocraft PATH] [--port 7878] [--page] [--no-bar]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The shared OpenFill core: next to this package in a release, in gimp-openfill/ in the repo.
for candidate in (HERE.parent, HERE.parent.parent / "gimp-openfill"):
    if (candidate / "openfill" / "app.py").is_file() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openfill_photocraft.bridge import PhotoCraftBridge, default_profile  # noqa: E402
from openfill_photocraft.control import ControlClient, ControlError  # noqa: E402

FLATPAK_ID = "ai.storyteller.photocraft"
COMPANION_FILE = "companion.json"


def find_photocraft(explicit: str | None) -> list[str] | None:
    """The command that starts PhotoCraft, or None."""
    if explicit:
        return [explicit]
    env = os.environ.get("PHOTOCRAFT_BIN")
    if env:
        return [env]
    found = shutil.which("photocraft")
    if found:
        return [found]
    home = Path.home()
    candidates = [
        Path("/usr/bin/photocraft"),
        Path("/opt/photocraft/bin/photocraft"),
        home / ".local" / "bin" / "photocraft",
    ]
    folders = (
        home / ".local" / "share" / "openfill-photocraft",
        home / "Applications",
        home / "Downloads",
        home / ".local" / "bin",
        home / "Desktop",
    )
    for folder in folders:
        if folder.is_dir():
            candidates += sorted(folder.glob("photocraft-*/bin/photocraft"), reverse=True)
            candidates += sorted(folder.glob("photocraft-*.AppImage"), reverse=True)
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        installed = _registry_install_dir()
        if installed:
            candidates.append(Path(installed) / "photocraft.exe")
        candidates += [
            Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files") / "PhotoCraft" / "photocraft.exe",
            local / "Programs" / "PhotoCraft" / "photocraft.exe",
        ]
        for folder in (home / "Downloads", home / "Desktop", home / "Documents", home, Path("C:/"), Path("C:/AI")):
            if folder.is_dir():  # the portable zip, unpacked
                candidates += sorted(folder.glob("photocraft*/photocraft.exe"), reverse=True)
    if sys.platform == "darwin":
        candidates += [Path("/Applications/PhotoCraft.app/Contents/MacOS/photocraft")]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)]
    if shutil.which("flatpak"):
        r = subprocess.run(["flatpak", "info", FLATPAK_ID], capture_output=True, text=True)
        if r.returncode == 0:
            return ["flatpak", "run", FLATPAK_ID]
    return None


def _registry_install_dir() -> str | None:
    """Where PhotoCraft's Windows installer put it (HKLM\\Software\\PhotoCraft InstallDir)."""
    try:
        import winreg

        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, r"Software\PhotoCraft", 0, winreg.KEY_READ | view
                ) as key:
                    return str(winreg.QueryValueEx(key, "InstallDir")[0])
            except OSError:
                continue
    except ImportError:
        pass
    return None


def app_window(url: str) -> bool:
    """Open ``url`` as a small app window (Edge or Chrome without tabs or address bar), for
    when Python has no Tk for the OpenFill bar. False when no such browser is found."""
    names = ["msedge", "chrome", "chromium", "chromium-browser", "google-chrome", "brave-browser"]
    paths = [shutil.which(n) for n in names]
    if sys.platform == "win32":
        for base in (
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                paths += [
                    str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                    str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                ]
    for path in paths:
        if path and Path(path).is_file():
            subprocess.Popen(
                [path, f"--app={url}", "--window-size=480,820"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
            )
            return True
    return False


def photocraft_settings_dir() -> Path | None:
    """PhotoCraft's per-user settings folder (where its single-instance lock lives)."""
    if os.environ.get("PHOTOCRAFT_CONFIG_DIR"):
        return None  # a custom folder never takes the lock
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Photocraft" if appdata else None
    if sys.platform == "darwin":
        return None  # macOS hands files to the running app itself
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "photocraft"


def plain_photocraft_running() -> bool:
    """True when a PhotoCraft opened the usual way (without OpenFill) is running: it holds
    the lock on ``instance.lock`` in its settings folder."""
    folder = photocraft_settings_dir()
    lock = folder / "instance.lock" if folder else None
    if lock is None or not lock.is_file():
        return False
    try:
        with open(lock, "r+b") as f:
            if sys.platform == "win32":
                import msvcrt

                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                return False
            import fcntl

            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch(
    command: list[str], port: int, token: Path, read_root: Path, write_root: Path, files: list[str], log: Path
) -> subprocess.Popen:
    args = list(command)
    if args[:2] == ["flatpak", "run"]:
        # The sandbox must see the images it may open and the exchange folder.
        args.insert(2, "--filesystem=home")
    args += [
        "--control",
        str(port),
        "--control-token-file",
        str(token),
        "--automation-read-root",
        str(read_root),
        "--automation-write-root",
        str(write_root),
        *files,
    ]
    kwargs: dict = {"stdout": open(log, "ab"), "stderr": subprocess.STDOUT, "env": dict(os.environ)}  # noqa: SIM115
    if Path(args[0]).is_file():
        kwargs["cwd"] = str(Path(args[0]).resolve().parent)  # not OpenFill's folder: an update must replace it
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def read_root_for(exchange: Path) -> Path:
    """What PhotoCraft may open for OpenFill: the home folder (so images double-clicked while
    it runs can be opened in it), as long as the exchange folder is inside it."""
    home = Path.home().resolve()
    try:
        exchange.resolve().relative_to(home)
        return home
    except ValueError:
        return exchange


# a running companion ----------------------------------------------------------------------


def running_companion(profile: Path) -> dict | None:
    """The companion already running for this profile (its page address), if it answers."""
    try:
        info = json.loads((profile / COMPANION_FILE).read_text(encoding="utf-8"))
        with urllib.request.urlopen(_req(info, "/api/ping"), timeout=3) as r:
            if json.loads(r.read()).get("ok"):
                return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def _req(info: dict, path: str, body: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(
        info["base"] + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"X-OpenFill-Key": info["key"], "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )


def hand_over(info: dict, files: list[str], command: list[str] | None) -> int:
    """Open ``files`` in the running companion's PhotoCraft and bring its bar up."""
    left = []
    for name in files:
        try:
            with urllib.request.urlopen(_req(info, "/api/open", {"path": str(Path(name).resolve())}), timeout=60):
                pass
        except OSError:
            left.append(name)
    if left and command:
        # Outside the folder OpenFill's PhotoCraft may read: open them in a PhotoCraft of their own.
        subprocess.Popen([*command, "--new-instance", *left], start_new_session=sys.platform != "win32")
    try:
        with urllib.request.urlopen(_req(info, "/api/show", {"action": "panel"}), timeout=5):
            pass
    except OSError:
        pass
    return 0


# messages ---------------------------------------------------------------------------------


def tell(title: str, text: str, ask: bool = False) -> bool:
    """A message box when there is no terminal to print to (started from the app menu)."""
    print(f"{title}: {text}", file=sys.stderr)
    if sys.stderr and sys.stderr.isatty() and not ask:
        return False
    try:
        import tkinter as tk

        from openfill_photocraft import ui

        root = tk.Tk(className="OpenFill")
        root.withdraw()
        try:
            if ask:
                return ui.ask(root, text, "OK", "warn")
            ui.message(root, text, "info")
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001 - no Tk (ComfyUI's embedded Python on Windows)
        if sys.platform == "win32":
            import ctypes

            flags = 0x1 | 0x30 if ask else 0x40  # OK/Cancel with a warning icon, or OK with info
            return ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000) == 1
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfill-photocraft", description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="images to open in PhotoCraft")
    parser.add_argument("--photocraft", help="path of the photocraft program (found automatically otherwise)")
    parser.add_argument("--port", type=int, default=7878, help="PhotoCraft control port (default 7878)")
    parser.add_argument("--web-port", type=int, default=0, help="port of the OpenFill page (default: any free one)")
    parser.add_argument("--page", action="store_true", help="also open the OpenFill page in the browser")
    parser.add_argument("--no-bar", action="store_true", help="no OpenFill window; use the page instead")
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # older name, no effect
    args = parser.parse_args(argv)

    profile = default_profile()
    exchange = profile / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    if not sys.stdout or not sys.stdout.isatty():
        # Started from the app menu: keep what would go to a terminal.
        log = open(profile / "companion.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        sys.stdout = sys.stderr = log
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "OpenFill for PhotoCraft starting", sys.argv[1:])

    command = find_photocraft(args.photocraft)
    running = running_companion(profile)
    if running:
        print("OpenFill is already running; handing over.")
        return hand_over(running, args.files, command)

    token = profile / "control.token"
    read_root = read_root_for(exchange)
    client = ControlClient(args.port, token)
    process = None
    if port_open(args.port):
        try:
            client.call("ui.inspect")
            print(f"Attached to the PhotoCraft on port {args.port}.")
        except ControlError:
            tell(
                "OpenFill",
                f"Something else is using port {args.port}. Close it, or start OpenFill with --port and a free one.",
            )
            return 2
        for name in args.files:
            try:
                client.call("app.open", path=os.path.relpath(Path(name).resolve(), read_root).replace(os.sep, "/"))
            except (ControlError, ValueError) as err:
                print(f"could not open {name}: {err}")
    else:
        if command is None:
            tell(
                "OpenFill",
                "PhotoCraft was not found. Install it from https://github.com/storytold/photocraft/releases "
                "and open OpenFill again.",
            )
            return 2
        while plain_photocraft_running():
            if not tell(
                "OpenFill",
                "PhotoCraft is already open without OpenFill.\n\nSave your work and close PhotoCraft, then press OK "
                "to open it again with OpenFill.",
                ask=True,
            ):
                return 1
        print("Starting PhotoCraft:", " ".join(command))
        process = launch(command, args.port, token, read_root, exchange, args.files, profile / "photocraft.log")
        for _ in range(240):
            if port_open(args.port) and token.is_file():
                break
            if process.poll() is not None:
                tell(
                    "OpenFill",
                    f"PhotoCraft closed right away (code {process.returncode}). See {profile / 'photocraft.log'}.",
                )
                return 2
            time.sleep(0.25)
        else:
            tell("OpenFill", "PhotoCraft did not start its control channel within a minute.")
            return 2

    from openfill_photocraft import menu
    from openfill_photocraft.service import Service
    from openfill_photocraft.web import Api, serve

    bridge = PhotoCraftBridge(client, exchange, profile, read_root=read_root)
    service = Service(client, bridge)
    api = Api(service)
    server, url = serve(api, args.web_port)
    base, key = url.split("/?key=")
    (profile / "page-url.txt").write_text(url + "\n", encoding="utf-8")

    holder: dict = {}

    def picked(action: str) -> None:
        bar = holder.get("bar")
        if bar is not None:
            bar.call_soon(bar.show, action)
        else:
            api.pick(action)  # the page switches to it on its next look

    watcher = menu.Watcher(client, picked)
    service.watcher = watcher
    client.on_wait = watcher.set_waiting
    api.on_show = picked
    bar = None
    if not args.no_bar:
        try:
            from openfill_photocraft.bar import Bar

            bar = holder["bar"] = Bar(service, watcher, url, profile)
        except Exception as err:  # noqa: BLE001 - no Tk or no display: use the page
            print("no OpenFill window (", err, "); using the page")
            bar = None
    try:
        labels = menu.install(client)
        print("Filter > Plug-ins:", ", ".join(labels) or "(this PhotoCraft has no plug-ins)")
    except ControlError as err:
        print("menu entries not added:", err)
    try:
        watcher.poll()  # so the page and the bar know the document from the start
    except ControlError:
        pass
    watcher.start()
    (profile / COMPANION_FILE).write_text(json.dumps({"pid": os.getpid(), "base": base, "key": key}), encoding="utf-8")
    print(f"OpenFill for PhotoCraft is running: {url}")
    service.warm_up()
    if os.environ.get("OPENFILL_NO_BROWSER"):
        pass  # tests and headless use: the address is in page-url.txt
    elif args.page or (bar is None and not app_window(url + "#fill")):
        webbrowser.open(url)

    def alive() -> bool:
        if process is not None:
            return process.poll() is None
        return client.alive()

    try:
        if bar is not None:
            bar.run(alive)
        else:
            while alive():
                time.sleep(1.0)
        print("PhotoCraft was closed; OpenFill stops too.")
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        server.shutdown()
        try:
            (profile / COMPANION_FILE).unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
