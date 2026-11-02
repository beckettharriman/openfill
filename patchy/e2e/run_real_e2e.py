# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check with a real ComfyUI and real models (not a unit test): the companion as
an artist installs and uses it, against a Patchy build.

    python patchy/e2e/run_real_e2e.py --patchy PATH/patchy [--connector PATH] [--python PATH]
        [--comfy DIR] [--home-root DIR] [--out DIR]

Installs OpenFill (``python -m openfill_patchy.install``), starts its own Patchy (offscreen,
its own settings folder and connector endpoint) on tests/data/test-scene.png, starts the
companion (``python -m openfill_patchy --no-bar``) and drives it through its page's API, the
calls the panel makes: the ComfyUI folder (``--comfy``, when it is not found by itself),
LaMa and DreamShaper 8 Inpainting through the model manager's download, then Remove, Fill
with 3 variations and Keep, Fill with 3 and Discard, Expand left and Create. Each result is
checked to be one undo step (one Undo takes it back, Redo puts it back). Then Patchy closes:
the companion has to stop, and so does a ComfyUI it started. Last, the uninstall, which must
leave settings and models in place. Writes a report and the document's image after each job
to --out.

``--home-root`` keeps the companion's settings and the companions folder under that folder
(``APPDATA`` on Windows, ``HOME`` on macOS, the XDG folders on Linux) instead of the user's.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "patchy"), str(ROOT / "photocraft"), str(ROOT / "gimp-openfill")]

from openfill_patchy.client import (  # noqa: E402
    PatchyClient,
    PatchyError,
    connector_command,
    is_appimage,
    protocol_problem,
)

report: list[dict] = []
LAMA, DREAMSHAPER = "lama-big", "dreamshaper-8-inpainting"


def record(case: str, ok: bool, **data) -> None:
    entry = {"case": case, "ok": bool(ok), **data}
    report.append(entry)
    print(json.dumps(entry)[:700], flush=True)


def js(client: PatchyClient, code: str, args: dict | None = None):
    client.get_state()
    return client.run_script("var A = " + json.dumps(args or {}) + ";\n" + code, name="e2e")


SELECT = """
var d = app.getDocument(A.doc);
var data = new Uint8Array(A.w * A.h);
data.fill(255);
d.selection.setMask({x: A.x, y: A.y, width: A.w, height: A.h, data: data.buffer});
patchy.setResult(d.selection.bounds);
"""

