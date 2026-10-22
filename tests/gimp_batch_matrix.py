# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Correctness matrix driver, executed INSIDE GIMP by the python-fu-eval batch
interpreter (not a unit test). It reads a JSON spec of cases, and for each case
builds the input image, applies a selection recipe, calls a registered
procedure non-interactively and records what happened. Pixel statistics are
computed afterwards by the launcher (system Python with numpy), from the raw
buffers this script dumps.

    set GIMP3_DIRECTORY=<isolated test profile>
    set OPENFILL_MATRIX_SPEC=<spec.json>
    set OPENFILL_MATRIX_OUT=<output root>
    gimp-console-3.2.exe --new-instance --no-splash --console-messages ^
        --batch-interpreter python-fu-eval ^
        -b "exec(open(r'C:\\repos\\gimp-stable-boy\\tests\\gimp_batch_matrix.py').read())"

Spec format: {"cases": [case, ...]}; a case is

    {"id": "sel-tiny-20px",
     "image": {"w": 1024, "h": 768, "source": "scene" | "new" | "<path>",
               "base": "rgb" | "gray" | "indexed",
               "precision": "u8" | "u16" | "float" | "linear",
               "scale": true,
               "setup": ["hidden_layer", "text_layer", ...]},
     "selection": {"kind": "none" | "all" | "rect" | "ellipse" | "multi" | "blob",
                   "rect": [x, y, w, h], "rects": [[...], ...], "units": "px" | "frac",
                   "feather": 0},
     "proc": "fill" | "expand" | "txt2img",
     "args": {...procedure arguments...},
     "runs": 1,
     "expect": {"status": "success" | "error", "message_contains": ["..."]}}

A "source" path loads that file (a photograph, for the photo-* cases); it is
scaled to w x h unless "scale" is false, which keeps the file's own size.

