# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Install, start, check and uninstall OpenFill for PhotoCraft against a real PhotoCraft
(the Windows CI job runs it after installing PhotoCraft's .msi). No GPU or ComfyUI needed.

    python photocraft/e2e/smoke_install.py
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


def main() -> int:
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
    req = urllib.request.Request(info["base"] + "/api/state", headers={"X-OpenFill-Key": info["key"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        state = json.loads(resp.read())
    print("state:", json.dumps(state)[:600])
    pc = ControlClient(7878, profile / "control.token")
    entries = [m["label"] for m in pc.call("ui.menu.list") if m.get("path", [])[:2] == ["Filter", "Plug-ins"]]
    print("Filter > Plug-ins:", entries)
    ok = state["photocraft"]["connected"] and any(label.startswith("OpenFill Generative Fill") for label in entries)

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
