# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Performance driver, executed INSIDE GIMP by the python-fu-eval batch
interpreter (not a unit test). It builds one photo-like test image, then runs
Generative Fill non-interactively once per case, timing each PDB call and
cutting the plug-in log lines that belong to it (the backend writes
"timing:" lines with the upload, queue, sampling and download split).

    set GIMP3_DIRECTORY=<isolated test profile>
    set OPENFILL_PERF_PLAN=<plan.json>
    set OPENFILL_PERF_OUT=<output folder>
    gimp-console-3.2.exe --new-instance --no-splash --console-messages ^
        --batch-interpreter python-fu-eval ^
        -b "exec(open(r'C:\\repos\\gimp-stable-boy\\tests\\gimp_batch_perf.py').read())"

Plan format:

    {"image": "<photo path>", "size": 2048, "pause_after": 1.5,
     "keep_layers": false,
     "cases": [{"id": "sd15-512", "model": "sd15-inpainting", "sel": 512,
                "prompt": "a wooden cabin", "remove": false}, ...]}

The photo is cropped to a centred square and scaled to size x size. Each
selection is a centred square of ``sel`` px. The layers a fill adds are
deleted afterwards unless ``keep_layers`` is true, so every case starts from
the same image. Results go to <out>/perf-results.jsonl, one line per case
with epoch start and end times (an outside sampler correlates its memory
samples with them) and the log lines of that call.
"""

import json
import os
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio  # noqa: E402

PLAN = os.environ["OPENFILL_PERF_PLAN"]
OUT = os.environ["OPENFILL_PERF_OUT"]
QUIT = os.environ.get("OPENFILL_TEST_QUIT", "1") == "1"
LOG_PATH = os.path.join(Gimp.directory(), "openfill", "openfill.log")
PROC = "python-fu-openfill-generative-fill"


def log_size():
    try:
        return os.path.getsize(LOG_PATH)
    except OSError:
        return 0


def log_slice(start, end):
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(start)
            return fh.read(max(0, end - start)).decode("utf-8", errors="replace")
    except OSError as err:
        return f"(log unavailable: {err})"


def emit(record):
    with open(os.path.join(OUT, "perf-results.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def build_image(path, size):
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
    w, h = image.get_width(), image.get_height()
    side = min(w, h)
    image.crop(side, side, (w - side) // 2, (h - side) // 2)
    image.scale(size, size)
    return image


def run_fill(image, case):
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(PROC)
    cfg = proc.create_config()
    cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    cfg.set_property("image", image)
    cfg.set_core_object_array("drawables", [image.get_layers()[0]])
    cfg.set_property("prompt", case.get("prompt", "a wooden cabin"))
    cfg.set_property("negative", "blurry, low quality")
    cfg.set_property("variations", 1)
    cfg.set_property("seed", int(case.get("seed", 1234)))
    cfg.set_property("model", case["model"])
    cfg.set_property("remove", bool(case.get("remove", False)))
    result = proc.run(cfg)
    status = result.index(0)
    message = ""
    try:
        if result.length() > 1:
            message = str(result.index(1) or "")
    except Exception:  # noqa: BLE001
        pass
    if not message:
        try:
            message = pdb.get_last_error() or ""
        except Exception:  # noqa: BLE001
            pass
    return str(status.value_nick if hasattr(status, "value_nick") else status), message


def main():
    with open(PLAN, encoding="utf-8") as fh:
        plan = json.load(fh)
    os.makedirs(OUT, exist_ok=True)
    size = int(plan.get("size", 2048))
    t_build = time.time()
    image = build_image(plan["image"], size)
    emit({"event": "image", "t": time.time(), "build_s": round(time.time() - t_build, 3), "size": size})
    base_ids = {layer.get_id() for layer in image.get_layers()}
    for case in plan["cases"]:
        sel = int(case["sel"])
        off = (size - sel) // 2
        image.select_rectangle(Gimp.ChannelOps.REPLACE, off, off, sel, sel)
        start_log = log_size()
        t0 = time.time()
        p0 = time.perf_counter()
        try:
            status, message = run_fill(image, case)
        except Exception as err:  # noqa: BLE001
            status, message = "driver-error", repr(err)
        wall = time.perf_counter() - p0
        t1 = time.time()
        added = [layer for layer in image.get_layers() if layer.get_id() not in base_ids]
        record = {
            "event": "fill",
            "id": case["id"],
            "model": case["model"],
            "sel": sel,
            "remove": bool(case.get("remove", False)),
            "t_start": t0,
            "t_end": t1,
            "wall_s": round(wall, 3),
            "status": status,
            "message": message,
            "layers_added": len(added),
            "log": log_slice(start_log, log_size()),
        }
        if not plan.get("keep_layers"):
            for layer in added:
                image.remove_layer(layer)
        emit(record)
        time.sleep(float(plan.get("pause_after", 1.5)))  # lets the outside sampler take a reading


try:
    main()
except Exception:  # noqa: BLE001
    traceback.print_exc()
    emit({"event": "driver-error", "t": time.time(), "trace": traceback.format_exc()})
finally:
    emit({"event": "quit", "t": time.time()})
    if QUIT:
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
