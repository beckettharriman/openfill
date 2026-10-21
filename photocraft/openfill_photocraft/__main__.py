# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Start OpenFill for PhotoCraft: launch PhotoCraft with its control channel (or attach to
one this launcher started earlier), then open the OpenFill page in the browser.

    python -m openfill_photocraft [--photocraft PATH] [--port 7878] [--no-browser]
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
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
    for folder in (home / "Applications", home / "Downloads", home / ".local" / "bin", home / "Desktop"):
        candidates += sorted(folder.glob("photocraft-*.AppImage"), reverse=True) if folder.is_dir() else []
        candidates += sorted(folder.glob("photocraft-*/bin/photocraft"), reverse=True) if folder.is_dir() else []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        candidates += [
            local / "Programs" / "PhotoCraft" / "photocraft.exe",
            Path(r"C:\Program Files\PhotoCraft\photocraft.exe"),
        ]
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


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch(command: list[str], port: int, token: Path, exchange: Path, log: Path) -> subprocess.Popen:
    args = list(command)
    if args[:2] == ["flatpak", "run"]:
        # The sandbox must see the exchange folder and the token.
        args.insert(2, f"--filesystem={exchange.parent}")
    args += [
        "--control",
        str(port),
        "--control-token-file",
        str(token),
        "--automation-read-root",
        str(exchange),
        "--automation-write-root",
        str(exchange),
    ]
    env = dict(os.environ)
    kwargs: dict = {"stdout": open(log, "ab"), "stderr": subprocess.STDOUT, "env": env}  # noqa: SIM115
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfill-photocraft", description=__doc__.split("\n\n")[0])
    parser.add_argument("--photocraft", help="path of the photocraft program (found automatically otherwise)")
    parser.add_argument("--port", type=int, default=7878, help="PhotoCraft control port (default 7878)")
    parser.add_argument("--web-port", type=int, default=0, help="port of the OpenFill page (default: any free one)")
    parser.add_argument("--no-browser", action="store_true", help="print the page address instead of opening it")
    args = parser.parse_args(argv)

    profile = default_profile()
    exchange = profile / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    token = profile / "control.token"
    client = ControlClient(args.port, token)

    process = None
    if port_open(args.port):
        try:
            client.call("ui.inspect")
            print(f"Attached to the PhotoCraft on port {args.port}.")
        except ControlError:
            print(
                f"Something is already using port {args.port} and is not a PhotoCraft started by OpenFill.\n"
                "Close PhotoCraft and start it again with this launcher (or pass --port with a free one).",
                file=sys.stderr,
            )
            return 2
    else:
        command = find_photocraft(args.photocraft)
        if command is None:
            print(
                "PhotoCraft was not found. Install it (https://github.com/storytold/photocraft/releases), or pass\n"
                "--photocraft /path/to/photocraft (or set PHOTOCRAFT_BIN).",
                file=sys.stderr,
            )
            return 2
        print("Starting PhotoCraft:", " ".join(command))
        process = launch(command, args.port, token, exchange, profile / "photocraft.log")
        for _ in range(120):
            if port_open(args.port) and token.is_file():
                break
            if process.poll() is not None:
                print(
                    f"PhotoCraft exited (code {process.returncode}); see {profile / 'photocraft.log'}", file=sys.stderr
                )
                return 2
            time.sleep(0.5)
        else:
            print("PhotoCraft did not open its control channel within a minute.", file=sys.stderr)
            return 2

    from openfill_photocraft.service import Service
    from openfill_photocraft.web import Api, serve

    bridge = PhotoCraftBridge(client, exchange, profile)
    service = Service(client, bridge)
    api = Api(service)
    server, url = serve(api, args.web_port)
    print(f"OpenFill for PhotoCraft is running: {url}")
    (profile / "page-url.txt").write_text(url + "\n", encoding="utf-8")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1.0)
            if process is not None and process.poll() is not None:
                print("PhotoCraft was closed; OpenFill stops too.")
                break
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
