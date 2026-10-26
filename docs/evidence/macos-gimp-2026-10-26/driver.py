# Runs inside GIMP (python-fu-eval): every OpenFill operation on macOS, non-interactively.
import json
import os
import sys
import time
import traceback

from gi.repository import Gimp, Gio

REPO = os.environ["OPENFILL_REPO"]
OUT = os.environ["OPENFILL_TEST_OUT"]
IMAGE = os.path.join(REPO, "tests", "data", "test-scene.png")
os.makedirs(OUT, exist_ok=True)
report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "gimp_pid": os.getppid(), "cases": []}


def save_report():
    with open(os.path.join(OUT, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)


def layers(image):
    out = []
    for layer in image.get_layers():
        out.append({"name": layer.get_name(), "mask": layer.get_mask() is not None, "group": layer.is_group_layer()})
        if layer.is_group_layer():
            out += [{"name": "  " + c.get_name()} for c in layer.get_children()]
    return out


def run(case, name, image, **args):
    proc = Gimp.get_pdb().lookup_procedure(name)
    config = proc.create_config()
    config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    config.set_property("image", image)
    config.set_core_object_array("drawables", image.get_layers())
    for key, value in args.items():
        config.set_property(key, value)
    before = len(image.get_layers())
    t0 = time.time()
    try:
        result = proc.run(config)
        status = str(result.index(0))
        error = ""
        if result.length() > 1 and result.index(1) is not None:
            error = str(getattr(result.index(1), "message", result.index(1)))
    except Exception as err:  # noqa: BLE001
        status, error = "EXCEPTION", repr(err)
    entry = {
        "case": case,
        "procedure": name,
        "args": {k: v for k, v in args.items()},
        "status": status,
        "error": error,
        "seconds": round(time.time() - t0, 1),
        "layers_before": before,
        "layers": layers(image),
        "size": [image.get_width(), image.get_height()],
    }
    report["cases"].append(entry)
    print(f"[mac-e2e] {case}: {status} {entry['seconds']}s {error[:200]}", flush=True)
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(os.path.join(OUT, f"{case}.png")))
    save_report()
    return entry


try:
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(IMAGE))
    image.scale(512, 384)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 210, 150, 130, 100)
    run("fill", "python-fu-openfill-generative-fill", image, prompt="a large grey boulder on grass, photo", variations=1, seed=12345, model="active")
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 380, 30, 70, 70)
    run("remove", "python-fu-openfill-generative-fill", image, prompt="", variations=1, seed=1, model="active", remove=True)
    Gimp.Selection.none(image)
    run("expand", "python-fu-openfill-generative-expand", image, prompt="", variations=1, left=0, top=0, right=64, bottom=0, seed=7, model="active")
    run("create", "python-fu-openfill-text-to-image", image, prompt="a small wooden cabin in a meadow, photo", variations=1, width=512, height=384, target="layer", seed=3, model="active")
    report["ok"] = all("SUCCESS" in case["status"] for case in report["cases"])
except Exception:
    report["ok"] = False
    report["error"] = traceback.format_exc()
finally:
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_report()
