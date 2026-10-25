"""Extra end-to-end cases for PhotoCraft 0.16 (not in the repo's e2e): Fill with 1 variation,
Keep, Discard, Remove, Expand, Create as layer and as image, Cancel, and one Undo after each.

    python3 e2e_extra.py PORT OUT_DIR

Drives the running companion's HTTP API like run_e2e.py does; checks history and layers through
the control channel. Undo is PhotoCraft's own Edit > Undo (ui.menu.invoke edit.undo).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path("/home/beckett/projects/photocraft-openfill")
sys.path[:0] = [str(REPO / "photocraft"), str(REPO / "gimp-openfill")]

from openfill_photocraft.bridge import default_profile  # noqa: E402
from openfill_photocraft.control import ControlClient  # noqa: E402

PROFILE = default_profile()
URL = (PROFILE / "page-url.txt").read_text().strip()
BASE, KEY = URL.split("/?key=")
pc = ControlClient(int(sys.argv[1]), PROFILE / "control.token")
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
SD15 = "dreamshaper-8-inpainting"
report: list[dict] = []


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-OpenFill-Key": KEY, "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def ndocs():
    return len(pc.call("ui.inspect")["session"]["documents"])


def doc():
    return pc.run("document.inspect")


def history():
    return doc()["history"]


def layer_rows():
    return [(r["name"], tuple(r.get("bounds") or ()), r.get("visible")) for r in pc.run("layer.list", flat=True)["layers"]]


def wait_job(timeout=1200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api("/api/state")["job"]
        if st["state"] != "running":
            st["seconds"] = round(time.time() - t0, 1)
            return st
        time.sleep(1.0)
    raise TimeoutError


def undo():
    pc.call("ui.menu.invoke", id="edit.undo")
    time.sleep(0.5)


def record(case, ok, **data):
    entry = {"case": case, "ok": bool(ok), **data}
    report.append(entry)
    print(json.dumps(entry)[:700], flush=True)


def state():
    return {"history": history(), "layers": layer_rows(), "size": [doc()["width"], doc()["height"]]}


def run_case(case, kind, body, after=None):
    """Start a job, check it adds one history step, then Undo once and check it is all gone."""
    before = state()
    docs0 = ndocs()
    api(f"/api/{kind}", body)
    st = wait_job()
    if after:
        after(st)
    mid = state()
    added = mid["history"][len(before["history"]) :] if mid["history"][: len(before["history"])] == before["history"] else mid["history"]
    undo()
    back = state()
    ok = st["state"] == "done" and len(added) == 1 and back == before and ndocs() == docs0
    record(
        case,
        ok,
        state=st["state"],
        message=st.get("message"),
        seconds=st["seconds"],
        steps_added=added,
        layers_after=mid["layers"],
        size_after=mid["size"],
        undo_restored=back == before,
    )
    return st


def main():
    for _ in range(10):
        if not ndocs():
            break
        pc.run("file.close")
    pc.call("app.open", path=os.path.relpath(PROFILE / "exchange" / "porsche-v2.png", Path.home()))
    w0, h0 = doc()["width"], doc()["height"]
    record("open", (w0, h0) == (1448, 1086), size=[w0, h0], history=history())

    # Fill, 1 variation (kept as it is), one Undo
    pc.run("select.rect", x=60, y=60, width=300, height=240)
    run_case("fill-1-variation", "fill", {"prompt": "a hot air balloon", "variations": 1, "strength": 1, "seed": 5, "model": SD15})

    # Fill, 3 variations, Keep the second, one Undo
    def keep_second(st):
        api("/api/variation", {"index": 1})
        api("/api/keep", {"which": "selected"})

    run_case(
        "fill-3-keep",
        "fill",
        {"prompt": "a hot air balloon", "variations": 3, "strength": 1, "seed": 6, "model": SD15},
        keep_second,
    )

    # Fill, 3 variations, Discard: nothing is left, no history step
    before = state()
    api("/api/fill", {"prompt": "a hot air balloon", "variations": 3, "strength": 1, "seed": 7, "model": SD15})
    st = wait_job()
    shown = state()
    api("/api/keep", {"which": "none"})
    time.sleep(1)
    after = state()
    record(
        "fill-3-discard",
        st["state"] == "done" and after == before and len(shown["layers"]) > len(before["layers"]),
        state=st["state"],
        layers_while_shown=len(shown["layers"]),
        history_after=after["history"],
        same_as_before=after == before,
    )

    # Remove (LaMa), one Undo
    pc.run("select.rect", x=250, y=680, width=700, height=300, ellipse=True)
    run_case("remove-lama", "fill", {"prompt": "", "remove": True, "variations": 1, "strength": 1, "seed": 3, "model": "lama-big"})

    # Expand left and right, one Undo takes the canvas back
    pc.run("select.deselect")
    run_case(
        "expand",
        "expand",
        {"prompt": "", "left": 128, "top": 0, "right": 128, "bottom": 0, "variations": 1, "seed": 7, "model": SD15},
    )

    # Create as a layer, one Undo
    run_case(
        "create-layer",
        "txt2img",
        {"prompt": "a log cabin in snow", "width": 512, "height": 384, "variations": 1, "seed": 3, "model": SD15, "target": "layer"},
    )

    # Create as an image: a new document
    docs0 = ndocs()
    api("/api/txt2img", {"prompt": "a log cabin in snow", "width": 512, "height": 384, "variations": 1, "seed": 3, "model": SD15, "target": "image"})
    st = wait_job()
    d = doc()
    record(
        "create-image",
        st["state"] == "done" and ndocs() == docs0 + 1 and [d["width"], d["height"]] == [512, 384],
        state=st["state"],
        documents_added=ndocs() - docs0,
        size=[d["width"], d["height"]],
        history=d["history"],
        layers=layer_rows(),
    )
    pc.run("file.close")

    # Cancel a fill part way: the document is as before
    pc.run("select.rect", x=60, y=60, width=300, height=240)
    before = state()
    api("/api/fill", {"prompt": "a hot air balloon", "variations": 3, "strength": 1, "seed": 8, "model": SD15})
    time.sleep(8)
    api("/api/cancel", {})
    st = wait_job()
    time.sleep(1)
    after = state()
    record("cancel", st["state"] != "done" and after == before, state=st["state"], message=st.get("message"), same_as_before=after == before)

    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    bad = [r["case"] for r in report if not r["ok"]]
    print("FAILED:" if bad else "ALL OK", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
