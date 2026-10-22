# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Inside GIMP: verify that a node-install task restarts a running backend and
that the Settings dialog opens on the Models tab (for a screenshot).

Steps: start the backend, queue an install_nodes task for ComfyUI-GGUF
(already cloned; git checkout + pip are idempotent) with restart_backend,
wait for the task, then poll /object_info/UnetLoaderGGUF on the restarted
server. Finally open the Settings dialog (auto-closes) and quit.
"""

import json
import os
import sys
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp

REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
sys.path.insert(0, os.path.join(REPO, "gimp-openfill"))
OUT = os.environ.get("OPENFILL_TEST_OUT", os.path.join(REPO, "docs", "evidence", "models"))
os.makedirs(OUT, exist_ok=True)
report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "gimp_pid": os.getpid(), "steps": []}


def step(name, **info):
    info["time"] = time.strftime("%H:%M:%S")
    report["steps"].append({"step": name, **info})
    print(f"[sb-restart] {name}: {json.dumps(info, default=str)[:300]}")


try:
    from openfill import catalog as cat
    from openfill.app import App
    from openfill.comfy_api import ComfyClient
    from openfill.config import comfy_layout

    app = App()
    url = app.server.ensure_running(lambda f, t: None)
    client = ComfyClient(url)
    before = client.system_stats()
    state_before = app.server.read_state()
    step(
        "backend-up",
        url=url,
        server_pid=state_before.get("server_pid"),
        supervisor=state_before.get("supervisor_pid"),
        gguf_before=bool(client.object_info("UnetLoaderGGUF")),
    )

    entry = cat.by_id(app.catalog, "flux1-fill-dev-gguf-q4")
    layout = comfy_layout(app.settings.comfy_dir)
    task = {
        "type": "install_nodes",
        "model_id": entry.id,
        "custom_nodes_dir": str(app.custom_nodes_dir),
        "python": str(layout["python"]),
        "restart_backend": True,
        "nodes": [{"repo": n.repo, "folder": n.folder, "commit": n.commit} for n in entry.requires_nodes],
    }
    tid = app.server.queue_task(task)
    step("install-queued", task_id=tid, worker_alive=app.server.task_worker_alive())
    result = None
    t0 = time.time()
    while time.time() - t0 < 600:
        for h in app.task_status().get("history") or []:
            if h.get("task_id") == tid:
                result = h
                break
        if result:
            break
        time.sleep(2)
    step("install-finished", result=result, seconds=round(time.time() - t0, 1))

    restarted = False
    new_pid = None
    for _ in range(150):
        st = app.server.read_state()
        new_pid = st.get("server_pid")
        if (
            new_pid
            and new_pid != state_before.get("server_pid")
            and client.is_alive(2.0)
            and client.object_info("UnetLoaderGGUF")
        ):
            restarted = True
            break
        time.sleep(2)
    step(
        "backend-restarted",
        restarted=restarted,
        old_pid=state_before.get("server_pid"),
        new_pid=new_pid,
        gguf_after=bool(client.object_info("UnetLoaderGGUF")) if client.is_alive(2.0) else None,
    )

    os.environ["OPENFILL_SETTINGS_TAB"] = "models"
    os.environ.setdefault("OPENFILL_TEST_AUTOCLOSE", "10")
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
    with open(os.path.join(OUT, "restart-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    if os.environ.get("OPENFILL_TEST_QUIT", "1") == "1":
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
