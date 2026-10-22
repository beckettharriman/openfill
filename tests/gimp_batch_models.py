# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check of the model catalog features, executed INSIDE GIMP via
python-fu-eval (see gimp_batch_fill.py for the command line). It downloads
one small catalog entry (MAT, 125 MB, published sha256), verifies the status
machinery, switches the active model, runs a prompt-free removal, installs a
custom node pack through the supervisor (backend restart), then switches back
to SD 1.5 and runs a normal fill.

Environment:
    OPENFILL_REPO, OPENFILL_TEST_OUT, OPENFILL_TEST_IMAGE  as in gimp_batch_fill.py
    OPENFILL_TEST_DOWNLOAD_ID   catalog id to download (default mat-places512)
    OPENFILL_TEST_NODES_ID      catalog id whose nodes to install (default flux1-fill-dev-gguf-q4)
    OPENFILL_TEST_SETTINGS      "1" to open the Settings dialog on the Models tab at the end
                          (auto-closes after OPENFILL_TEST_AUTOCLOSE seconds, default 8)
"""

import hashlib
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
OUT = os.environ.get("OPENFILL_TEST_OUT", os.path.join(REPO, "docs", "evidence", "models"))
DOWNLOAD_ID = os.environ.get("OPENFILL_TEST_DOWNLOAD_ID", "mat-places512")
NODES_ID = os.environ.get("OPENFILL_TEST_NODES_ID", "flux1-fill-dev-gguf-q4")
SELECTION = (420, 300, 260, 200)
os.makedirs(OUT, exist_ok=True)

report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "gimp_pid": os.getpid(), "steps": []}


def step(name, **info):
    info["time"] = time.strftime("%H:%M:%S")
    report["steps"].append({"step": name, **info})
    print(f"[sb-models] {name}: {json.dumps(info, default=str)[:300]}")
    with open(os.path.join(OUT, "batch-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)


def describe_layers(image):
    return [
        {"name": layer.get_name(), "has_mask": layer.get_mask() is not None, "visible": layer.get_visible()}
        for layer in image.get_layers()
    ]


def run_fill(image, prompt, model):
    proc = Gimp.get_pdb().lookup_procedure("python-fu-openfill-generative-fill")
    config = proc.create_config()
    config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    config.set_property("image", image)
    config.set_core_object_array("drawables", image.get_layers())
    config.set_property("prompt", prompt)
    config.set_property("negative", "blurry")
    config.set_property("variations", 1)
    config.set_property("strength", 1.0)
    config.set_property("seed", 4242)
    config.set_property("model", model)
    return proc.run(config).index(0)


def wait_task(app, task_id, timeout=900):
    samples = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        status = app.task_status()
        cur = status.get("current") or {}
        if cur.get("task_id") == task_id:
            samples.append(
                {
                    "t": round(time.time() - t0, 1),
                    "done": cur.get("done"),
                    "total": cur.get("total"),
                    "speed_mbps": round((cur.get("speed") or 0) / 1048576, 2),
                    "state": cur.get("state"),
                    "file": cur.get("file"),
                }
            )
        for h in status.get("history") or []:
            if h.get("task_id") == task_id:
                return h, samples
        time.sleep(2)
    return {"state": "timeout"}, samples


try:
    from openfill import catalog as cat
    from openfill.app import App

    app = App()
    rows = app.model_rows()
    step(
        "catalog",
        ids=[r["id"] for r in rows],
        statuses={r["id"]: r["status"] for r in rows},
        active=app.settings.active_model,
        models_root=str(app.models_root),
    )

    entry = cat.by_id(app.catalog, DOWNLOAD_ID)
    for f in entry.files:  # start clean so the download really happens
        p = app.models_root / f.dest
        if p.exists():
            p.unlink()
    ids = app.queue_download(DOWNLOAD_ID)
    step("download-queued", task_ids=ids, worker_alive=app.server.task_worker_alive())
    result, samples = wait_task(app, ids[-1])
    step("download-finished", result=result, samples=samples[:3] + samples[-3:], sample_count=len(samples))
    f = entry.files[0]
    path = app.models_root / f.dest
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    step(
        "download-verified",
        exists=path.exists(),
        size=path.stat().st_size if path.exists() else None,
        expected_size=f.size,
        sha256=digest,
        expected_sha256=f.sha256,
        match=digest == f.sha256,
    )
    status_now = {r["id"]: r["status"] for r in app.model_rows()}
    step("status-after-download", status=status_now.get(DOWNLOAD_ID), all=status_now)

    # duplicate download must be detected as already installed (no bytes transferred)
    ids2 = app.queue_download(DOWNLOAD_ID)
    result2, _ = wait_task(app, ids2[-1], timeout=120)
    step("download-again", result=result2)

    app.set_active(DOWNLOAD_ID)
    app2 = App()
    entry_, spec = app2.resolve_model("active")
    step(
        "set-active", active=app2.settings.active_model, template=spec.effective_template, prompt_free=spec.prompt_free
    )

    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(IMAGE))
    Gimp.Display.new(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *SELECTION)
    t0 = time.time()
    status = run_fill(image, "", "active")
    step("removal-fill", status=str(status), seconds=round(time.time() - t0, 1), layers=describe_layers(image))
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(os.path.join(OUT, "01-removal-mat.png")))

    nodes = app.nodes_to_install(NODES_ID)
    step("nodes-needed", nodes=[(n.folder, n.commit) for n in nodes])
    if nodes:
        tid = app.queue_node_install(NODES_ID)
        result3, _ = wait_task(app, tid, timeout=900)
        step("nodes-installed", result=result3, folder_exists=(app.custom_nodes_dir / nodes[0].folder).is_dir())
        from openfill.comfy_api import ComfyClient

        client = ComfyClient(app.server.url())
        seen = False
        for _ in range(120):
            if client.is_alive(2.0) and client.object_info("UnetLoaderGGUF"):
                seen = True
                break
            time.sleep(2)
        step("backend-restarted-with-gguf-nodes", unet_loader_gguf_present=seen)

    app.set_active("sd15-inpainting")
    t0 = time.time()
    status = run_fill(image, "a large grey boulder on grass, photo", "active")
    step("sd15-fill-after", status=str(status), seconds=round(time.time() - t0, 1), layers=describe_layers(image))
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(os.path.join(OUT, "02-sd15-fill.png")))

    final = {r["id"]: r["status"] for r in App().model_rows(App().server_model_files())}
    step("status-final", all=final)

    if os.environ.get("OPENFILL_TEST_SETTINGS") == "1":
        os.environ["OPENFILL_SETTINGS_TAB"] = "models"
        os.environ.setdefault("OPENFILL_TEST_AUTOCLOSE", "8")
        with open(os.path.join(OUT, "dialog-open.marker"), "w") as fh:
            fh.write(str(time.time()))
        proc = Gimp.get_pdb().lookup_procedure("python-fu-openfill-settings")
        config = proc.create_config()
        config.set_property("run-mode", Gimp.RunMode.INTERACTIVE)
        res = proc.run(config)
        step("settings-dialog", status=str(res.index(0)))
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
