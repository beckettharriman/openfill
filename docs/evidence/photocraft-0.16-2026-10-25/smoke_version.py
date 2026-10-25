"""Short check of one PhotoCraft version with the running companion.

    python3 smoke_version.py PORT OUT.json

The Filter > Plug-ins > OpenFill entry is listed and picking it reaches the companion (and leaves
no history step), an image opens, a LaMa Remove adds one layer and one history step, and one
Undo takes it back. Writes the result to OUT.json.
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
BASE, KEY = (PROFILE / "page-url.txt").read_text().strip().split("/?key=")
pc = ControlClient(int(sys.argv[1]), PROFILE / "control.token")


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-OpenFill-Key": KEY, "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def state():
    d = pc.run("document.inspect")
    rows = pc.run("layer.list", flat=True)["layers"]
    return {"history": d["history"], "layers": [(r["name"], r.get("bounds")) for r in rows]}


out: dict = {}
for _ in range(10):
    if not pc.call("ui.inspect")["session"]["documents"]:
        break
    pc.run("file.close")
pc.call("app.open", path=os.path.relpath(PROFILE / "exchange" / "porsche-v2.png", Path.home()))
entries = [m["id"] for m in pc.call("ui.menu.list") if m.get("path", [])[:2] == ["Filter", "Plug-ins"] and "openfill" in m["id"]]
out["menu_entries"] = entries
before = state()
seq0 = api("/api/state")["pick"]["seq"]
pc.call("ui.menu.invoke", id="plugin.filter.app.openfill.panel")
picked = False
for _ in range(40):
    time.sleep(0.5)
    if api("/api/state")["pick"]["seq"] != seq0:
        picked = True
        break
time.sleep(2)  # the watcher deletes the entry's history step on its next look
out["menu_pick_reached_companion"] = picked
out["menu_pick_left_no_step"] = state()["history"] == before["history"]

pc.run("select.rect", x=250, y=680, width=700, height=300, ellipse=True)
before = state()
api("/api/fill", {"prompt": "", "remove": True, "variations": 1, "strength": 1, "seed": 3, "model": "lama-big"})
t0 = time.time()
while api("/api/state")["job"]["state"] == "running" and time.time() - t0 < 600:
    time.sleep(1)
job = api("/api/state")["job"]
mid = state()
out["remove"] = {"state": job["state"], "message": job["message"], "seconds": round(time.time() - t0, 1)}
out["remove_steps_added"] = mid["history"][len(before["history"]) :]
out["remove_layers_added"] = len(mid["layers"]) - len(before["layers"])
pc.call("ui.menu.invoke", id="edit.undo")
time.sleep(0.5)
out["undo_restored"] = state() == before
out["ok"] = bool(
    entries == ["plugin.filter.app.openfill.panel"]
    and picked
    and out["menu_pick_left_no_step"]
    and job["state"] == "done"
    and len(out["remove_steps_added"]) == 1
    and out["remove_layers_added"] == 1
    and out["undo_restored"]
)
Path(sys.argv[2]).write_text(json.dumps(out, indent=2))
print(json.dumps(out))