Outputs per case in <out>/<id>/: gimp-report.json and raw buffers (*.raw,
described in the report: width, height, channels, offset).
"""

import hashlib
import json
import math
import os
import sys
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gegl, Gimp, Gio  # noqa: E402

REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
SPEC = os.environ["OPENFILL_MATRIX_SPEC"]
OUT = os.environ["OPENFILL_MATRIX_OUT"]
QUIT = os.environ.get("OPENFILL_TEST_QUIT", "1") == "1"
SCENE = os.path.join(REPO, "tests", "data", "test-scene.png")
PROCS = {
    "fill": "python-fu-openfill-generative-fill",
    "expand": "python-fu-openfill-generative-expand",
    "txt2img": "python-fu-openfill-text-to-image",
}
TIMELINE = os.path.join(OUT, "timeline.jsonl")
LOG_PATH = os.path.join(Gimp.directory(), "openfill", "openfill.log")
DRAWABLES = {}  # image id -> drawables to pass (set by setup ops)


def timeline(event, case_id, **extra):
    with open(TIMELINE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "event": event, "case": case_id, **extra}) + "\n")


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


# ----------------------------------------------------------------- pixels ----


def read_buffer(drawable, fmt, rect=None):
    buf = drawable.get_buffer()
    if rect is None:
        rect = (0, 0, drawable.get_width(), drawable.get_height())
    region = Gegl.Rectangle.new(*rect)
    return bytes(buf.get(region, 1.0, fmt, Gegl.AbyssPolicy.NONE))


def write_buffer(drawable, rect, fmt, data):
    buf = drawable.get_buffer()
    buf.set(Gegl.Rectangle.new(*rect), fmt, data)
    buf.flush()
    drawable.update(*rect)


def dump_raw(case_dir, name, data, width, height, channels, offset=(0, 0)):
    path = os.path.join(case_dir, name + ".raw")
    with open(path, "wb") as fh:
        fh.write(data)
    return {
        "file": name + ".raw",
        "width": width,
        "height": height,
        "channels": channels,
        "offset": list(offset),
        "md5": hashlib.md5(data).hexdigest(),
    }


def composite_bytes(image):
    layer = Gimp.Layer.new_from_visible(image, image, "matrix composite")
    try:
        return read_buffer(layer, "R'G'B'A u8", (0, 0, image.get_width(), image.get_height()))
    finally:
        layer.delete()


def selection_bytes(image):
    return read_buffer(image.get_selection(), "Y u8", (0, 0, image.get_width(), image.get_height()))


# ------------------------------------------------------------------ build ----


def new_layer(image, name, w, h):
    return Gimp.Layer.new(image, name, w, h, Gimp.ImageType.RGBA_IMAGE, 100.0, Gimp.LayerMode.NORMAL)


def pattern_bytes(w, h, seed=1, alpha=255):
    """Deterministic, textured RGBA content (stripes + checker) for helper layers."""
    rows = []
    for y in range(h):
        row = bytearray(w * 4)
        for x in range(w):
            c = ((x // 12) + (y // 12) + seed) % 2
            r = 60 + 120 * c + (x * 7 + seed * 31) % 50
            g = 90 + (y * 5 + seed * 17) % 120
            b = 160 - 80 * c
            row[x * 4 : x * 4 + 4] = bytes((r % 256, g % 256, b % 256, alpha))
        rows.append(bytes(row))
    return b"".join(rows)


def build_new_scene(w, h):
    """A scene made only with GIMP operations (no file behind it)."""
    image = Gimp.Image.new(w, h, Gimp.ImageBaseType.RGB)
    layer = Gimp.Layer.new(image, "Background", w, h, Gimp.ImageType.RGB_IMAGE, 100.0, Gimp.LayerMode.NORMAL)
    image.insert_layer(layer, None, 0)
    Gimp.context_push()
    try:
        Gimp.context_set_foreground(Gegl.Color.new("#4a7fc0"))
        Gimp.context_set_background(Gegl.Color.new("#e8d9a8"))
        Gimp.context_set_gradient_fg_bg_rgb()
        layer.edit_gradient_fill(Gimp.GradientType.LINEAR, 0.0, False, 1, 0.0, True, 0, 0, 0, h)
        # some hard-edged boxes so there is structure to compare
        for i, color in enumerate(("#2e5d2a", "#7a3b1d", "#d0d0d0", "#202020")):
            Gimp.context_set_foreground(Gegl.Color.new(color))
            image.select_rectangle(
                Gimp.ChannelOps.REPLACE, int(w * (0.08 + 0.22 * i)), int(h * 0.55), int(w * 0.15), int(h * 0.3)
            )
            layer.edit_fill(Gimp.FillType.FOREGROUND)
        Gimp.Selection.none(image)
    finally:
        Gimp.context_pop()
    return image


def build_image(spec):
    w, h = int(spec.get("w", 1024)), int(spec.get("h", 768))
    source = spec.get("source", "scene")
    if source == "new":
        image = build_new_scene(w, h)
    else:
        path = SCENE if source == "scene" else source
        image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
        if (image.get_width(), image.get_height()) != (w, h) and spec.get("scale", True):
            image.scale(w, h)
    info = {"source": source, "size": [image.get_width(), image.get_height()], "setup": []}
    base = image.get_layers()[0]
    for op in spec.get("setup", []):
        info["setup"].append(apply_setup(image, base, op))
    for op in spec.get("post_setup", []):
        info["setup"].append(apply_setup(image, base, op))
    if spec.get("base") == "gray":
        image.convert_grayscale()
    elif spec.get("base") == "indexed":
        try:
            # GIMP 3.2.6 wants a palette name here even for GENERATE (ignored then)
            image.convert_indexed(
                Gimp.ConvertDitherType.NONE,
                Gimp.ConvertPaletteType.GENERATE,
                64,
                False,
                False,
                Gimp.context_get_palette().get_name(),
            )
        except Exception as err:  # noqa: BLE001
            proc = Gimp.get_pdb().lookup_procedure("gimp-image-convert-indexed")
            names = [a.get_name() for a in proc.get_arguments()]
            raise RuntimeError(f"indexed conversion failed: {err}; pdb args {names}") from err
        if image.get_base_type() != Gimp.ImageBaseType.INDEXED:
            raise RuntimeError("indexed conversion had no effect")
    precision = {
        "u16": Gimp.Precision.U16_NON_LINEAR,
        "float": Gimp.Precision.FLOAT_NON_LINEAR,
        "linear": Gimp.Precision.FLOAT_LINEAR,
        "u8_linear": Gimp.Precision.U8_LINEAR,
    }.get(spec.get("precision", "u8"))
    if precision is not None:
        image.convert_precision(precision)
    info["base_type"] = str(image.get_base_type())
    info["precision"] = str(image.get_precision())
    return image, info


def apply_setup(image, base, op):
    """Extra structure on top of the scene. Returns a description; may set
    image-level hints used for the drawables argument (``_drawables``)."""
    w, h = image.get_width(), image.get_height()
    if isinstance(op, dict):
        kind, params = op["op"], op
    else:
        kind, params = op, {}
    if kind == "hidden_layer":
        # bright magenta layer over the selection area, hidden: must not leak
        layer = new_layer(image, "hidden magenta", w, h)
        image.insert_layer(layer, None, 0)
        write_buffer(layer, (0, 0, w, h), "R'G'B'A u8", bytes((255, 0, 255, 255)) * (w * h))
        layer.set_visible(False)
        return {"op": kind, "layer": layer.get_name()}
    if kind == "alpha_transparent":
        # clear the left part of the area to transparent pixels
        if not base.has_alpha():
            base.add_alpha()
        x, y, cw, ch = [int(v) for v in params.get("rect", (0, 0, w // 2, h))]
        write_buffer(base, (x, y, cw, ch), "R'G'B'A u8", bytes(4 * cw * ch))
        return {"op": kind, "rect": [x, y, cw, ch]}
    if kind == "text_layer":
        font = Gimp.context_get_font()
        text = Gimp.TextLayer.new(
            image, params.get("text", "STABLE BOY"), font, float(params.get("size", 96)), Gimp.Unit.pixel()
        )
        image.insert_layer(text, None, 0)
        x, y = params.get("at", (w // 3, h // 3))
        text.set_offsets(int(x), int(y))
        return {"op": kind, "size": [text.get_width(), text.get_height()], "offset": [int(x), int(y)]}
    if kind == "rotated_layer":
        lw, lh = params.get("size", (w // 3, h // 3))
        layer = new_layer(image, "rotated", lw, lh)
        image.insert_layer(layer, None, 0)
        write_buffer(layer, (0, 0, lw, lh), "R'G'B'A u8", pattern_bytes(lw, lh, 3))
        x, y = params.get("at", (w // 3, h // 3))
        layer.set_offsets(int(x), int(y))
        rotated = layer.transform_rotate(math.radians(float(params.get("degrees", 30))), True, 0, 0)
        layer = rotated if rotated is not None else layer
        return {"op": kind, "size": [layer.get_width(), layer.get_height()], "offset": list(layer.get_offsets()[-2:])}
    if kind == "layer_offset":
        lw, lh = params.get("size", (w // 2, h // 2))
        x, y = params.get("at", (w // 4, h // 4))
        layer = new_layer(image, "offset layer", lw, lh)
        image.insert_layer(layer, None, 0)
        write_buffer(layer, (0, 0, lw, lh), "R'G'B'A u8", pattern_bytes(lw, lh, 5))
        layer.set_offsets(int(x), int(y))
        image.set_selected_layers([layer])
        DRAWABLES[image.get_id()] = [layer]
        return {"op": kind, "size": [lw, lh], "offset": [int(x), int(y)]}
    if kind == "layer_in_group":
        group = Gimp.GroupLayer.new(image, "group")
        image.insert_layer(group, None, 0)
        lw, lh = params.get("size", (w // 2, h // 2))
        x, y = params.get("at", (w // 4, h // 4))
        layer = new_layer(image, "child", lw, lh)
        image.insert_layer(layer, group, 0)
        write_buffer(layer, (0, 0, lw, lh), "R'G'B'A u8", pattern_bytes(lw, lh, 7))
        layer.set_offsets(int(x), int(y))
        image.set_selected_layers([layer])
        DRAWABLES[image.get_id()] = [layer]
        return {"op": kind, "group": group.get_name(), "child_size": [lw, lh], "offset": [int(x), int(y)]}
    if kind == "layer_mask_active":
        if not base.has_alpha():
            base.add_alpha()
        mask = base.create_mask(Gimp.AddMaskType.WHITE)
        base.add_mask(mask)
        DRAWABLES[image.get_id()] = [mask]
        return {"op": kind, "mask_of": base.get_name()}
    if kind == "channel_active":
        channel = Gimp.Channel.new(image, "user channel", w, h, 50.0, Gegl.Color.new("red"))
        image.insert_channel(channel, None, 0)
        image.set_selected_channels([channel])
        DRAWABLES[image.get_id()] = [channel]
        return {"op": kind, "channel": channel.get_name()}
    if kind == "path":
        cls = getattr(Gimp, "Path", None) or Gimp.Vectors
        path = cls.new(image, "user path")
        image.insert_path(path, None, 0) if hasattr(image, "insert_path") else image.insert_vectors(path, None, 0)
        sid = path.bezier_stroke_new_moveto(w * 0.1, h * 0.1)
        path.bezier_stroke_lineto(sid, w * 0.9, h * 0.2)
        path.bezier_stroke_lineto(sid, w * 0.5, h * 0.9)
        path.stroke_close(sid)
        return {"op": kind, "path": path.get_name()}
    raise ValueError(f"unknown setup op {kind}")


def _px(value, total, units):
    return round(value * total) if units == "frac" else int(value)


def apply_selection(image, spec):
    w, h = image.get_width(), image.get_height()
    kind = spec.get("kind", "rect")
    units = spec.get("units", "frac")

    def rect(r):
        return (_px(r[0], w, units), _px(r[1], h, units), max(1, _px(r[2], w, units)), max(1, _px(r[3], h, units)))

    if kind == "none":
        Gimp.Selection.none(image)
    elif kind == "all":
        Gimp.Selection.all(image)
    elif kind == "rect":
        image.select_rectangle(Gimp.ChannelOps.REPLACE, *rect(spec["rect"]))
    elif kind == "ellipse":
        image.select_ellipse(Gimp.ChannelOps.REPLACE, *rect(spec["rect"]))
    elif kind == "multi":
        for i, r in enumerate(spec["rects"]):
            image.select_rectangle(Gimp.ChannelOps.REPLACE if i == 0 else Gimp.ChannelOps.ADD, *rect(r))
    elif kind == "blob":
        # "quick mask" style: paint a soft irregular blob into a channel, load it as the selection
        x, y, bw, bh = rect(spec["rect"])
        channel = Gimp.Channel.new(image, "painted mask", w, h, 50.0, Gegl.Color.new("black"))
        image.insert_channel(channel, None, 0)
        data = bytearray(bw * bh)
        for yy in range(bh):
            for xx in range(bw):
                nx, ny = (xx - bw / 2) / (bw / 2), (yy - bh / 2) / (bh / 2)
                ang = math.atan2(ny, nx)
                radius = 0.75 + 0.2 * math.sin(3 * ang) + 0.05 * math.cos(7 * ang)
                d = math.hypot(nx, ny) / radius
                v = 255 if d < 0.85 else max(0, int(255 * (1 - (d - 0.85) / 0.15))) if d < 1 else 0
                data[yy * bw + xx] = v
        write_buffer(channel, (x, y, bw, bh), "Y u8", bytes(data))
        image.select_item(Gimp.ChannelOps.REPLACE, channel)
        image.remove_channel(channel)
    else:
        raise ValueError(f"unknown selection kind {kind}")
    if spec.get("feather"):
        Gimp.Selection.feather(image, float(spec["feather"]))
    bounds = tuple(Gimp.Selection.bounds(image))[-5:]
    return {
        "kind": kind,
        "empty": bool(Gimp.Selection.is_empty(image)),
        "bounds": [bounds[1], bounds[2], bounds[3] - bounds[1], bounds[4] - bounds[2]] if bounds[0] else None,
    }


# ---------------------------------------------------------------- inspect ----


def item_tree(image):
    def describe(layer):
        mask = layer.get_mask()
        d = {
            "id": layer.get_id(),
            "name": layer.get_name(),
            "size": [layer.get_width(), layer.get_height()],
            "offset": list(layer.get_offsets()[-2:]),
            "visible": layer.get_visible(),
            "is_group": layer.is_group_layer(),
            "has_mask": mask is not None,
        }
        if layer.is_group_layer():
            d["children"] = [describe(c) for c in layer.get_children()]
        return d

    return [describe(layer) for layer in image.get_layers()]


def flat_ids(tree):
    out = []
    for d in tree:
        out.append(d["id"])
        out += flat_ids(d.get("children", []))
    return out


def channel_names(image):
    return [c.get_name() for c in image.get_channels()]


def run_procedure(name, image, drawables, args):
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(name)
    if proc is None:
        raise RuntimeError(f"{name} is not registered")
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
    return status, message


# ------------------------------------------------------------------- case ----


def run_once(case, case_dir, run_index):
    tag = "" if run_index == 0 else f"-run{run_index + 1}"
    rec = {"run": run_index + 1}
    image, rec["image"] = build_image(case.get("image", {}))
    rec["selection"] = apply_selection(image, case.get("selection", {"kind": "rect", "rect": [0.4, 0.4, 0.2, 0.2]}))
    w0, h0 = image.get_width(), image.get_height()
    before_tree = item_tree(image)
    before_channels = channel_names(image)
    images_before = {i.get_id() for i in Gimp.get_images()}
    sel_before = selection_bytes(image)
    if run_index == 0:
        rec["raw_before"] = dump_raw(case_dir, "before", composite_bytes(image), w0, h0, 4)
        rec["raw_selection"] = dump_raw(case_dir, "selection", sel_before, w0, h0, 1)
    drawables = DRAWABLES.get(image.get_id()) or image.get_selected_layers() or image.get_layers()
    rec["drawables"] = [d.get_name() for d in drawables]
    proc_name = PROCS[case.get("proc", "fill")]
    t0 = time.time()
    status, message = run_procedure(proc_name, image, drawables, case.get("args", {}))
    rec["seconds"] = round(time.time() - t0, 2)
    rec["status"] = status.value_nick if hasattr(status, "value_nick") else str(status)
    rec["message"] = message
    after_tree = item_tree(image)
    rec["layers_before"] = before_tree
    rec["layers_after"] = after_tree
    rec["layer_count_before"] = len(flat_ids(before_tree))
    rec["layer_count_after"] = len(flat_ids(after_tree))
    rec["channels_before"] = before_channels
    rec["channels_after"] = channel_names(image)
    rec["size_after"] = [image.get_width(), image.get_height()]
    sel_after = selection_bytes(image) if rec["size_after"] == [w0, h0] else None
    rec["selection_restored"] = sel_after == sel_before if sel_after is not None else None
    if sel_after is None:
        rec["selection_after_empty"] = bool(Gimp.Selection.is_empty(image))
    new_ids = set(flat_ids(after_tree)) - set(flat_ids(before_tree))
    top_new = [d for d in after_tree if d["id"] in new_ids]
    rec["new_top_level"] = top_new
    # the generated layer: the first new top-level layer, or the first child of a new group
    target = None
    if top_new:
        first = Gimp.Item.get_by_id(top_new[0]["id"])
        if first.is_group_layer():
            children = first.get_children()
            rec["group_children"] = len(children)
            rec["group_children_with_mask"] = sum(1 for c in children if c.get_mask() is not None)
            rec["group_children_visible"] = [c.get_visible() for c in children]
            target = children[0] if children else None
            for i, child in enumerate(children[1:], start=2):
                if case.get("dump_all_children"):
                    rec[f"raw_child{i}"] = dump_raw(
                        case_dir,
                        f"layer-child{i}{tag}",
                        read_buffer(child, "R'G'B'A u8"),
                        child.get_width(),
                        child.get_height(),
                        4,
                        child.get_offsets()[-2:],
                    )
        else:
            target = first
    if target is not None:
        rec["raw_layer"] = dump_raw(
            case_dir,
            f"layer{tag}",
            read_buffer(target, "R'G'B'A u8"),
            target.get_width(),
            target.get_height(),
            4,
            target.get_offsets()[-2:],
        )
        mask = target.get_mask()
        if mask is not None:
            rec["raw_mask"] = dump_raw(
                case_dir,
                f"mask{tag}",
                read_buffer(mask, "Y u8"),
                mask.get_width(),
                mask.get_height(),
                1,
                target.get_offsets()[-2:],
            )
    if run_index == 0:
        rec["raw_after"] = dump_raw(case_dir, "after", composite_bytes(image), image.get_width(), image.get_height(), 4)
    # new images (text to image -> new image)
    new_images = [i for i in Gimp.get_images() if i.get_id() not in images_before]
    rec["new_images"] = []
    for i, img in enumerate(new_images):
        entry = {"size": [img.get_width(), img.get_height()], "layers": len(img.get_layers())}
        if run_index == 0:
            entry["raw"] = dump_raw(
                case_dir, f"newimage{i + 1}", composite_bytes(img), img.get_width(), img.get_height(), 4
            )
        rec["new_images"].append(entry)
        img.delete()
    image.delete()
    return rec


def run_case(case):
    case_id = case["id"]
    case_dir = os.path.join(OUT, case_id)
    os.makedirs(case_dir, exist_ok=True)
    report = {
        "id": case_id,
        "spec": case,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gimp_version": f"{Gimp.MAJOR_VERSION}.{Gimp.MINOR_VERSION}.{Gimp.MICRO_VERSION}",
        "runs": [],
    }
    log0 = log_size()
    timeline("start", case_id)
    t0 = time.time()
    try:
        for i in range(int(case.get("runs", 1))):
            report["runs"].append(run_once(case, case_dir, i))
        report["driver_error"] = None
    except Exception as err:  # noqa: BLE001 - recorded, the matrix continues
        report["driver_error"] = f"{type(err).__name__}: {err}"
        report["traceback"] = traceback.format_exc()
        print(report["traceback"], file=sys.stderr)
    report["total_seconds"] = round(time.time() - t0, 2)
    timeline("end", case_id)
    with open(os.path.join(case_dir, "plugin-log.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(log_slice(log0, log_size()).replace("\r\n", "\n"))
    with open(os.path.join(case_dir, "gimp-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(
        f"[sb-matrix] {case_id}: "
        + ", ".join(f"{r.get('status')} {r.get('seconds')}s" for r in report["runs"])
        + (f" DRIVER ERROR {report['driver_error']}" if report["driver_error"] else "")
    )


def main():
    with open(SPEC, encoding="utf-8") as fh:
        spec = json.load(fh)
    os.makedirs(OUT, exist_ok=True)
    only = [s for s in os.environ.get("OPENFILL_MATRIX_ONLY", "").split(",") if s]
    for case in spec["cases"]:
        if only and case["id"] not in only:
            continue
        # clean up leftovers from a failed case so memory does not pile up
        for img in Gimp.get_images():
            img.delete()
        run_case(case)


try:
    main()
except Exception:  # noqa: BLE001
    traceback.print_exc()
finally:
    timeline("quit", "-")
    if QUIT:
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
