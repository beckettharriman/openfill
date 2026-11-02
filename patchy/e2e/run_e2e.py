# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check against a real Patchy (not a unit test), with the stand-in generator from
patchy/tests/stub_backend.py in place of ComfyUI: no GPU or models needed.

    python patchy/e2e/run_e2e.py --patchy PATH/patchy.exe [--connector PATH] [--out DIR]

Starts its own Patchy (offscreen, its own settings folder and connector endpoint, so a
Patchy you have open is never touched) on a test image, attaches the companion through
``patchy-mcp --attach`` and runs the companion's own jobs: a fill (one masked layer, one
Undo removes it), three variations with the picker and Keep, Discard, an expand and text to
image. Checks layers, masks, pixels and history through the connector and writes a report
and the document's image after each job to --out.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [
    str(ROOT / "patchy"),
    str(ROOT / "patchy" / "tests"),
    str(ROOT / "photocraft"),
    str(ROOT / "gimp-openfill"),
]

import stub_backend  # noqa: E402
from openfill_patchy.bridge import PatchyBridge  # noqa: E402
from openfill_patchy.client import PatchyClient, PatchyError, protocol_problem  # noqa: E402
from openfill_patchy.service import PatchyService, Watcher  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

report: list[dict] = []


def record(case: str, ok: bool, **data) -> None:
    entry = {"case": case, "ok": bool(ok), **data}
    report.append(entry)
    print(json.dumps(entry)[:500], flush=True)


def js(client: PatchyClient, code: str, args: dict | None = None):
    client.get_state()
    return client.run_script("var A = " + json.dumps(args or {}) + ";\n" + code, name="e2e")


# The visible pixel at (px, py) and the bottom layer's own pixel there; the layers come
# from get_state, which reports each raster mask (hasLayerMask, layerMask).
INSPECT = """
var d = app.getDocument(A.doc);
var c = d.getCompositePixels({x: A.px, y: A.py, width: 1, height: 1});
var bytes = new Uint8Array(c.data);
var p = d.layers[0].getPixels(), own = null;
if (A.px >= p.x && A.py >= p.y && A.px < p.x + p.width && A.py < p.y + p.height) {
  var b = new Uint8Array(p.data), i = ((A.py - p.y) * p.width + (A.px - p.x)) * 4;
  own = [b[i], b[i + 1], b[i + 2], b[i + 3]];
}
patchy.setResult({width: d.width, height: d.height, pixel: [bytes[0], bytes[1], bytes[2], bytes[3]],
  bottomPixel: own, canUndo: d.canUndo});
"""


def layer_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        row = {k: r.get(k) for k in ("id", "name", "visible", "isGroup", "bounds")}
        row["x"], row["y"] = (r.get("bounds") or {}).get("x"), (r.get("bounds") or {}).get("y")
        if r.get("hasLayerMask"):
            row["mask"] = r.get("layerMask")
        if r.get("isGroup"):
            row["children"] = layer_rows(r.get("children") or [])
        out.append(row)
    return out


SELECT = """
var d = app.getDocument(A.doc);
var w = A.w, h = A.h, data = new Uint8Array(w * h);
for (var y = 0; y < h; y++) for (var x = 0; x < w; x++) {
  var e = Math.min(x, y, w - 1 - x, h - 1 - y);
  data[y * w + x] = e >= 4 ? 255 : 64 * e;  // a soft edge four pixels wide
}
d.selection.setMask({x: A.x, y: A.y, width: w, height: h, data: data.buffer});
patchy.setResult(d.selection.bounds);
"""


SHOT = """
var d = app.getDocument(A.doc);
patchy.io.writeImage(A.path, d.getCompositePixels());
patchy.setResult(true);
"""


def shot(client, doc, out: Path, name: str) -> None:
    """The document's image as Patchy composites it (Patchy runs offscreen: no screenshots)."""
    try:
        js(client, SHOT, {"doc": doc, "path": str(out / f"{name}.png")})
    except PatchyError as err:
        print("image failed:", err)


def inspect(client, doc, px=0, py=0) -> dict:
    """The document's size, layers (bottom first, as get_state lists them) and pixels."""
    facts = js(client, INSPECT, {"doc": doc, "px": px, "py": py})
    d = next(d for d in client.get_state()["documents"] if d["id"] == doc)
    return {**facts, "layers": layer_rows(d.get("layers") or [])}


def history(client, doc) -> str:
    d = next(d for d in client.get_state()["documents"] if d["id"] == doc)
    return str(d["historyStateId"])


