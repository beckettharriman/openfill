# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Install, start, check and uninstall OpenFill for PhotoCraft against a real PhotoCraft
(the Windows CI job runs it after installing PhotoCraft's .msi). No GPU or ComfyUI needed.

    python photocraft/e2e/smoke_install.py [--first-run]

With ``--first-run`` (a fresh machine: no ComfyUI, no models) it also checks what the panel
shows the first time: the AI engine not found, no model installed, and on Windows that
Download ComfyUI resolves the archive it would fetch (a HEAD request, nothing downloaded).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def step(text: str) -> None:
    print(f"== {text}", flush=True)


def head(url: str) -> tuple[int, int]:
    """Status and Content-Length of ``url`` after redirects, without downloading it."""

    class KeepHead(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None:
                new.method = req.get_method()
            return new

    opener = urllib.request.build_opener(KeepHead)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "openfill-smoke"})
    with opener.open(req, timeout=60) as resp:
        return resp.status, int(resp.headers.get("Content-Length") or -1)


def first_run_checks(info: dict, state: dict) -> bool:
    """The first-run states of a fresh install: no ComfyUI, no models, Download ComfyUI ready."""
    ok = True

    def get(path: str) -> dict:
        req = urllib.request.Request(info["base"] + path, headers={"X-OpenFill-Key": info["key"]})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    step("first run: no ComfyUI")
    print("comfyReady:", state.get("comfyReady"), "| comfyProblem:", state.get("comfyProblem"))
    if state.get("comfyReady") or not state.get("comfyProblem"):
        print("FAIL: a fresh install should report the AI engine as not found")
        ok = False
    step("first run: no models")
    models = get("/api/models")
    installed = [r["id"] for r in models.get("rows", []) if r.get("installed")]
    print("rows:", len(models.get("rows", [])), "| installed:", installed, "| error:", models.get("error"))
    if installed:
        print("FAIL: a fresh install should have no model installed")
        ok = False
    if sys.platform == "win32":
        step("first run: Download ComfyUI resolves its archive")
        pre = get("/api/comfy/preflight")
        print({k: pre.get(k) for k in ("folder", "size", "url", "bytes", "blocked", "cpu", "text")})
        if not str(pre.get("folder", "")).lower().endswith(os.path.join("openfill", "comfyui").lower()):
            print("FAIL: the ComfyUI folder is not under %LOCALAPPDATA%\\OpenFill")
            ok = False
        try:
            status, length = head(pre["url"])
            print("HEAD", status, length)
            if status != 200 or length != pre["bytes"]:
                print("FAIL: the archive URL does not answer with the pinned size")
                ok = False
        except Exception as err:  # noqa: BLE001
            print("FAIL: the archive URL did not resolve:", err)
            ok = False
    return ok


def main() -> int:
    first_run = "--first-run" in sys.argv[1:]
    sys.path[:0] = [str(ROOT / "photocraft"), str(ROOT / "gimp-openfill")]
    from openfill_photocraft import install
    from openfill_photocraft.bridge import default_profile
    from openfill_photocraft.control import ControlClient

    zip_dir = ROOT / "dist-smoke"
    step("build the zip")
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_photocraft_zip.py"), "--out", str(zip_dir)], check=True)
    import zipfile

    archive = sorted(zip_dir.glob("openfill-photocraft-*.zip"))[-1]
    zipfile.ZipFile(archive).extractall(zip_dir)
    unpacked = zip_dir / archive.stem

    step("install")
    env = dict(os.environ, OPENFILL_PYTHON=sys.executable)
    if sys.platform == "win32":
        r = subprocess.run(
            ["cmd", "/c", "Install OpenFill for PhotoCraft.cmd"],
            cwd=unpacked,
            env=env,
            input="\n",
            capture_output=True,
            text=True,
            timeout=300,
        )
        links = install.windows_shortcut_paths()
        boot = install.windows_home() / "app" / install.BOOT
    else:
        r = subprocess.run(["sh", "install.sh"], cwd=unpacked, env=env, capture_output=True, text=True, timeout=300)
        links = [install.data_home() / "applications" / install.DESKTOP_ID]
        boot = None
    print(r.stdout[-2000:], r.stderr[-2000:])
    missing = [str(p) for p in links if not p.is_file()]
    if r.returncode != 0 or missing:
        print("install failed:", r.returncode, missing)
        return 1

    step("start OpenFill (it starts PhotoCraft)")
    profile = default_profile()
    (profile / "companion.json").unlink(missing_ok=True)
    run_env = dict(os.environ, OPENFILL_NO_BROWSER="1")
    command = [sys.executable, str(boot)] if boot else [str(Path.home() / ".local" / "bin" / "openfill-photocraft")]
    companion = subprocess.Popen([*command, "--no-bar"], env=run_env)
    info = None
    for _ in range(240):
        if (profile / "companion.json").is_file():
            info = json.loads((profile / "companion.json").read_text())
            break
        if companion.poll() is not None:
            break
        time.sleep(0.5)
    log = profile / "companion.log"
    print(log.read_text(errors="replace")[-3000:] if log.is_file() else "(no companion.log)")
    if info is None:
        print("OpenFill did not start")
        return 1
    state: dict = {}
    for _ in range(60):  # the watcher's first look at PhotoCraft
        req = urllib.request.Request(info["base"] + "/api/state", headers={"X-OpenFill-Key": info["key"]})
        with urllib.request.urlopen(req, timeout=30) as resp:
            state = json.loads(resp.read())
        if state["photocraft"].get("connected"):
            break
        time.sleep(0.5)
    print("photocraft:", state["photocraft"])
    pc = ControlClient(7878, profile / "control.token")
    entries = [m["label"] for m in pc.call("ui.menu.list") if m.get("path", [])[:2] == ["Filter", "Plug-ins"]]
    print("Filter > Plug-ins:", [label.encode("ascii", "replace").decode() for label in entries])
    ok = True
    if not state["photocraft"].get("connected"):
        print("FAIL: the page does not see PhotoCraft")
        ok = False
    if "OpenFill" not in entries:
        print("FAIL: no OpenFill entries in Filter > Plug-ins")
        ok = False
    if first_run and not first_run_checks(info, state):
        ok = False

    step("close PhotoCraft: OpenFill stops too")
    try:
        pc.call("app.quit")
    except Exception as err:  # noqa: BLE001 - the connection closes as it quits
        print("quit:", err)
    try:
        companion.wait(60)
    except subprocess.TimeoutExpired:
        companion.kill()
        print("OpenFill did not stop after PhotoCraft closed")
        ok = False

    step("uninstall")
    if sys.platform == "win32":
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from openfill_photocraft.install import main; raise SystemExit(main(['--uninstall']))",
                str(unpacked),
            ],
            check=True,
        )
    else:
        subprocess.run([str(Path.home() / ".local" / "bin" / "openfill-photocraft-uninstall")], check=True)
    left = [str(p) for p in links if p.is_file() and install.MARKER in p.read_text(errors="replace")]
    if sys.platform == "win32":
        left = [str(p) for p in links if p.is_file()]
    if left:
        print("uninstall left:", left)
        ok = False
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
