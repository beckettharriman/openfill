# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Inside GIMP: run one Generative Fill so the floating progress window can be
observed/captured from outside, and record the outcome. Launch twice:

  normal run:   OPENFILL_TEST_OUT=.../progress-window/normal
  cancel run:   OPENFILL_TEST_CANCEL_AT=0.3 (set in the launcher shell so the plug-in
                process inherits it) and OPENFILL_TEST_OUT=.../progress-window/cancel

The report records the procedure status, the layers before/after, the
ComfyUI queue state afterwards and the processes the plug-in left behind.
"""

import json
import os
import sys
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio

REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
sys.path.insert(0, os.path.join(REPO, "gimp-openfill"))
IMAGE = os.environ.get("OPENFILL_TEST_IMAGE", os.path.join(REPO, "tests", "data", "test-scene.png"))
OUT = os.environ.get("OPENFILL_TEST_OUT", os.path.join(REPO, "docs", "evidence", "progress-window"))
os.makedirs(OUT, exist_ok=True)
report = {
    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    "gimp_pid": os.getpid(),
    "cancel_at": os.environ.get("OPENFILL_TEST_CANCEL_AT"),
    "steps": [],
}


def step(name, **info):
    info["time"] = time.strftime("%H:%M:%S")
    report["steps"].append({"step": name, **info})
    print(f"[sb-progress] {name}: {json.dumps(info, default=str)[:300]}")


def layers(image):
    return [{"name": layer.get_name(), "has_mask": layer.get_mask() is not None} for layer in image.get_layers()]


try:
    from openfill.app import App
    from openfill.comfy_api import ComfyClient

    app = App()
    step(
        "settings",
        progress_window=app.settings.progress_window,
        position=(app.settings.progress_window_x, app.settings.progress_window_y),
    )
    url = app.server.ensure_running(lambda f, t: None)  # warm backend so the run is about sampling
    step("backend", url=url)

    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(IMAGE))
    Gimp.Display.new(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 420, 300, 260, 200)
    before = layers(image)
    with open(os.path.join(OUT, "fill-started.marker"), "w") as fh:
        fh.write(str(time.time()))
    proc = Gimp.get_pdb().lookup_procedure("python-fu-openfill-generative-fill")
    config = proc.create_config()
    config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    config.set_property("image", image)
    config.set_core_object_array("drawables", image.get_layers())
    config.set_property("prompt", "a large grey boulder on grass, photo")
    config.set_property("negative", "blurry")
    config.set_property("variations", 1)
    config.set_property("strength", 1.0)
    config.set_property("seed", 777)
    config.set_property("model", "fast")
    t0 = time.time()
    result = proc.run(config)
    status = result.index(0)
    after = layers(image)
    step(
        "generative-fill",
        status=str(status),
        seconds=round(time.time() - t0, 1),
        layers_before=before,
        layers_after=after,
        layer_added=len(after) > len(before),
    )
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(os.path.join(OUT, "after.png")))

    client = ComfyClient(url)
    q = client.queue()
    step("comfy-queue", running=len(q.get("queue_running", [])), pending=len(q.get("queue_pending", [])))
    report["ok"] = True
except Exception as err:  # noqa: BLE001
    report["ok"] = False
    report["error"] = f"{type(err).__name__}: {err}"
    report["traceback"] = traceback.format_exc()
    print(report["traceback"], file=sys.stderr)
finally:
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(OUT, "batch-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    if os.environ.get("OPENFILL_TEST_QUIT", "1") == "1":
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
