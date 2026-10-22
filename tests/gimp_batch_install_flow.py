# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Fresh-PC walk-through, executed INSIDE a GUI GIMP 3.2 with an empty profile
(python-fu-eval batch interpreter). Not a unit test.

It runs the real interactive Generative Fill procedure on a photo while no
ComfyUI is configured: the "ComfyUI is required" notice appears, the test hook
``setup_action`` answers Download ComfyUI (no synthetic mouse input: the PC may
be in use), the setup task runs in the supervisor, the
model manager opens and the starting models download (the test hook
``models_starter_button`` presses the one button for both), then a real fill and a real
removal run. Every window is captured as it appears.

Environment (set before GIMP starts): GIMP3_DIRECTORY, OPENFILL_TEST_UI_FILE
(hooks JSON), OPENFILL_INSTALL_DIR (where ComfyUI goes), OPENFILL_NO_FIXED_ROOTS=1,
OPENFILL_INSTALL_OUT (raw output folder), OPENFILL_PHOTOS.
"""

import ast
import json
import os
import subprocess
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio  # noqa: E402

REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
OUT = os.environ["OPENFILL_INSTALL_OUT"]
HOOKS = os.environ["OPENFILL_TEST_UI_FILE"]
PHOTOS = os.environ.get("OPENFILL_PHOTOS", r"C:\AI\overnight-scratch\photos")
GIMP_PID = os.getppid()
PROFILE = Gimp.directory()
LOG_PATH = os.path.join(PROFILE, "openfill", "openfill.log")
POWERSHELL = os.path.join(
    os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
)
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
os.makedirs(OUT, exist_ok=True)
T0 = time.time()
report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "gimp_pid": GIMP_PID, "profile": PROFILE, "steps": []}

with open(os.path.join(REPO, "tests", "gimp_batch_ui_screenshots.py"), encoding="utf-8") as _fh:
    _src = _fh.read()
CAPTURE_PS1 = next(
    n.value.value
    for n in ast.parse(_src).body
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "CAPTURE_PS1"
)


def stamp(name, **extra):
    entry = {"step": name, "t": round(time.time() - T0, 1), **extra}
    report["steps"].append(entry)
    print(f"[install-flow] {json.dumps(entry, default=str)[:300]}")
    return entry


def write(name, text):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def ps(script, args, wait=False):
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(OUT, script), *args]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=FLAGS)


write("capture-window.ps1", CAPTURE_PS1)


def capture(title, name, delays_ms, timeout_s=1800):
    args = ["-Title", title, "-Out", os.path.join(OUT, name), "-ParentPid", str(GIMP_PID), "-DelaysMs", delays_ms]
    args += ["-TimeoutSec", str(timeout_s)]
    return ps("capture-window.ps1", args)


def finish(proc, timeout=30):
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = "capture timed out"
    return (out or "").strip()


def set_hooks(**hooks):
    with open(HOOKS, "w", encoding="utf-8") as fh:
        json.dump(hooks, fh)


def export_png(image, path):
    dup = image.duplicate()
    try:
        dup.flatten()
        longest = max(dup.get_width(), dup.get_height())
        if longest > 1024:
            dup.scale(round(dup.get_width() * 1024 / longest), round(dup.get_height() * 1024 / longest))
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, dup, Gio.File.new_for_path(path))
    finally:
        dup.delete()


def run_interactive(proc_name, image, props, hooks):
    set_hooks(**hooks)
    proc = Gimp.get_pdb().lookup_procedure(proc_name)
    cfg = proc.create_config()
    cfg.set_property("run-mode", Gimp.RunMode.INTERACTIVE)
    cfg.set_property("image", image)
    cfg.set_core_object_array("drawables", image.get_layers()[:1])
    for key, value in props.items():
        cfg.set_property(key, value)
    t0 = time.time()
    result = proc.run(cfg)
    return {"status": str(result.index(0)), "seconds": round(time.time() - t0, 1)}


def load(name):
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(os.path.join(PHOTOS, name)))
    Gimp.Display.new(image)
    while not image.undo_is_enabled():
        if not image.undo_enable():
            break
    return image


FILL = "python-fu-openfill-generative-fill"
caps = []
try:
    dump = os.path.join(OUT, "notice-buttons.json")
    if os.path.exists(dump):
        os.remove(dump)
    image = load("landscape.jpg")
    sel = [420, 90, 480, 300]  # open sky left of the barn
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *sel)
    Gimp.displays_flush()
    time.sleep(2)
    export_png(image, os.path.join(OUT, "fill-before.png"))
    stamp("photo open", selection=sel)

    caps.append(("notice", capture("ComfyUI is required", "01-notice.png", "1200")))
    caps.append(
        (
            "setup progress",
            capture(
                "OpenFill - Setting up ComfyUI",
                "02-setup-progress.png",
                "6000,30000,60000,90000,130000,180000,240000,300000,400000,500000",
            ),
        )
    )
    caps.append(("model manager", capture("AI models", "03-model-manager.png", "2500,9000")))
    caps.append(
        ("model downloads", capture("OpenFill - Model downloads", "04-model-downloads.png", "5000,20000,40000"))
    )
    caps.append(("fill dialog", capture("Generative Fill", "05-fill-dialog.png", "900")))
    caps.append(("fill progress", capture("OpenFill - Generative Fill", "06-fill-progress.png", "20000,60000,90000")))
    log_start = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0
    time.sleep(8)  # the capture helpers compile their user32 bindings first
    stamp("flow started")
    result = run_interactive(
        FILL,
        image,
        {"prompt": "", "negative": "", "variations": 1, "seed": 1234},
        {
            "setup_dump": dump,
            "setup_action": "download",
            "setup_delay": 4,
            "models_starter_button": True,
            "type": "hot air balloon",
            "press": "Return",
            "values": {"variations": 1, "seed": 1234, "remove": False},
            "autoclose": 1500,
        },
    )
    stamp("fill procedure returned", **result)
    for name, proc in caps:
        report.setdefault("captures", {})[name] = finish(proc, 5)
    Gimp.displays_flush()
    time.sleep(1)
    export_png(image, os.path.join(OUT, "fill-after.png"))
    report["fill_layers"] = [layer.get_name() for layer in image.get_layers()]

    # one removal on the product photo (the cup handle)
    prod = load("product.jpg")
    prod.select_rectangle(Gimp.ChannelOps.REPLACE, 155, 335, 215, 185)
    Gimp.displays_flush()
    time.sleep(2)
    export_png(prod, os.path.join(OUT, "remove-before.png"))
    cap_remove = capture("Generative Fill", "07-remove-dialog.png", "1500")
    time.sleep(6)
    result = run_interactive(
        FILL,
        prod,
        {"prompt": "", "negative": "", "variations": 1, "seed": 1234},
        {"mode": "remove", "click": "generate", "autoclose": 600},
    )
    stamp("remove procedure returned", **result)
    report["capture_remove"] = finish(cap_remove, 10)
    Gimp.displays_flush()
    time.sleep(1)
    export_png(prod, os.path.join(OUT, "remove-after.png"))
    report["remove_layers"] = [layer.get_name() for layer in prod.get_layers()]
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(log_start)
            report["plugin_log"] = fh.read().decode("utf-8", errors="replace")[-6000:]
    except OSError as err:
        report["plugin_log"] = f"(log unavailable: {err})"
except Exception as err:  # noqa: BLE001
    report["error"] = f"{type(err).__name__}: {err}"
    report["traceback"] = traceback.format_exc()
finally:
    write("report.json", json.dumps(report, indent=2, default=str))
    print("[install-flow] done")
