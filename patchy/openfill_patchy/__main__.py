# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Start OpenFill next to a running Patchy: attach through Patchy's connector and show the
OpenFill panel. Patchy starts it from Plugins > Companions > OpenFill (and sends a running
one ``companionCommand`` instead; "quit" from that menu ends it as Patchy closing does). It
can also be started by hand while Patchy is open.

    python -m openfill_patchy [--connector PATH] [--page] [--no-bar]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The shared OpenFill core and the PhotoCraft companion's panel: next to this package in a
# release, in gimp-openfill/ and photocraft/ in the repository.
for candidate in (HERE.parent, HERE.parent.parent / "gimp-openfill", HERE.parent.parent / "photocraft"):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openfill_patchy import __version__  # noqa: E402
from openfill_patchy.bridge import PatchyBridge, default_profile  # noqa: E402
from openfill_patchy.client import (  # noqa: E402
    COMPANION_PROTOCOL_MAX,
    COMPANION_PROTOCOL_MIN,
    FIRST_PATCHY_WITH_COMPANIONS,
    PatchyClient,
    PatchyError,
    find_connector,
    protocol_problem,
)

REOPEN = "Plugins > Companions > OpenFill in Patchy"


def tell(title: str, text: str) -> None:
    """A message box when there is no terminal to print to (started from Patchy's menu)."""
    print(f"{title}: {text}", file=sys.stderr)
    if sys.stderr and sys.stderr.isatty():
        return
    try:
        import tkinter as tk

        from openfill_photocraft import ui

        root = tk.Tk(className="OpenFill")
        root.withdraw()
        try:
            ui.message(root, text, "info")
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001 - no Tk (ComfyUI's embedded Python on Windows)
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x10000)


def until_quit(alive: Callable[[], bool], quit_asked: threading.Event) -> Callable[[], bool]:
    """``alive`` that also turns false when Patchy's menu asks OpenFill to quit, so a quit
    ends OpenFill the same way as Patchy closing."""
    return lambda: not quit_asked.is_set() and alive()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfill-patchy", description=__doc__.split("\n\n")[0])
    parser.add_argument("--connector", help="path of patchy-mcp (found automatically otherwise)")
    parser.add_argument("--web-port", type=int, default=0, help="port of the OpenFill page (default: any free one)")
    parser.add_argument("--page", action="store_true", help="also open the OpenFill page in the browser")
    parser.add_argument("--no-bar", action="store_true", help="no OpenFill window; use the page instead")
    args = parser.parse_args(argv)

    profile = default_profile()
    exchange = profile / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    if not sys.stdout or not sys.stdout.isatty():
        # Started from Patchy's menu: keep what would go to a terminal.
        log = open(profile / "companion.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        sys.stdout = sys.stderr = log
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "OpenFill for Patchy starting", sys.argv[1:])

    command = find_connector(args.connector)
    if command is None:
        # Only possible when started by hand: Patchy's menu says where its connector is.
        tell(
            "OpenFill",
            f"Patchy's connector (patchy-mcp) was not found. Install Patchy {FIRST_PATCHY_WITH_COMPANIONS} or newer"
            " and open OpenFill from its Plugins > Companions menu, or start OpenFill with --connector and the"
            " path of patchy-mcp.",
        )
        return 2
    holder: dict = {}

    def picked(action: str) -> None:
        bar = holder.get("bar")
        if bar is not None:
            bar.call_soon(bar.show, action)
        elif holder.get("api") is not None:
            holder["api"].pick(action)  # the page switches to it on its next look

    def on_event(event: dict) -> None:
        watcher = holder.get("watcher")
        if watcher is not None:
            watcher.on_event(event)

    client = PatchyClient(command, on_event=on_event, log=profile / "connector.log")
    try:
        client.start()
        info = client.get_info()
    except PatchyError as err:
        tell("OpenFill", f"Could not reach Patchy: {err}")
        client.close()
        return 2
    if info.get("workspace") != "attached" or not info.get("workspaceAvailable", True):
        tell("OpenFill", "Open Patchy first, then pick OpenFill in Plugins > Companions.")
        client.close()
        return 2
    print(f"Attached to Patchy {info.get('version')} (process {info.get('processId')}). Connector: {' '.join(command)}")
    print(
        f"OpenFill for Patchy {__version__}, companion protocol {COMPANION_PROTOCOL_MIN} to {COMPANION_PROTOCOL_MAX};"
        f" Patchy {info.get('version')}, companionProtocol {info.get('companionProtocol')}."
    )
    problem = protocol_problem(info)
    if problem:
        tell("OpenFill", problem)
        client.close()
        return 2

    from openfill_photocraft.web import Api

    from openfill_patchy.page import serve  # web.serve without its host-name lookup (see page.py)
    from openfill_patchy.service import PatchyService, Watcher

    bridge = PatchyBridge(client, exchange, profile)
    service = PatchyService(client, bridge)
    quit_asked = threading.Event()
    watcher = holder["watcher"] = Watcher(client, picked, on_quit=quit_asked.set)
    watcher.native_id = int((info.get("mainWindow") or {}).get("nativeId") or 0)
    service.watcher = watcher
    client.on_wait = watcher.set_waiting
    api = holder["api"] = Api(service)
    server, url = serve(api, args.web_port)
    api.on_show = picked
    (profile / "page-url.txt").write_text(url + "\n", encoding="utf-8")
    bar = None
    if not args.no_bar:
        try:
            from openfill_photocraft.bar import Bar

            from openfill_patchy.tether import PatchyTether

            tether = PatchyTether(lambda: watcher.native_id)
            bar = Bar(service, watcher, url, profile, host="Patchy", reopen=REOPEN, tether=tether)
            holder["bar"] = bar
            if sys.platform == "win32":
                from openfill_patchy.focus import FocusReturn

                panel: dict[str, int] = {}

                def read_panel() -> None:  # on the Tk thread; the panel's window lives as long as OpenFill
                    panel["hwnd"] = int(bar.root.wm_frame(), 16)

                bar.call_soon(read_panel)
                service.focus = FocusReturn(lambda: watcher.native_id, lambda: panel.get("hwnd", 0), log=print)
        except Exception as err:  # noqa: BLE001 - no Tk or no display: use the page
            import traceback

            traceback.print_exc()
            print("no OpenFill window (", err, "); using the page")
            bar = None
    try:
        watcher.poll()  # so the panel knows the document from the start
    except PatchyError:
        pass
    watcher.start()
    print(f"OpenFill for Patchy is running: {url}")
    service.warm_up()
    if os.environ.get("PATCHY_COMPANION_ID") and bar is not None:
        bar.call_soon(bar.show, "panel")  # picked in Plugins > Companions: open, not the strip
    if os.environ.get("OPENFILL_NO_BROWSER"):
        pass  # tests and headless use: the address is in page-url.txt
    elif args.page or bar is None:
        import webbrowser

        webbrowser.open(url + "#fill", new=2)

    alive = until_quit(client.alive, quit_asked)
    try:
        if bar is not None:
            bar.run(alive)
        else:
            while alive():
                time.sleep(1.0)
        if quit_asked.is_set():
            print("Quit from Patchy's menu.")
        else:
            print("Patchy was closed; OpenFill stops too.")
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        server.shutdown()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
