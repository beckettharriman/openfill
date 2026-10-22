# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Model benchmark driver, executed INSIDE GIMP by the python-fu-eval batch
interpreter (not a unit test). It runs one model over a list of benchmark
cases (real photos, remove / replace / fill / expand) and records timings,
the plug-in and ComfyUI log lines of each case and raw before/after buffers.
Image building, selections and the procedure call reuse the helpers of
``tests/gimp_batch_matrix.py``; JPEG evidence, VRAM attribution and the index
are produced afterwards by the launcher (system Python with numpy and PIL).

    set GIMP3_DIRECTORY=<isolated test profile>
    set OPENFILL_BENCH_SPEC=<benchmark-spec.json>
    set OPENFILL_BENCH_OUT=<output root>           (results go to <out>/<model>/<case>/)
    set OPENFILL_BENCH_MODEL=<catalog model id>
    set OPENFILL_BENCH_CASES=<id,id,...>           (optional; default every applicable case)
    set OPENFILL_BENCH_DEADLINE=<epoch seconds>    (optional; no new case starts after it)
    gimp-console-3.2.exe --new-instance --no-splash --console-messages ^
        --batch-interpreter python-fu-eval ^
        -b "exec(open(r'C:\\repos\\gimp-stable-boy\\tests\\gimp_batch_benchmark.py').read())"