SHOT = """
var d = app.getDocument(A.doc);
patchy.io.writeImage(A.path, d.getCompositePixels());
patchy.setResult({width: d.width, height: d.height});
"""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def isolated(env: dict, home_root: Path | None) -> dict:
    """``env`` with the companion's per-user folders under ``home_root``."""
    if home_root is None:
        return env
    env = dict(env)
    if sys.platform == "win32":
        env["APPDATA"] = str(home_root / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        env["HOME"] = str(home_root)
    else:
        env["XDG_CONFIG_HOME"] = str(home_root / ".config")
        env["XDG_DATA_HOME"] = str(home_root / ".local" / "share")
    return env


def folders(python: str, env: dict) -> tuple[Path, Path]:
    """(the companion's profile, Patchy's companions folder) as the companion sees them."""
    code = (
        "import sys; sys.path[:0] = ['../photocraft', '../gimp-openfill']\n"
        "from openfill_patchy.bridge import default_profile\n"
        "from openfill_patchy.install import companions_dir\n"
        "print(default_profile()); print(companions_dir())\n"
    )
    out = subprocess.run(
        [python, "-c", code], cwd=ROOT / "patchy", env=env, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return Path(out[0]), Path(out[1])


class Page:
    """The companion's page API, as the panel and the browser page use it."""

    def __init__(self, url: str) -> None:
        self.base, self.key = url.strip().split("/?key=")

    def __call__(self, path: str, body: dict | None = None, timeout: float = 120):
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-OpenFill-Key": self.key, "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patchy", required=True, help="patchy.exe (or patchy) of the build to test")
    parser.add_argument("--connector", help="patchy-mcp beside it by default")
    parser.add_argument("--python", default=sys.executable, help="the Python the companion runs with")
    parser.add_argument("--comfy", help="ComfyUI folder to choose when the companion does not find one")
    parser.add_argument("--home-root", help="keep the companion's per-user folders here")
    parser.add_argument("--out", default=str(HERE / "out-real"))
    parser.add_argument("--keep-install", action="store_true", help="skip the uninstall at the end")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    patchy = Path(args.patchy).resolve()
    # Patchy's AppImage is its own connector (``<file> patchy-mcp``); elsewhere patchy-mcp is beside patchy.
    default = patchy if is_appimage(patchy) else patchy.with_name("patchy-mcp" + patchy.suffix)
    connector = Path(args.connector) if args.connector else default
    home_root = Path(args.home_root).resolve() if args.home_root else None
    python = args.python

    scratch = Path(tempfile.mkdtemp(prefix="openfill-patchy-real-"))
    ini = scratch / "settings" / "RTsoft" / "Patchy" / "Patchy.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("[updates]\ncheckOnStartup=false\n", encoding="utf-8")
    # Short: on macOS the socket is $TMPDIR/<name>, and a socket path has room for 104 bytes.
    endpoint = "ofp-real-" + uuid.uuid4().hex[:8]
    base_env = {**os.environ, "PATCHY_MCP_ENDPOINT": endpoint}
    patchy_env = {
        **base_env,
        "QT_QPA_PLATFORM": "offscreen",
        "PATCHY_SETTINGS_DIR": str(scratch / "settings"),
        "PATCHY_NO_SINGLE_INSTANCE": "1",
        "PATCHY_NO_SOUND": "1",
    }
    companion_env = isolated({**base_env, "OPENFILL_NO_BROWSER": "1"}, home_root)
    profile, companions = folders(python, companion_env)
    record("environment", True, os=platform.platform(), machine=platform.machine(), python=python)

    # 1. install, as the README says
    result = subprocess.run(
        [python, "-m", "openfill_patchy.install", "--python", python],
        cwd=ROOT / "patchy",
        env=companion_env,
        capture_output=True,
        text=True,
    )
    manifest = companions / "openfill.json"
    entry = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    record(
        "install",
        result.returncode == 0 and entry.get("minProtocol") == 1,
        exit_code=result.returncode,
        manifest_keys=sorted(entry),
        output=(result.stdout + result.stderr).strip()[-300:],
    )

    image = scratch / "test-scene.png"
    image.write_bytes((ROOT / "tests" / "data" / "test-scene.png").read_bytes())
    app_log = (out / "patchy-app.log").open("w", encoding="utf-8")
    app = subprocess.Popen([str(patchy), str(image)], cwd=scratch, env=patchy_env, stdout=app_log, stderr=app_log)
    client = PatchyClient(connector_command(str(connector)), env=base_env, log=out / "connector.log")
    companion = None
    companion_log = None
    comfy_pid = 0
    try:
        client.start()
        deadline = time.monotonic() + 90
        info: dict = {}
        while time.monotonic() < deadline:
            try:
                info = client.get_info(patience=0)
                if info.get("workspace") == "attached" and client.get_state(patience=0).get("documents"):
                    break
            except PatchyError:
                pass
            time.sleep(0.5)
        record(
            "attach",
            info.get("workspace") == "attached" and protocol_problem(info) is None,
            version=info.get("version"),
            companionProtocol=info.get("companionProtocol"),
        )
        doc = str(client.get_state()["activeDocumentId"])

        def history() -> str:
            d = next(d for d in client.get_state()["documents"] if str(d["id"]) == doc)
            return str(d["historyStateId"])

        def one_step(before: str) -> bool:
            """One Undo goes back to ``before``; Redo puts the result back."""
            after = history()
            client.get_state()
            client.undo(doc)
            back = history() == before
            client.get_state()
            client.redo(doc)
            return back and history() == after and after != before

        def shot(name: str, target: str | None = None) -> None:
            try:
                js(client, SHOT, {"doc": target or doc, "path": str(out / f"{name}.png")})
            except PatchyError as err:
                print("image failed:", err)

        # 2. the companion, started as Patchy's menu would start it
        log_path = profile / "companion.log"
        offset = log_path.stat().st_size if log_path.exists() else 0
        companion_log = (out / "companion-output.log").open("w", encoding="utf-8")
        started = time.monotonic()
        companion = subprocess.Popen(
            [python, "-m", "openfill_patchy", "--no-bar", "--connector", str(connector)],
            cwd=ROOT / "patchy",
            env=companion_env,
            stdin=subprocess.DEVNULL,
            stdout=companion_log,
            stderr=companion_log,
        )
        text = ""
        while time.monotonic() - started < 120 and companion.poll() is None:
            if log_path.exists():
                text = log_path.read_bytes()[offset:].decode("utf-8", "replace")
                if "OpenFill for Patchy is running" in text:
                    break
            time.sleep(0.5)
        record(
            "companion_starts",
            "OpenFill for Patchy is running" in text,
            seconds=round(time.monotonic() - started, 1),
            log=[line for line in text.splitlines() if "protocol" in line],
        )
        page = Page((profile / "page-url.txt").read_text(encoding="utf-8"))

        # 3. ComfyUI: found by itself, or chosen like Choose folder
        state = page("/api/state")
        found = bool(state.get("comfyReady"))
        if not found and args.comfy:
            page("/api/settings", {"comfy_dir": str(Path(args.comfy).resolve())})
            state = page("/api/state")
        record(
            "comfy_folder",
            bool(state.get("comfyReady")),
            found_by_itself=found,
            chosen=None if found else args.comfy,
            problem=state.get("comfyProblem"),
        )

        # 4. the models, through the model manager's download
        def rows() -> dict:
            return {row["id"]: row for row in page("/api/models")["rows"]}

        for model in (LAMA, DREAMSHAPER):
            row = rows().get(model) or {}
            t0, queued = time.monotonic(), False
            if not row.get("installed"):
                page("/api/models/download", {"id": model})
                queued = True
            while time.monotonic() - t0 < 3600:
                listing = page("/api/models")
                row = next((r for r in listing["rows"] if r["id"] == model), {})
                busy = (listing.get("current") or {}).get("model_id") == model or model in (
                    listing.get("pending") or []
                )
                if row.get("installed") and not busy:
                    break
                time.sleep(5)
            nodes = bool(row.get("installed") and row.get("missing_nodes"))
            if nodes:  # "Needs extra parts": the model manager's Install nodes
                page("/api/models/nodes", {"id": model})
                while time.monotonic() - t0 < 3600:
                    row = rows().get(model) or {}
                    if not row.get("missing_nodes"):
                        break
                    time.sleep(5)
            record(
                f"model_{model}",
                bool(row.get("installed")) and bool(row.get("runnable")) and not row.get("missing_nodes"),
                downloaded=queued,
                installed_nodes=nodes,
                seconds=round(time.monotonic() - t0, 1),
                status=row.get("statusText") or row.get("status"),
                missing_nodes=row.get("missing_nodes"),
                missing_models=row.get("missing_models"),
            )
        page("/api/models/activate", {"id": DREAMSHAPER})
        t0 = time.monotonic()
        check = page("/api/backend/check", {}, timeout=900)
        record(
            "backend_start",
            bool(check.get("ok")),
            seconds=round(time.monotonic() - t0, 1),
            device=check.get("device"),
            detail=check.get("detail"),
            hardware=page("/api/models").get("hardware"),
        )
        server_state = profile / "openfill" / "server-state.json"
        if server_state.is_file():
            comfy_pid = int(json.loads(server_state.read_text(encoding="utf-8")).get("server_pid") or 0)

        def job(kind: str, body: dict, timeout: float = 1800) -> dict:
            t0 = time.monotonic()
            page(f"/api/{kind}", body)
            while time.monotonic() - t0 < timeout:
                st = page("/api/state")["job"]
                if st["state"] != "running":
                    st["seconds"] = round(time.monotonic() - t0, 1)
                    return st
                time.sleep(1.0)
            raise TimeoutError(kind)

        def layers() -> list:
            d = next(d for d in client.get_state()["documents"] if str(d["id"]) == doc)
            return d.get("layers") or []

        shot("0-before")
        # 5. Remove with LaMa (the dark box in the test scene)
        js(client, SELECT, {"doc": doc, "x": 420, "y": 300, "w": 260, "h": 200})
        before = history()
        st = job(
            "fill",
            {
                "variations": 1,
                "seed": 7,
                "model": "active",
                "prompt": "",
                "remove": True,
                "strength": 1.0,
                "negative": "",
                "removal": LAMA,
            },
        )
        shot("1-remove")
        record("remove_lama", st["state"] == "done" and one_step(before), **_job(st), layers=_names(layers()))

        # 6. Fill, 3 variations, pick the second, Keep: one step
        js(client, SELECT, {"doc": doc, "x": 120, "y": 360, "w": 220, "h": 220})
        before = history()
        fill = {
            "variations": 3,
            "seed": 7,
            "model": DREAMSHAPER,
            "prompt": "a red wooden door",
            "remove": False,
            "strength": 1.0,
            "negative": "",
        }
        st = job("fill", fill)
        group = _names(layers())
        page("/api/variation", {"index": 1})
        shot("2-fill-variation-2")
        page("/api/keep", {"which": "selected"})
        kept = _names(layers())
        record("fill_3_keep_one_step", st["state"] == "done" and one_step(before), **_job(st), group=group, kept=kept)

        # 7. Fill, 3 variations, Discard: no step
        before, count = history(), len(layers())
        st = job("fill", {**fill, "prompt": "a blue window", "seed": 11})
        page("/api/variation", {"index": 2})
        page("/api/keep", {"which": "none"})
        record(
            "fill_3_discard_no_step",
            st["state"] == "done" and history() == before and len(layers()) == count,
            **_job(st),
        )

        # 8. Expand 160 px on the left
        width = next(d for d in client.get_state()["documents"] if str(d["id"]) == doc)["width"]
        before = history()
        st = job(
            "expand",
            {
                "variations": 1,
                "seed": 7,
                "model": DREAMSHAPER,
                "prompt": "",
                "negative": "",
                "left": 160,
                "top": 0,
                "right": 0,
                "bottom": 0,
            },
        )
        d = next(d for d in client.get_state()["documents"] if str(d["id"]) == doc)
        shot("3-expand-left")
        record(
            "expand_left_one_step",
            st["state"] == "done" and d["width"] == width + 160 and one_step(before),
            **_job(st),
            width=[width, d["width"]],
            bottom=(d.get("layers") or [{}])[0].get("bounds"),
        )

        # 9. Create as a new image
        docs = len(client.get_state()["documents"])
        st = job(
            "txt2img",
            {
                "variations": 1,
                "seed": 7,
                "model": DREAMSHAPER,
                "prompt": "a red fox sitting in fresh snow, photo",
                "negative": "",
                "width": 512,
                "height": 512,
                "target": "image",
            },
        )
        state = client.get_state()
        made = state["documents"][-1]
        shot("4-create", str(made["id"]))
        record(
            "create_new_image",
            st["state"] == "done" and len(state["documents"]) == docs + 1,
            **_job(st),
            size=[made.get("width"), made.get("height")],
        )

        # 10. Patchy closes: the companion stops, and the ComfyUI it started
        record("comfy_running_before_close", pid_alive(comfy_pid), server_pid=comfy_pid)
        client.close()
        app.terminate()
        app.wait(timeout=60)
        t0 = time.monotonic()
        try:
            code = companion.wait(timeout=60)
        except subprocess.TimeoutExpired:
            code = None
        companion_seconds = round(time.monotonic() - t0, 1)
        while pid_alive(comfy_pid) and time.monotonic() - t0 < 120:
            time.sleep(1)
        text = log_path.read_bytes()[offset:].decode("utf-8", "replace")
        record(
            "patchy_closes_companion_and_comfy_stop",
            code == 0 and "Patchy was closed" in text and not pid_alive(comfy_pid),
            companion_exit=code,
            companion_seconds=companion_seconds,
            comfy_stopped_after=round(time.monotonic() - t0, 1),
            comfy_alive=pid_alive(comfy_pid),
        )

        # 11. uninstall: the entry goes, settings and models stay
        if not args.keep_install:
            result = subprocess.run(
                [python, "-m", "openfill_patchy.install", "--uninstall"],
                cwd=ROOT / "patchy",
                env=companion_env,
                capture_output=True,
                text=True,
            )
            config = json.loads((profile / "openfill" / "config.json").read_text(encoding="utf-8"))
            from openfill.config import comfy_layout

            layout = comfy_layout(config["comfy_dir"]) if config.get("comfy_dir") else None
            models_root = layout["main"].parent / "models" if layout else Path("missing")
            kept_models = {
                name: (models_root / name).is_file()
                for name in ("inpaint/big-lama.pt", "checkpoints/DreamShaper_8_INPAINTING.inpainting.safetensors")
            }
            record(
                "uninstall",
                result.returncode == 0 and not manifest.exists() and all(kept_models.values()),
                manifest_left=manifest.exists(),
                settings_kept=(profile / "openfill" / "config.json").is_file(),
                models_kept=kept_models,
                still_running=[pid for pid in (companion.pid, comfy_pid, app.pid) if pid_alive(pid)],
            )
    except Exception as err:  # noqa: BLE001 - reported
        record("real_run", False, error=f"{err}", trace=traceback.format_exc()[-1200:])
    finally:
        client.close()
        for proc in (companion, app):
            if proc is not None and proc.poll() is None:
                proc.terminate()  # started by this script, stopped by its own handle
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
        app_log.close()
        if companion_log is not None:
            companion_log.close()
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    bad = [r["case"] for r in report if not r["ok"]]
    print("FAILED:" if bad else "ALL DONE", bad, flush=True)
    return 1 if bad else 0


def _job(st: dict) -> dict:
    return {
        "state": st.get("state"),
        "message": st.get("message"),
        "seconds": st.get("seconds"),
        "model": st.get("model"),
        "variations": st.get("variations"),
    }


def _names(rows: list) -> list:
    return [[r.get("name"), r.get("visible"), _names(r.get("children") or [])] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