def run_job(service: PatchyService, kind: str, params: dict, timeout: float = 120) -> dict:
    service.start(kind, params)
    deadline = time.monotonic() + timeout
    while service.busy() and time.monotonic() < deadline:
        time.sleep(0.1)
    return service.state.snapshot()


def undo_count_back(client, doc, to_state: str, limit: int = 5) -> int:
    """Undos until the history is at ``to_state``; how many it took."""
    for n in range(1, limit + 1):
        client.get_state()
        client.undo(doc)
        if history(client, doc) == to_state:
            return n
    return -1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patchy", required=True, help="patchy.exe (or patchy) of the build to test")
    parser.add_argument("--connector", help="patchy-mcp beside it by default")
    parser.add_argument("--out", default=str(HERE / "out"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    patchy = Path(args.patchy).resolve()
    connector = Path(args.connector) if args.connector else patchy.with_name("patchy-mcp" + patchy.suffix)

    scratch = Path(tempfile.mkdtemp(prefix="openfill-patchy-e2e-"))
    settings = scratch / "settings"
    ini = settings / "RTsoft" / "Patchy" / "Patchy.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("[updates]\ncheckOnStartup=false\n", encoding="utf-8")
    photo = scratch / "photo.png"
    im = Image.new("RGB", (256, 192), (200, 100, 50))
    ImageDraw.Draw(im).rectangle((0, 120, 255, 191), fill=(30, 140, 60))
    im.save(photo)
    # Short: on macOS the socket is $TMPDIR/<name>, and a socket path has room for 104 bytes.
    endpoint = "ofp-e2e-" + uuid.uuid4().hex[:8]
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PATCHY_SETTINGS_DIR": str(settings),
        "PATCHY_MCP_ENDPOINT": endpoint,
        "PATCHY_NO_SINGLE_INSTANCE": "1",
        "PATCHY_NO_SOUND": "1",
        "PATCHY_COMPANION_ID": "io.github.beckettharriman.openfill",
    }
    app_log = (out / "patchy-app.log").open("w", encoding="utf-8")
    app = subprocess.Popen([str(patchy), str(photo)], cwd=scratch, env=env, stdout=app_log, stderr=app_log)
    events: list[dict] = []
    holder: dict = {}

    def on_event(event: dict) -> None:
        events.append(event)
        if holder.get("watcher"):
            holder["watcher"].on_event(event)

    client = PatchyClient([str(connector), "--attach"], on_event=on_event, env=env, log=out / "connector.log")
    try:
        client.start()
        deadline = time.monotonic() + 60
        info = {}
        while time.monotonic() < deadline:
            try:
                info = client.get_info(patience=0)
                if info.get("workspace") == "attached" and info.get("workspaceAvailable", True):
                    state = client.get_state(patience=0)
                    if state.get("documents"):
                        break
            except PatchyError:
                pass
            time.sleep(0.5)
        record(
            "attach",
            info.get("workspace") == "attached"
            and int(info.get("processId") or 0) == app.pid
            and protocol_problem(info) is None,
            version=info.get("version"),
            companionProtocol=info.get("companionProtocol"),
            mainWindow=info.get("mainWindow"),
            companionEvents="companionEvents" in (info.get("capabilities") or []),
        )
        state = client.get_state()
        doc = str(state["activeDocumentId"])
        watcher = holder["watcher"] = Watcher(client, lambda action: None, fallback=60.0)
        watcher.poll()

        profile = scratch / "profile"
        bridge = PatchyBridge(client, profile / "exchange", profile)
        service = PatchyService(client, bridge)
        service.watcher = watcher
        stub = stub_backend.install(service.app)
        service.app.settings.seam_correction = False  # cosmetic passes; the stub's flat result has no seam
        service.app.settings.grain_match = False
        service.app.settings.rank_variations = True

        # selection, and the event that reports it
        events.clear()
        bounds = js(client, SELECT, {"doc": doc, "x": 64, "y": 48, "w": 96, "h": 64})
        time.sleep(0.6)
        changed = [e for e in events if e.get("type") == "selectionChanged"]
        record(
            "selection_event",
            bool(changed) and changed[-1].get("bounds") == bounds,
            bounds=bounds,
            events=changed[-1:] or None,
        )
        want = [bounds[k] for k in ("x", "y", "width", "height")]
        record("watcher_follows_selection", watcher.snapshot().get("selection") == want, watcher=watcher.snapshot())

        # reading changes nothing
        before = history(client, doc)
        image = bridge.track(bridge.active_image())
        visible, selection = bridge._snapshot(image)
        record(
            "read_adds_no_step",
            history(client, doc) == before and visible.size == (256, 192) and selection is not None,
            selection_bbox=selection.getbbox() if selection else None,
            soft_edge=[selection.getpixel((x, 80)) for x in (64, 65, 66, 67, 68)] if selection else None,
        )

        # 1. a fill: one masked layer, the context inverted; one Undo takes it back
        base_state = history(client, doc)
        base = inspect(client, doc, 112, 80)
        shot(client, doc, out, "0-before")
        st = run_job(service, "fill", {"prompt": "a red door", "variations": 1, "seed": 7})
        after = inspect(client, doc, 112, 80)
        shot(client, doc, out, "1-fill")
        top = after["layers"][-1]
        steps = undo_count_back(client, doc, base_state) if st["state"] == "done" else -1
        undone = inspect(client, doc, 112, 80)
        record(
            "fill_one_step",
            st["state"] == "done"
            and top["name"] == "Generative Fill: a red door"
            and bool(top.get("mask"))
            and after["pixel"][:3] == [255 - v for v in base["pixel"][:3]]
            and steps == 1
            and len(undone["layers"]) == len(base["layers"])
            and undone["pixel"] == base["pixel"],
            job=st["state"],
            message=st["message"],
            layer=top,
            pixel_before=base["pixel"],
            pixel_after=after["pixel"],
            undos_to_remove=steps,
            requests=len(stub.requests),
        )

        # 2. three variations, the picker, Keep: still one step
        base_state = history(client, doc)
        st = run_job(service, "fill", {"prompt": "a window", "variations": 3, "seed": 7})
        group = inspect(client, doc)["layers"][-1]
        service.show_variation(2)
        shot(client, doc, out, "2-variation-3")
        service.keep("selected")
        kept = inspect(client, doc)
        steps = undo_count_back(client, doc, base_state)
        record(
            "variations_keep_one_step",
            st["state"] == "done"
            and group.get("isGroup")
            and [c["visible"] for c in reversed(group["children"])] == [True, False, False]
            and not kept["layers"][-1].get("isGroup")
            and kept["layers"][-1]["name"] == "Generative Fill: a window"
            and bool(kept["layers"][-1].get("mask"))
            and steps == 1,
            group=[(c["name"], c["visible"]) for c in group.get("children") or []],
            kept=kept["layers"][-1],
            undos_to_remove=steps,
        )

        # 3. Discard after picking leaves no step
        base_state = history(client, doc)
        layers_before = len(inspect(client, doc)["layers"])
        st = run_job(service, "fill", {"prompt": "a tree", "variations": 2, "seed": 7})
        service.show_variation(1)
        service.keep("none")
        record(
            "discard_no_step",
            st["state"] == "done"
            and history(client, doc) == base_state
            and len(inspect(client, doc)["layers"]) == layers_before,
            history_back=history(client, doc) == base_state,
        )

        # 4. an expand: canvas and layers in one step
        base_state = history(client, doc)
        st = run_job(service, "expand", {"right": 128, "variations": 1, "seed": 7})
        grown = inspect(client, doc, 300, 20)
        shot(client, doc, out, "3-expand")
        steps = undo_count_back(client, doc, base_state) if st["state"] == "done" else -1
        back = inspect(client, doc)
        record(
            "expand_one_step",
            st["state"] == "done"
            and grown["width"] == 384
            and (grown["layers"][0]["x"], grown["layers"][0]["y"]) == (0, 0)
            and all(r.get("mask") for r in grown["layers"][1:])
            and steps == 1
            and back["width"] == 256,
            job=st["state"],
            message=st["message"],
            width=grown["width"],
            new_area_pixel=grown["pixel"],
            layers=[(r["name"], r["x"], r["y"]) for r in grown["layers"]],
            undos_to_remove=steps,
        )

        # 5. an expand to the left on a new document: its Background layer grows to cover the
        # new canvas, filled with the image's mean colour where the result leaves gaps
        made = js(client, "var d = app.newDocument(200, 150, {name: 'Background test'}); patchy.setResult(d.id);")
        bg_doc = str(made)
        js(
            client,
            "var d = app.getDocument(A.doc); d.layers[0].fillRect(0, 0, 200, 150, '#3366cc'); patchy.setResult(true);",
            {"doc": bg_doc},
        )
        base_state = history(client, bg_doc)
        st = run_job(service, "expand", {"left": 64, "variations": 1, "seed": 7})
        done = st["state"] == "done"
        new_area = inspect(client, bg_doc, 10, 75) if done else {}
        old_area = inspect(client, bg_doc, 164, 75) if done else {}
        shot(client, bg_doc, out, "4-expand-left")
        steps = undo_count_back(client, bg_doc, base_state) if done else -1
        back = inspect(client, bg_doc) if done else {}
        layers = new_area.get("layers") or [{}]
        blue = [0x33, 0x66, 0xCC, 255]
        record(
            "expand_left_background",
            done
            and new_area.get("width") == 264
            and layers[0].get("name") == "Background"
            and layers[0].get("bounds") == {"x": 0, "y": 0, "width": 264, "height": 150}
            and new_area.get("bottomPixel") == blue  # the fill: the image's mean colour
            and old_area.get("bottomPixel") == blue  # the old content, moved 64 px right
            and all(r.get("mask") for r in layers[1:])
            and steps == 1
            and back.get("width") == 200
            and (back.get("layers") or [{}])[0].get("bounds") == {"x": 0, "y": 0, "width": 200, "height": 150},
            job=st["state"],
            message=st["message"],
            width=new_area.get("width"),
            layers=[(r.get("name"), r.get("bounds"), r.get("mask")) for r in layers],
            background_new_area=new_area.get("bottomPixel"),
            background_old_area=old_area.get("bottomPixel"),
            visible_new_area=new_area.get("pixel"),
            undos_to_remove=steps,
        )

        # 6. text to image as a new document
        docs_before = len(client.get_state()["documents"])
        st = run_job(service, "txt2img", {"prompt": "a fox", "width": 128, "height": 96, "variations": 2, "seed": 7})
        state = client.get_state()
        new_doc = state["documents"][-1]
        shot(client, str(new_doc["id"]), out, "5-txt2img")
        record(
            "txt2img_new_image",
            st["state"] == "done" and len(state["documents"]) == docs_before + 1,
            job=st["state"],
            message=st["message"],
            document={k: new_doc.get(k) for k in ("name", "width", "height")},
            layers=[(r["name"], r["visible"]) for r in new_doc.get("layers") or []],
        )

        record("exchange_folder_empty", not list((profile / "exchange").iterdir()))

        # 7. the real launcher (no panel) attached next to this script, and how it ends: a
        # Quit from Plugins > Companions ends it through this same path (``until_quit``), but
        # an offscreen Patchy offers no way to pick that menu entry from outside.
        launcher_profile = scratch / "launcher-appdata"
        settings_dir = launcher_profile / "OpenFill for Patchy" / "openfill"
        settings_dir.mkdir(parents=True)
        (settings_dir / "config.json").write_text('{"autostart": false}', encoding="utf-8")
        launcher_env = {
            **env,
            "APPDATA": str(launcher_profile),  # its own settings and logs, never ComfyUI
            "LOCALAPPDATA": str(scratch / "launcher-local"),
            "OPENFILL_NO_BROWSER": "1",
        }
        # Its output to a file, not NUL (on Windows NUL reports itself as a terminal), so the
        # launcher writes companion.log as it does when Patchy starts it.
        log_path = launcher_profile / "OpenFill for Patchy" / "companion.log"
        launcher_log = (out / "launcher-output.log").open("w", encoding="utf-8")
        launcher = subprocess.Popen(
            [sys.executable, "-m", "openfill_patchy", "--no-bar", "--connector", str(connector)],
            cwd=ROOT / "patchy",
            env=launcher_env,
            stdin=subprocess.DEVNULL,
            stdout=launcher_log,
            stderr=launcher_log,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and launcher.poll() is None:
            if log_path.exists() and "OpenFill for Patchy is running" in log_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.5)
        running = launcher.poll() is None
        app.terminate()  # Patchy closes
        app.wait(timeout=30)
        try:
            code = launcher.wait(timeout=30)
        except subprocess.TimeoutExpired:
            launcher.kill()
            code = None
        launcher_log.close()
        text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        record(
            "launcher_exits_when_patchy_closes",
            running and code == 0 and "Patchy was closed; OpenFill stops too." in text,
            attached="Attached to Patchy" in text,
            was_running=running,
            exit_code=code,
            said_patchy_closed="Patchy was closed; OpenFill stops too." in text,
        )
    except Exception as err:  # noqa: BLE001 - reported
        record("e2e_run", False, error=f"{err}", trace=traceback.format_exc()[-800:])
    finally:
        client.close()
        app.terminate()  # the Patchy this script started, by its own handle
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
        app_log.close()
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    bad = [r["case"] for r in report if not r["ok"]]
    print("FAILED:" if bad else "ALL DONE", bad, flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