Spec format: {"cases": [case, ...]} where a case is a matrix case (see
gimp_batch_matrix.py: image, selection, proc, args) plus
"op" (remove-small, remove-large, replace-into-empty, replace-object,
material, damaged, expand-side, expand-all) and "families" (the model
families it applies to: sd15, sdxl, flux, qwen_image, inpaint_model).
The "model" argument is set from OPENFILL_BENCH_MODEL. A case already holding a
gimp-report.json is skipped, so an interrupted run resumes where it stopped.
"""

import json
import os
import sys
import time
import traceback

REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
BENCH_SPEC = os.environ["OPENFILL_BENCH_SPEC"]
BENCH_OUT = os.environ["OPENFILL_BENCH_OUT"]
BENCH_MODEL = os.environ["OPENFILL_BENCH_MODEL"]
BENCH_CASES = [s for s in os.environ.get("OPENFILL_BENCH_CASES", "").split(",") if s]
DEADLINE = float(os.environ.get("OPENFILL_BENCH_DEADLINE", "0") or 0)

# Reuse the matrix helpers without running the matrix: execute its source up to
# the module-level main() call. It reads OPENFILL_MATRIX_SPEC / OPENFILL_MATRIX_OUT at import.
os.environ.setdefault("OPENFILL_MATRIX_SPEC", BENCH_SPEC)
os.environ.setdefault("OPENFILL_MATRIX_OUT", BENCH_OUT)
_matrix_path = os.path.join(REPO, "tests", "gimp_batch_matrix.py")
with open(_matrix_path, encoding="utf-8") as _fh:
    _source = _fh.read()
_cut = _source.rfind("\ntry:\n    main()")
if _cut < 0:
    raise RuntimeError("gimp_batch_matrix.py layout changed: main() call not found")
M = {"__name__": "gimp_batch_matrix_helpers", "__file__": _matrix_path}
exec(compile(_source[:_cut], _matrix_path, "exec"), M)  # noqa: S102 - trusted repo file

Gimp = M["Gimp"]
PROFILE_DIR = Gimp.directory()
PLUGIN_LOG = os.path.join(PROFILE_DIR, "openfill", "openfill.log")
COMFY_LOG = os.path.join(PROFILE_DIR, "openfill", "comfyui.log")
TIMELINE = os.path.join(BENCH_OUT, "timeline.jsonl")


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def file_slice(path, start, end):
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(max(0, end - start)).decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError as err:
        return f"(log unavailable: {err})"


def timeline(event, case_id, **extra):
    with open(TIMELINE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), "event": event, "model": BENCH_MODEL, "case": case_id, **extra}) + "\n")


def run_case(case, index_in_process):
    case_id = case["id"]
    case_dir = os.path.join(BENCH_OUT, BENCH_MODEL, case_id)
    os.makedirs(case_dir, exist_ok=True)
    args = dict(case.get("args", {}))
    args["model"] = BENCH_MODEL
    report = {
        "id": case_id,
        "model": BENCH_MODEL,
        "op": case.get("op"),
        "spec": case,
        "args": args,
        "index_in_process": index_in_process,
        "gimp_version": f"{Gimp.MAJOR_VERSION}.{Gimp.MINOR_VERSION}.{Gimp.MICRO_VERSION}",
    }
    plog0, clog0 = file_size(PLUGIN_LOG), file_size(COMFY_LOG)
    try:
        image, report["image"] = M["build_image"](case.get("image", {}))
        report["selection"] = M["apply_selection"](image, case.get("selection", {"kind": "none"}))
        w0, h0 = image.get_width(), image.get_height()
        report["raw_before"] = M["dump_raw"](case_dir, "before", M["composite_bytes"](image), w0, h0, 4)
        if not report["selection"]["empty"]:
            report["raw_selection"] = M["dump_raw"](case_dir, "selection", M["selection_bytes"](image), w0, h0, 1)
        drawables = image.get_selected_layers() or image.get_layers()
        before_ids = set(M["flat_ids"](M["item_tree"](image)))
        timeline("start", case_id)
        report["started_epoch"] = time.time()
        status, message = M["run_procedure"](M["PROCS"][case.get("proc", "fill")], image, drawables, args)
        report["ended_epoch"] = time.time()
        timeline("end", case_id)
        report["seconds"] = round(report["ended_epoch"] - report["started_epoch"], 2)
        report["status"] = status.value_nick if hasattr(status, "value_nick") else str(status)
        report["message"] = message
        after_tree = M["item_tree"](image)
        report["new_layers"] = [d for d in after_tree if d["id"] not in before_ids]
        report["size_after"] = [image.get_width(), image.get_height()]
        report["raw_after"] = M["dump_raw"](
            case_dir, "after", M["composite_bytes"](image), image.get_width(), image.get_height(), 4
        )
        image.delete()
        report["driver_error"] = None
    except Exception as err:  # noqa: BLE001 - recorded, the benchmark continues
        report["driver_error"] = f"{type(err).__name__}: {err}"
        report["traceback"] = traceback.format_exc()
        print(report["traceback"], file=sys.stderr)
    report["plugin_log"] = file_slice(PLUGIN_LOG, plog0, file_size(PLUGIN_LOG))
    report["comfy_log"] = file_slice(COMFY_LOG, clog0, file_size(COMFY_LOG))
    with open(os.path.join(case_dir, "gimp-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(
        f"[sb-bench] {BENCH_MODEL} {case_id}: {report.get('status')} {report.get('seconds')}s"
        + (f" DRIVER ERROR {report['driver_error']}" if report["driver_error"] else ""),
        flush=True,
    )


def main():
    with open(BENCH_SPEC, encoding="utf-8") as fh:
        spec = json.load(fh)
    os.makedirs(BENCH_OUT, exist_ok=True)
    done_in_process = 0
    for case in spec["cases"]:
        if BENCH_CASES and case["id"] not in BENCH_CASES:
            continue
        if os.path.exists(os.path.join(BENCH_OUT, BENCH_MODEL, case["id"], "gimp-report.json")):
            continue
        if DEADLINE and time.time() > DEADLINE:
            timeline("deadline", case["id"])
            print(f"[sb-bench] deadline reached before {case['id']}", flush=True)
            break
        for img in Gimp.get_images():  # leftovers from a failed case
            img.delete()
        run_case(case, done_in_process)
        done_in_process += 1


try:
    main()
except Exception:  # noqa: BLE001
    traceback.print_exc()
finally:
    timeline("quit", "-")
    if os.environ.get("OPENFILL_TEST_QUIT", "1") == "1":
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
