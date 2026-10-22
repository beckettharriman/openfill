# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Robustness driver, executed INSIDE GIMP by the python-fu-eval batch
interpreter (not a unit test). One case per GIMP start: the launcher prepares
the scenario (config.json, listeners on the port, ...), this script builds a
small synthetic image with a selection, calls Generative Fill
non-interactively and records what the user would be left with.

    set GIMP3_DIRECTORY=<isolated robustness profile>
    set OPENFILL_ROB_CASE=rob-backend-not-installed
    set OPENFILL_ROB_OUT=<evidence folder for the case>
    gimp-console-3.2.exe --new-instance --no-splash --console-messages ^
        --batch-interpreter python-fu-eval ^
        -b "exec(open(r'C:\\repos\\gimp-stable-boy\\tests\\gimp_batch_robustness.py').read())"

The messages the plug-in shows with Gimp.message go to GIMP's stderr in
console mode; the launcher captures that stream and merges it into
report.json together with the process state after GIMP has exited.

Outputs in OPENFILL_ROB_OUT: gimp-report.json (this script's part), before.png,
after.png and plugin-log.txt (the openfill.log lines written during the
case).
"""

import hashlib
import json
import os
import sys
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gegl, Gimp, Gio  # noqa: E402

CASE = os.environ.get("OPENFILL_ROB_CASE", "rob-unnamed")
OUT = os.environ["OPENFILL_ROB_OUT"]
QUIT = os.environ.get("OPENFILL_TEST_QUIT", "1") == "1"
PROC = os.environ.get("OPENFILL_ROB_PROC", "python-fu-openfill-generative-fill")
WIDTH = int(os.environ.get("OPENFILL_ROB_W", "512"))
HEIGHT = int(os.environ.get("OPENFILL_ROB_H", "384"))
SELECTION = tuple(int(v) for v in os.environ.get("OPENFILL_ROB_SEL", "200,140,120,100").split(","))  # x, y, w, h
VARIATIONS = int(os.environ.get("OPENFILL_ROB_VARIATIONS", "1"))
HOLD = float(os.environ.get("OPENFILL_ROB_HOLD", "0"))  # seconds to stay open after the call (idle timeout)
LOG_PATH = os.path.join(Gimp.directory(), "openfill", "openfill.log")

report = {
    "case": CASE,
    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    "gimp_pid": os.getpid(),
    "gimp_version": f"{Gimp.MAJOR_VERSION}.{Gimp.MINOR_VERSION}.{Gimp.MICRO_VERSION}",
    "profile": Gimp.directory(),
    "procedure": PROC,
}


def log_size():
    try:
        return os.path.getsize(LOG_PATH)
    except OSError:
        return 0


def log_slice(start):
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(start)
            return fh.read().decode("utf-8", errors="replace")
    except OSError as err:
        return f"(log unavailable: {err})"


def build_image():
    """Gradient plus four hard-edged boxes, made with GIMP operations only."""
    image = Gimp.Image.new(WIDTH, HEIGHT, Gimp.ImageBaseType.RGB)
    layer = Gimp.Layer.new(image, "Background", WIDTH, HEIGHT, Gimp.ImageType.RGB_IMAGE, 100.0, Gimp.LayerMode.NORMAL)
    image.insert_layer(layer, None, 0)
    Gimp.context_push()
    try:
        Gimp.context_set_foreground(Gegl.Color.new("#4a7fc0"))
        Gimp.context_set_background(Gegl.Color.new("#e8d9a8"))
        Gimp.context_set_gradient_fg_bg_rgb()
        layer.edit_gradient_fill(Gimp.GradientType.LINEAR, 0.0, False, 1, 0.0, True, 0, 0, 0, HEIGHT)
        for i, color in enumerate(("#2e5d2a", "#7a3b1d", "#d0d0d0", "#202020")):
            Gimp.context_set_foreground(Gegl.Color.new(color))
            image.select_rectangle(
                Gimp.ChannelOps.REPLACE,
                int(WIDTH * (0.08 + 0.22 * i)),
                int(HEIGHT * 0.55),
                int(WIDTH * 0.15),
                int(HEIGHT * 0.3),
            )
            layer.edit_fill(Gimp.FillType.FOREGROUND)
        Gimp.Selection.none(image)
    finally:
        Gimp.context_pop()
    return image


def read_bytes(drawable, fmt, rect):
    buf = drawable.get_buffer()
    return bytes(buf.get(Gegl.Rectangle.new(*rect), 1.0, fmt, Gegl.AbyssPolicy.NONE))


def composite_md5(image):
    layer = Gimp.Layer.new_from_visible(image, image, "rob composite")
    try:
        return hashlib.md5(read_bytes(layer, "R'G'B'A u8", (0, 0, image.get_width(), image.get_height()))).hexdigest()
    finally:
        layer.delete()


def selection_state(image):
    bounds = tuple(Gimp.Selection.bounds(image))[-5:]
    data = read_bytes(image.get_selection(), "Y u8", (0, 0, image.get_width(), image.get_height()))
    return {
        "empty": bool(Gimp.Selection.is_empty(image)),
        "bounds": [bounds[1], bounds[2], bounds[3] - bounds[1], bounds[4] - bounds[2]] if bounds[0] else None,
        "md5": hashlib.md5(data).hexdigest(),
    }


def layer_names(image):
    out = []
    for layer in image.get_layers():
        out.append(layer.get_name())
        if layer.is_group_layer():
            out += ["  " + c.get_name() for c in layer.get_children()]
    return out


def image_state(image):
    return {
        "size": [image.get_width(), image.get_height()],
        "layers": layer_names(image),
        "layer_count": len(layer_names(image)),
        "channels": [c.get_name() for c in image.get_channels()],
        "selection": selection_state(image),
        "dirty": bool(image.is_dirty()),
        "undo_enabled": bool(image.undo_is_enabled()),
        "composite_md5": composite_md5(image),
    }


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


def run_procedure(image, drawables, args):
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(PROC)
    if proc is None:
        raise RuntimeError(f"{PROC} is not registered")
    cfg = proc.create_config()
    cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    cfg.set_property("image", image)
    cfg.set_core_object_array("drawables", drawables)
    for key, value in args.items():
        cfg.set_property(key, value)
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
    return (status.value_nick if hasattr(status, "value_nick") else str(status)), message


def main():
    os.makedirs(OUT, exist_ok=True)
    log0 = log_size()
    image = build_image()
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *SELECTION)
    image.clean_all()  # dirty afterwards = something undoable happened during the call
    report["before"] = image_state(image)
    export_png(image, os.path.join(OUT, "before.png"))
    args = {
        "prompt": os.environ.get("OPENFILL_ROB_PROMPT", "a red brick wall, photo"),
        "negative": "blurry",
        "variations": VARIATIONS,
        "strength": 1.0,
        "seed": 1234,
        "model": os.environ.get("OPENFILL_ROB_MODEL", "fast"),
    }
    report["args"] = args
    if os.environ.get("OPENFILL_ROB_WARMUP") == "1":
        # first fill starts the backend; the case's call then runs against a warm server
        t0 = time.monotonic()
        report["warmup"] = dict(zip(("status", "message"), run_procedure(image, image.get_layers(), args), strict=True))
        report["warmup"]["seconds"] = round(time.monotonic() - t0, 2)
        for layer in image.get_layers()[:-1]:  # drop what the warm-up added, keep the background
            image.remove_layer(layer)
        image.select_rectangle(Gimp.ChannelOps.REPLACE, *SELECTION)
        image.clean_all()
        report["before"] = image_state(image)
        go = os.path.join(OUT, "go.txt")
        with open(os.path.join(OUT, "warm-done.txt"), "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
        if os.environ.get("OPENFILL_ROB_WAIT_GO") == "1":
            t1 = time.time()
            while not os.path.exists(go) and time.time() - t1 < 120:
                time.sleep(0.5)
    step = os.environ.get("OPENFILL_ROB_CANCEL_STEP")
    if step and os.environ.get("OPENFILL_TEST_CANCEL_FILE"):
        with open(os.environ["OPENFILL_TEST_CANCEL_FILE"], "w", encoding="utf-8") as fh:
            fh.write(step)
    report["call_started"] = time.time()
    with open(os.path.join(OUT, "call-started.txt"), "w", encoding="utf-8") as fh:
        fh.write(str(report["call_started"]))
    t0 = time.monotonic()
    try:
        status, message = run_procedure(image, image.get_layers(), args)
        report["status"], report["return_message"] = status, message
    except Exception as err:  # noqa: BLE001 - recorded
        report["status"] = "driver-exception"
        report["return_message"] = f"{type(err).__name__}: {err}"
        report["driver_traceback"] = traceback.format_exc()
    report["seconds"] = round(time.monotonic() - t0, 2)
    report["call_finished"] = time.time()
    report["after"] = image_state(image)
    b, a = report["before"], report["after"]
    report["image_untouched"] = (
        a["layer_count"] == b["layer_count"]
        and a["size"] == b["size"]
        and a["selection"] == b["selection"]
        and a["composite_md5"] == b["composite_md5"]
        and not a["dirty"]
    )
    export_png(image, os.path.join(OUT, "after.png"))
    with open(os.path.join(OUT, "plugin-log.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(log_slice(log0).replace("\r\n", "\n"))
    image.delete()
    if HOLD:
        report["hold_seconds"] = HOLD
        time.sleep(HOLD)


try:
    main()
except Exception as err:  # noqa: BLE001
    report["driver_error"] = f"{type(err).__name__}: {err}"
    report["traceback"] = traceback.format_exc()
    print(report["traceback"], file=sys.stderr)
finally:
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(os.path.join(OUT, "gimp-report.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, indent=2, default=str)
    except OSError:
        pass
    print(f"[sb-rob] {CASE}: {report.get('status')} {report.get('seconds')}s", flush=True)
    if QUIT:
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
