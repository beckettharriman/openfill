# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check against a real Patchy (not a unit test), with the stub generator from
patchy/tests/stub_backend.py in place of ComfyUI: no GPU or models needed.

    python patchy/e2e/run_patchy_e2e.py --patchy PATH/patchy.exe [--out DIR]

Starts its own Patchy (offscreen, its own settings folder and connector endpoint, so a
Patchy you have open is never touched) on a test image, attaches the companion through
``patchy-mcp --attach`` and runs the companion's own jobs: a fill (one masked layer, one
Undo removes it), three variations with the picker and Keep, Discard, an expand and text to
image. Checks layers, masks, pixels and history through the connector and writes
report.json to --out.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
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
from openfill_patchy.client import PatchyClient, PatchyError  # noqa: E402
from openfill_patchy.service import PatchyService, Watcher  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

report: list[dict] = []
failures: list[str] = []


def record(case: str, ok: bool, **data) -> None:
    entry = {"case": case, "ok": bool(ok), **data}
    report.append(entry)
    if not ok:
        failures.append(case)
    print(("PASS " if ok else "FAIL ") + json.dumps(entry)[:600], flush=True)


def js(client: PatchyClient, code: str, args: dict | None = None):
    client.get_state()
    return client.run_script("var A = " + json.dumps(args or {}) + ";\n" + code, name="e2e")


INSPECT = """
var d = app.getDocument(A.doc);
function rows(list) {
  return list.map(function (l) {
    var m = l.getLayerMask && !l.isGroup ? l.getLayerMask() : null;
    var out = {id: l.id, name: l.name, visible: l.visible, isGroup: l.isGroup, x: l.x, y: l.y};
    if (m) { out.mask = {x: m.x, y: m.y, width: m.width, height: m.height, feather: m.feather}; }
    if (l.isGroup) out.children = rows(l.children);
    return out;
  });
}
var c = d.getCompositePixels({x: A.px, y: A.py, width: 1, height: 1});
var bytes = new Uint8Array(c.data);
patchy.setResult({width: d.width, height: d.height, layers: rows(d.layers),
  pixel: [bytes[0], bytes[1], bytes[2], bytes[3]], canUndo: d.canUndo});
"""

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


def inspect(client, doc, px=0, py=0) -> dict:
    return js(client, INSPECT, {"doc": doc, "px": px, "py": py})


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
    endpoint = "OpenFillPatchyE2E-" + uuid.uuid4().hex
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
            info.get("workspace") == "attached" and int(info.get("processId") or 0) == app.pid,
            version=info.get("version"),
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
            "selection event",
            bool(changed) and changed[-1].get("bounds") == bounds,
            bounds=bounds,
            events=changed[-1:] or None,
        )
        want = [bounds[k] for k in ("x", "y", "width", "height")]
        record("watcher follows the selection", watcher.snapshot().get("selection") == want, watcher=watcher.snapshot())

        # reading changes nothing
        before = history(client, doc)
        image = bridge.track(bridge.active_image())
        visible, selection = bridge._snapshot(image)
        record(
            "read is not a history step",
            history(client, doc) == before and visible.size == (256, 192) and selection is not None,
            selection_bbox=selection.getbbox() if selection else None,
            soft_edge=[selection.getpixel((x, 80)) for x in (64, 65, 66, 67, 68)] if selection else None,
        )

        # 1. a fill: one masked layer, the context inverted; one Undo takes it back
        base_state = history(client, doc)
        base = inspect(client, doc, 112, 80)
        st = run_job(service, "fill", {"prompt": "a red door", "variations": 1, "seed": 7})
        after = inspect(client, doc, 112, 80)
        top = after["layers"][-1]
        steps = undo_count_back(client, doc, base_state) if st["state"] == "done" else -1
        undone = inspect(client, doc, 112, 80)
        record(
            "fill inserts a masked layer that one Undo removes",
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
        service.keep("selected")
        kept = inspect(client, doc)
        steps = undo_count_back(client, doc, base_state)
        record(
            "variations in a group, Keep leaves one step",
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
            "Discard leaves no step",
            st["state"] == "done"
            and history(client, doc) == base_state
            and len(inspect(client, doc)["layers"]) == layers_before,
            history_back=history(client, doc) == base_state,
        )

        # 4. an expand: canvas and layers in one step
        base_state = history(client, doc)
        st = run_job(service, "expand", {"right": 128, "variations": 1, "seed": 7})
        grown = inspect(client, doc, 300, 20)
        steps = undo_count_back(client, doc, base_state) if st["state"] == "done" else -1
        back = inspect(client, doc)
        record(
            "expand is one step",
            st["state"] == "done" and grown["width"] == 384 and steps == 1 and back["width"] == 256,
            job=st["state"],
            message=st["message"],
            width=grown["width"],
            new_area_pixel=grown["pixel"],
            layers=[(r["name"], r["x"], r["y"]) for r in grown["layers"]],
            undos_to_remove=steps,
        )

        # 5. an expand to the left on a new document, whose Background layer is locked in place
        made = js(client, "var d = app.newDocument(200, 150, {name: 'Background test'}); patchy.setResult(d.id);")
        bg_doc = str(made)
        js(
            client,
            "var d = app.getDocument(A.doc); d.layers[0].fillRect(0, 0, 200, 150, '#3366cc'); patchy.setResult(true);",
            {"doc": bg_doc},
        )
        base_state = history(client, bg_doc)
        st = run_job(service, "expand", {"left": 64, "variations": 1, "seed": 7})
        grown = inspect(client, bg_doc, 100, 75) if st["state"] == "done" else {}
        steps = undo_count_back(client, bg_doc, base_state) if st["state"] == "done" else -1
        record(
            "expand left moves a Background layer with the canvas",
            st["state"] == "done" and grown.get("width") == 264 and grown["layers"][0]["x"] == 64 and steps == 1,
            job=st["state"],
            message=st["message"],
            width=grown.get("width"),
            layers=[(r["name"], r["x"], r["y"]) for r in grown.get("layers") or []],
            old_area_pixel=grown.get("pixel"),
            undos_to_remove=steps,
        )

        # 6. text to image as a new document
        docs_before = len(client.get_state()["documents"])
        st = run_job(service, "txt2img", {"prompt": "a fox", "width": 128, "height": 96, "variations": 2, "seed": 7})
        state = client.get_state()
        new_doc = state["documents"][-1]
        record(
            "text to image opens a new document",
            st["state"] == "done" and len(state["documents"]) == docs_before + 1,
            job=st["state"],
            message=st["message"],
            document={k: new_doc.get(k) for k in ("name", "width", "height")},
            layers=[(r["name"], r["visible"]) for r in new_doc.get("layers") or []],
        )

        record("no files left in the exchange folder", not list((profile / "exchange").iterdir()))
    except Exception as err:  # noqa: BLE001 - reported
        import traceback

        traceback.print_exc()
        record("e2e run", False, error=str(err))
    finally:
        client.close()
        app.terminate()  # the Patchy this script started, by its own handle
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
        app_log.close()
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{len(report) - len(failures)} of {len(report)} passed; report in {out / 'report.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
