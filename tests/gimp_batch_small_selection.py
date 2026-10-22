# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Inside GIMP: small-selection removal test. For each model id, open the
image, select an ellipse, run Generative Fill with an EMPTY prompt (removal
intent), export the full result and a crop around the selection.

Environment:
    OPENFILL_SS_IMAGE   image path (default tests/data/fabric-button.png)
    OPENFILL_SS_SEL     "x,y,w,h" ellipse selection (default around the synthetic button)
    OPENFILL_SS_MODELS  comma-separated catalog ids (default sd15-inpainting,juggernaut-xl-v9,mat-places512)
    OPENFILL_SS_OUT     output folder
    OPENFILL_SS_SCALE   optional: scale the image by this factor first (e.g. 0.2)
    OPENFILL_SS_REMOVE  "1" to tick the Remove object option when the procedure has it
    OPENFILL_SS_PROMPT  prompt (default empty)
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
IMAGE = os.environ.get("OPENFILL_SS_IMAGE", os.path.join(REPO, "tests", "data", "fabric-button.png"))
SEL = tuple(int(v) for v in os.environ.get("OPENFILL_SS_SEL", "584,514,52,52").split(","))
MODELS = os.environ.get("OPENFILL_SS_MODELS", "sd15-inpainting,juggernaut-xl-v9,mat-places512").split(",")
OUT = os.environ.get("OPENFILL_SS_OUT", os.path.join(REPO, "docs", "evidence", "small-selection", "run"))
SCALE = float(os.environ.get("OPENFILL_SS_SCALE", "0") or 0)
REMOVE = os.environ.get("OPENFILL_SS_REMOVE", "0") == "1"
PROMPT = os.environ.get("OPENFILL_SS_PROMPT", "")
os.makedirs(OUT, exist_ok=True)
report = {
    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    "image": IMAGE,
    "selection": SEL,
    "scale": SCALE,
    "remove": REMOVE,
    "prompt": PROMPT,
    "runs": [],
}


def save(image, path):
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(path))


def crop_export(image, rect, path, factor=4):
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    size = max(w, h) * factor
    cw = min(image.get_width(), int(size))
    ch = min(image.get_height(), int(size))
    ox = int(max(0, min(image.get_width() - cw, cx - cw / 2)))
    oy = int(max(0, min(image.get_height() - ch, cy - ch / 2)))
    dup = image.duplicate()
    dup.crop(cw, ch, ox, oy)
    save(dup, path)
    dup.delete()


def open_image():
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(IMAGE))
    if SCALE:
        image.scale(int(image.get_width() * SCALE), int(image.get_height() * SCALE))
    return image


def _buffer_bytes(drawable, fmt):
    gi.require_version("Gegl", "0.4")
    from gi.repository import Gegl

    buf = drawable.get_buffer()
    return bytes(buf.get(buf.get_extent(), 1.0, fmt, Gegl.AbyssPolicy.NONE))


def compare_with_raw_output(image, tag):
    """Pixel-compare the newest debug dump's result PNG with the new top layer,
    and measure how opaque the layer mask is. Copies the dump next to the
    evidence so the sent crop/mask and raw output are kept."""
    import glob
    import shutil

    dumps = sorted(glob.glob(os.path.join(Gimp.directory(), "openfill", "tmp", "debug-fill-*")))
    if not dumps:
        return {"error": "no debug dump (set OPENFILL_DEBUG_DUMP=1 in the launcher environment)"}
    dump = dumps[-1]
    dest = os.path.join(OUT, f"{tag}-dump")
    shutil.copytree(dump, dest, dirs_exist_ok=True)
    layer = image.get_layers()[0]
    raw = Gimp.file_load_layer(
        Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(os.path.join(dump, "result-0.png"))
    )
    out = {
        "dump": dest,
        "layer": layer.get_name(),
        "layer_size": [layer.get_width(), layer.get_height()],
        "layer_offset": list(layer.get_offsets()[-2:]),
        "raw_size": [raw.get_width(), raw.get_height()],
    }
    if (raw.get_width(), raw.get_height()) == (layer.get_width(), layer.get_height()):
        a = _buffer_bytes(layer, "R'G'B' u8")
        b = _buffer_bytes(raw, "R'G'B' u8")
        diffs = [abs(x - y) for x, y in zip(a, b, strict=False)]
        out["content_vs_raw"] = {
            "max_abs_diff": max(diffs),
            "mean_abs_diff": round(sum(diffs) / len(diffs), 3),
            "identical": max(diffs) == 0,
        }
    raw.delete()
    mask = layer.get_mask()
    if mask is not None:
        m = _buffer_bytes(mask, "Y u8")
        w, h = mask.get_width(), mask.get_height()
        x, y, sw, sh = sel
        ox, oy = out["layer_offset"]
        cx, cy = int(x + sw / 2 - ox), int(y + sh / 2 - oy)
        center = m[cy * w + cx] if 0 <= cx < w and 0 <= cy < h else None
        out["mask"] = {
            "center_value": center,
            "max": max(m),
            "pixels_ge_250": sum(1 for v in m if v >= 250),
            "pixels_gt_0": sum(1 for v in m if v > 0),
            "selection_area": int(3.1416 * sw * sh / 4),
        }
    return out


def log_tail(n=4):
    try:
        path = os.path.join(Gimp.directory(), "openfill", "openfill.log")
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        return [line[26:240] for line in lines[-n:]]
    except OSError:
        return []


try:
    first = open_image()
    sel = SEL if not SCALE else tuple(int(v * SCALE) for v in SEL)
    crop_export(first, sel, os.path.join(OUT, "00-before-crop.png"))
    first.delete()
    proc = Gimp.get_pdb().lookup_procedure("python-fu-openfill-generative-fill")
    has_remove = proc.find_argument("remove") is not None
    for model_id in MODELS:
        image = open_image()
        Gimp.Display.new(image)
        image.select_ellipse(Gimp.ChannelOps.REPLACE, *sel)
        config = proc.create_config()
        config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
        config.set_property("image", image)
        config.set_core_object_array("drawables", image.get_layers())
        config.set_property("prompt", PROMPT)
        config.set_property("negative", "blurry")
        config.set_property("variations", 1)
        config.set_property("strength", 1.0)
        config.set_property("seed", 31337)
        config.set_property("model", model_id)
        if has_remove:
            config.set_property("remove", REMOVE)
        t0 = time.time()
        status = proc.run(config).index(0)
        entry = {
            "model": model_id,
            "status": str(status),
            "seconds": round(time.time() - t0, 1),
            "layers": [layer.get_name() for layer in image.get_layers()],
            "log": log_tail(5),
        }
        tag = model_id.split("-")[0]
        if status == 3:
            crop_export(image, sel, os.path.join(OUT, f"{tag}-after-crop.png"))
            save(image, os.path.join(OUT, f"{tag}-after-full.png"))
            entry["pixels"] = compare_with_raw_output(image, tag)
        report["runs"].append(entry)
        print("[sb-ss]", json.dumps(entry)[:300])
        with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    report["ok"] = True
except Exception as err:  # noqa: BLE001
    report["ok"] = False
    report["error"] = f"{type(err).__name__}: {err}"
    report["traceback"] = traceback.format_exc()
    print(report["traceback"], file=sys.stderr)
finally:
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    if os.environ.get("OPENFILL_TEST_QUIT", "1") == "1":
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
