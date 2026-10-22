# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check executed INSIDE a running GIMP by the python-fu-eval batch
interpreter. It is not a unit test; it drives the registered procedure
non-interactively and records evidence.

    gimp-3.2.exe --new-instance --no-splash --console-messages ^
        --batch-interpreter python-fu-eval ^
        -b "exec(open(r'C:\\repos\\gimp-stable-boy\\tests\\gimp_batch_fill.py').read())"

(Keep single quotes inside the -b string; shells strip the double ones.)

Environment variables (all optional):
    OPENFILL_REPO          repository root (default: C:\\repos\\gimp-stable-boy)
    OPENFILL_TEST_IMAGE    input image (default: tests/data/test-scene.png)
    OPENFILL_TEST_OUT      output directory (default: docs/evidence)
    OPENFILL_TEST_PROMPT   prompt (default: "a large grey boulder on grass")
    OPENFILL_TEST_QUIT     "1" to quit GIMP at the end (default "1")
    OPENFILL_TEST_EXPAND   "1" to also run Generative Expand afterwards
"""

import json
import os
import sys
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio

# python-fu-eval runs this with exec(), so __file__ is not ours: the repo root
# comes from OPENFILL_REPO or the default checkout location.
REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
IMAGE = os.environ.get("OPENFILL_TEST_IMAGE", os.path.join(REPO, "tests", "data", "test-scene.png"))
OUT = os.environ.get("OPENFILL_TEST_OUT", os.path.join(REPO, "docs", "evidence"))
PROMPT = os.environ.get("OPENFILL_TEST_PROMPT", "a large grey boulder on grass, photo")
QUIT = os.environ.get("OPENFILL_TEST_QUIT", "1") == "1"
EXPAND = os.environ.get("OPENFILL_TEST_EXPAND", "0") == "1"
SELECTION = (420, 300, 260, 200)  # x, y, w, h : the dark box in the test scene

report = {
    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    "gimp_pid": os.getpid(),
    "gimp_version": f"{Gimp.MAJOR_VERSION}.{Gimp.MINOR_VERSION}.{Gimp.MICRO_VERSION}",
    "image": IMAGE,
    "prompt": PROMPT,
    "steps": [],
}
os.makedirs(OUT, exist_ok=True)


def step(name, **info):
    entry = {"step": name, "time": time.strftime("%H:%M:%S"), **info}
    report["steps"].append(entry)
    print(f"[sb-test] {name}: {info}")


def export(image, path):
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(path))


def describe_layers(image):
    out = []
    for layer in image.get_layers():
        mask = layer.get_mask()
        ox, oy = layer.get_offsets()[-2:]
        out.append(
            {
                "name": layer.get_name(),
                "size": [layer.get_width(), layer.get_height()],
                "offset": [ox, oy],
                "visible": layer.get_visible(),
                "has_mask": mask is not None,
                "is_group": layer.is_group_layer(),
            }
        )
        if layer.is_group_layer():
            for child in layer.get_children():
                m = child.get_mask()
                out.append({"name": "  " + child.get_name(), "has_mask": m is not None, "visible": child.get_visible()})
    return out


def run_procedure(name, image, drawables, **args):
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(name)
    if proc is None:
        raise RuntimeError(f"{name} is not registered")
    config = proc.create_config()
    config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    config.set_property("image", image)
    config.set_core_object_array("drawables", drawables)
    for key, value in args.items():
        config.set_property(key, value)
    result = proc.run(config)
    status = result.index(0)
    return status


try:
    pdb = Gimp.get_pdb()
    step(
        "pdb-lookup",
        registered={
            n: pdb.procedure_exists(n)
            for n in (
                "python-fu-openfill-generative-fill",
                "python-fu-openfill-generative-expand",
                "python-fu-openfill-text-to-image",
                "python-fu-openfill-settings",
                "python-fu-openfill-stop-backend",
            )
        },
    )
    proc = pdb.lookup_procedure("python-fu-openfill-generative-fill")
    step("menu", label=proc.get_menu_label(), paths=list(proc.get_menu_paths()))

    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(IMAGE))
    display = Gimp.Display.new(image)
    step(
        "opened",
        size=[image.get_width(), image.get_height()],
        layers=describe_layers(image),
        display_id=display.get_id(),
    )
    export(image, os.path.join(OUT, "01-before.png"))

    x, y, w, h = SELECTION
    image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, w, h)
    step("selected", rect=SELECTION, empty=Gimp.Selection.is_empty(image))

    t0 = time.time()
    status = run_procedure(
        "python-fu-openfill-generative-fill",
        image,
        image.get_layers(),
        prompt=PROMPT,
        negative="blurry, text, watermark",
        variations=1,
        strength=1.0,
        seed=12345,
        model=os.environ.get("OPENFILL_TEST_MODEL", "fast"),
    )
    step("generative-fill", status=str(status), seconds=round(time.time() - t0, 1), layers=describe_layers(image))
    export(image, os.path.join(OUT, "02-after-fill.png"))
    export(image, os.path.join(OUT, "02-after-fill.xcf"))

    if EXPAND:
        t0 = time.time()
        status = run_procedure(
            "python-fu-openfill-generative-expand",
            image,
            image.get_layers(),
            prompt="grass field under a blue sky, photo",
            negative="blurry",
            variations=1,
            left=0,
            top=0,
            right=256,
            bottom=0,
            seed=7,
            model=os.environ.get("OPENFILL_TEST_MODEL", "fast"),
        )
        step(
            "generative-expand",
            status=str(status),
            seconds=round(time.time() - t0, 1),
            size=[image.get_width(), image.get_height()],
            layers=describe_layers(image),
        )
        export(image, os.path.join(OUT, "03-after-expand.png"))

    if os.environ.get("OPENFILL_TEST_VARIATIONS", "0") == "1":
        image.select_rectangle(Gimp.ChannelOps.REPLACE, 760, 60, 140, 140)  # the sun
        t0 = time.time()
        status = run_procedure(
            "python-fu-openfill-generative-fill",
            image,
            image.get_layers(),
            prompt="a hot air balloon in the sky, photo",
            negative="blurry",
            variations=3,
            strength=1.0,
            seed=99,
            model=os.environ.get("OPENFILL_TEST_MODEL", "fast"),
        )
        step(
            "generative-fill-variations",
            status=str(status),
            seconds=round(time.time() - t0, 1),
            layers=describe_layers(image),
        )
        export(image, os.path.join(OUT, "04-after-variations.png"))

    if os.environ.get("OPENFILL_TEST_TXT2IMG", "0") == "1":
        Gimp.Selection.none(image)
        t0 = time.time()
        status = run_procedure(
            "python-fu-openfill-text-to-image",
            image,
            image.get_layers(),
            prompt="a small wooden cabin in a meadow, photo",
            negative="blurry",
            variations=1,
            width=512,
            height=384,
            target="layer",
            seed=3,
            model=os.environ.get("OPENFILL_TEST_MODEL", "fast"),
        )
        step(
            "text-to-image-layer", status=str(status), seconds=round(time.time() - t0, 1), layers=describe_layers(image)
        )
        export(image, os.path.join(OUT, "05-after-txt2img.png"))

    if os.environ.get("OPENFILL_TEST_SDXL", "0") == "1":
        image.select_rectangle(Gimp.ChannelOps.REPLACE, 80, 480, 300, 220)
        t0 = time.time()
        status = run_procedure(
            "python-fu-openfill-generative-fill",
            image,
            image.get_layers(),
            prompt="a wooden picnic table on grass, photo",
            negative="blurry",
            variations=1,
            strength=1.0,
            seed=5,
            model="quality",
        )
        step(
            "generative-fill-sdxl",
            status=str(status),
            seconds=round(time.time() - t0, 1),
            layers=describe_layers(image),
        )
        export(image, os.path.join(OUT, "06-after-sdxl.png"))

    idle_wait = int(os.environ.get("OPENFILL_TEST_IDLE_WAIT", "0"))
    if idle_wait:
        step("idle-wait-start", seconds=idle_wait)
        time.sleep(idle_wait)
        step("idle-wait-end")

    Gimp.displays_flush()
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["ok"] = True
except Exception as err:  # noqa: BLE001
    report["ok"] = False
    report["error"] = f"{type(err).__name__}: {err}"
    report["traceback"] = traceback.format_exc()
    print(report["traceback"], file=sys.stderr)
finally:
    with open(os.path.join(OUT, "batch-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    if QUIT:
        quit_proc = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = quit_proc.create_config()
        cfg.set_property("force", True)
        quit_proc.run(cfg)
