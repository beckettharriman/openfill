# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check against a running companion and PhotoCraft (not a unit test).

    python photocraft/e2e/run_e2e.py [--out DIR]

Opens porsche-v2.png from the exchange folder (copy it there first), then drives the
companion's own HTTP API exactly as the page does: expand, a prompted fill with three
variations and the picker, Remove object, text to image as a layer and as a new image.
Checks layers, masks and pixels through the control channel and writes a report and
screenshots to --out.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "gimp-openfill")]

from openfill_photocraft.bridge import default_profile  # noqa: E402
from openfill_photocraft.control import ControlClient  # noqa: E402

PROFILE = default_profile()
URL = (PROFILE / "page-url.txt").read_text().strip()
BASE, KEY = URL.split("/?key=")
pc = ControlClient(7878, PROFILE / "control.token")
report: list[dict] = []


def api(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-OpenFill-Key": KEY, "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def job(kind: str, body: dict, timeout: float = 900) -> dict:
    t0 = time.time()
    api(f"/api/{kind}", body)
    seqs = set()
    while time.time() - t0 < timeout:
        st = api("/api/state")["job"]
        seqs.add(st["previewSeq"])
        if st["state"] != "running":
            st["seconds"] = round(time.time() - t0, 1)
            st["previewFrames"] = len(seqs) - 1
            return st
        time.sleep(1.5)
    raise TimeoutError(kind)


def layers() -> list[dict]:
    return pc.run("layer.list", flat=True)["layers"]


def doc() -> dict:
    return pc.run("document.inspect")


def shot(out: Path, name: str) -> None:
    s = pc.call("ui.screenshot")
    (out / f"{name}.png").write_bytes(base64.b64decode(s["base64"]))


def export(out: Path, name: str):
    from PIL import Image

    pc.run("image.duplicate", name="e2e", mergedOnly=True)
    pc.call("app.save", path=f"e2e-{name}.png")
    pc.run("file.close")
    src = PROFILE / "exchange" / f"e2e-{name}.png"
    im = Image.open(src).convert("RGB")
    im.save(out / f"{name}-full.png")
    src.unlink()
    return im


def record(case: str, **data) -> None:
    entry = {"case": case, **data}
    report.append(entry)
    print(json.dumps(entry)[:400], flush=True)


def close_all() -> None:
    for _ in range(10):
        if not pc.call("ui.inspect")["session"]["documents"]:
            return
        pc.run("file.close")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE / "out"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    close_all()
    pc.call("app.open", path="porsche-v2.png")
    w0, h0 = doc()["width"], doc()["height"]

    # 1. expand top and bottom by 270 (4:3 towards portrait)
    st = job(
        "expand",
        {
            "prompt": "",
            "left": 0,
            "top": 270,
            "right": 0,
            "bottom": 270,
            "variations": 1,
            "seed": 7,
            "model": "dreamshaper-8-inpainting",
        },
    )
    d = doc()
    im = export(out, "1-expand")
    # brightness step across the old bottom edge (y = 270 + h0), a few columns
    step = max(
        abs(sum(im.getpixel((x, 270 + h0 + 1))) - sum(im.getpixel((x, 270 + h0 - 2)))) / 3
        for x in range(100, w0 - 100, 97)
    )
    edge = [sum(im.getpixel((x, 2))) / 3 for x in range(0, w0, 181)] + [
        sum(im.getpixel((w0 - 2, y))) / 3 for y in range(0, d["height"], 203)
    ]
    record(
        "expand",
        state=st["state"],
        message=st["message"],
        seconds=st["seconds"],
        previews=st["previewFrames"],
        canvas=[d["width"], d["height"]],
        expected=[w0, h0 + 540],
        layers=[(row["name"], row.get("bounds")) for row in layers()],
        old_edge_max_step=round(step, 1),
        canvas_edge_min=round(min(edge), 1),
        canvas_edge_max=round(max(edge), 1),
    )
    shot(out, "1-expand")

    # 2. prompted fill with three variations on top, picker, keep selected
    pc.run("select.rect", x=60, y=290, width=380, height=300)
    st = job(
        "fill",
        {
            "prompt": "a red and yellow hot air balloon",
            "variations": 3,
            "strength": 1,
            "seed": 21,
            "model": "realvisxl-v5",
        },
    )
    rows = layers()
    top = rows[0]
    record(
        "fill-3-variations",
        state=st["state"],
        message=st["message"],
        seconds=st["seconds"],
        previews=st["previewFrames"],
        variations=st["variations"],
        top_layer=(top["name"], top.get("depth")),
        layers=[(row["name"], row.get("depth"), row.get("visible")) for row in rows],
    )
    api("/api/variation", {"index": 1})
    vis = [(row["name"], row.get("visible")) for row in layers() if row.get("depth")]
    api("/api/keep", {"which": "selected"})
    after = [(row["name"], row.get("depth")) for row in layers()]
    record("picker", shown_after_pick=vis, after_keep=after, selection_kept=doc().get("selectionBounds"))
    shot(out, "2-fill")

    # 3. remove object on an ellipse (LaMa)
    pc.run("select.rect", x=250, y=680, width=960, height=480, ellipse=True)
    st = job("fill", {"prompt": "", "remove": True, "variations": 1, "strength": 1, "seed": 3, "model": "active"})
    record("remove", state=st["state"], message=st["message"], seconds=st["seconds"], top=layers()[0]["name"])
    export(out, "3-remove")
    shot(out, "3-remove")

    # 4. text to image as a layer of this document, then as a new document
    pc.run("select.deselect")
    st = job(
        "txt2img",
        {
            "prompt": "a cozy log cabin in a snowy pine forest at dusk, photo",
            "width": 512,
            "height": 384,
            "variations": 1,
            "seed": 3,
            "model": "realvisxl-v5",
            "target": "layer",
        },
    )
    record(
        "txt2img-layer",
        state=st["state"],
        message=st["message"],
        seconds=st["seconds"],
        top=(layers()[0]["name"], layers()[0].get("bounds")),
    )
    st = job(
        "txt2img",
        {
            "prompt": "a cozy log cabin in a snowy pine forest at dusk, photo",
            "width": 1024,
            "height": 768,
            "variations": 2,
            "seed": 3,
            "model": "realvisxl-v5",
            "target": "image",
        },
    )
    docs = [d["name"] for d in pc.call("ui.inspect")["session"]["documents"]]
    record(
        "txt2img-image",
        state=st["state"],
        message=st["message"],
        seconds=st["seconds"],
        documents=docs,
        new_doc_layers=[(row["name"], row.get("visible")) for row in layers()],
        size=[doc()["width"], doc()["height"]],
    )
    shot(out, "4-txt2img")

    (out / "report.json").write_text(json.dumps(report, indent=2))
    bad = [r["case"] for r in report if r.get("state") not in (None, "done")]
    print("FAILED:" if bad else "ALL DONE", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
