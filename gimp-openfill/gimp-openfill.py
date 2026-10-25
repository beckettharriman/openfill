#!/usr/bin/env python3
# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""GIMP 3 entry point: registers the Filters > OpenFill procedures.

OpenFill started as a GIMP 3 port of Stable Boy by Torben Giesselmann
(https://github.com/thndrbrrr/gimp-stable-boy); none of its code remains.

Menu:
  Filters > OpenFill > Generative Fill...           python-fu-openfill-generative-fill
  Filters > OpenFill > Generative Expand...         python-fu-openfill-generative-expand
  Filters > OpenFill > Text to Image...             python-fu-openfill-text-to-image
  Filters > OpenFill > Generative Fill Settings...  python-fu-openfill-settings
  Filters > OpenFill > Stop AI Backend              python-fu-openfill-stop-backend
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import TYPE_CHECKING


def _drop_console_window() -> None:
    """Close the blank terminal window some GIMP installs open for Python plug-ins.

    Those installs map Python plug-ins to python.exe (a console program)
    instead of pythonw.exe, so Windows opens an empty terminal while the
    plug-in runs. When this process is the console's only user (not started
    from a terminal by hand), detach from it so the window closes."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleWindow():
            return
        pids = (ctypes.c_uint32 * 4)()
        if kernel32.GetConsoleProcessList(pids, 4) != 1:
            return
        kernel32.FreeConsole()
        devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the process
        sys.stdout = sys.stderr = devnull
    except Exception:  # noqa: BLE001 - cosmetic; never block the plug-in
        pass


_drop_console_window()

import gi  # noqa: E402

gi.require_version("Gimp", "3.0")
gi.require_version("GimpUi", "3.0")
from gi.repository import Gimp, GimpUi, GLib, GObject  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openfill import __version__, ui_text  # noqa: E402
from openfill.backend import BackendError, GenerationCancelled, describe_error  # noqa: E402
from openfill.config import Paths, Settings  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable

    from openfill.app import App

PROC_FILL = "python-fu-openfill-generative-fill"
PROC_EXPAND = "python-fu-openfill-generative-expand"
PROC_TXT2IMG = "python-fu-openfill-text-to-image"
PROC_SETTINGS = "python-fu-openfill-settings"
PROC_STOP = "python-fu-openfill-stop-backend"
MENU = "<Image>/Filters/OpenFill"
AUTHOR = "Beckett Harriman"
RW = GObject.ParamFlags.READWRITE

SAMPLERS = [
    "euler",
    "euler_ancestral",
    "heun",
    "dpm_2",
    "dpm_2_ancestral",
    "lms",
    "dpmpp_2s_ancestral",
    "dpmpp_2m",
    "dpmpp_sde",
    "dpmpp_2m_sde",
    "dpmpp_2m_sde_gpu",
    "ddim",
    "uni_pc",
]
SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform"]


def _choice(options: list[tuple[str, str]]) -> Gimp.Choice:
    """A Gimp.Choice from (nick, label) pairs, in order."""
    choice = Gimp.Choice.new()
    for index, (nick, label) in enumerate(options):
        choice.add(nick, index, label, "")
    return choice


def _model_choice() -> Gimp.Choice:
    """'active' (from the settings), the two legacy tiers, then every catalog model."""
    options = [
        ("active", "Active model (see Settings > Models)"),
        ("fast", "Fast (SD 1.5 inpainting)"),
        ("quality", "Quality (SDXL + Fooocus)"),
    ]
    try:
        from pathlib import Path

        from openfill.catalog import load_catalog

        for entry in load_catalog(plugin_dir=Path(__file__).resolve().parent):  # resolve() follows a junction
            if entry.runnable:
                options.append((entry.id, f"{entry.name} [{entry.tier}]".replace("_", "__")))
    except Exception:  # noqa: BLE001 - registration must not fail: the three fixed entries still work
        pass
    return _choice(options)


def _load_models_cache() -> dict:
    """The checkpoint lists cached by the last backend check ({} when there is none)."""
    import json

    try:
        paths = Paths.for_profile(Gimp.directory())
        return json.loads(paths.models_cache.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _plugin_log() -> logging.Logger | None:
    """The plug-in log (openfill.log in the profile), or None when it cannot be opened."""
    try:
        from openfill.log import get_logger

        paths = Paths.for_profile(Gimp.directory())
        return get_logger(paths.log, debug=Settings.load(paths.config).debug)
    except Exception:  # noqa: BLE001 - logging must never break a run
        return None


def _fail(procedure: Gimp.Procedure, error: BaseException, log: logging.Logger | None = None) -> Gimp.ValueArray:
    """Report ``error`` to the user (unless cancelled) and build the procedure's return values."""
    if isinstance(error, GenerationCancelled):
        # The user asked for it: no error dialog (the progress window already says so), CANCEL status.
        if log is not None:
            log.info("procedure cancelled: %s", error)
        if "interrupted" in str(error):  # stopped from outside GIMP (ComfyUI's own Cancel or /interrupt)
            Gimp.message(ui_text.INTERRUPTED_OUTSIDE)
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if isinstance(error, BackendError):
        text = ui_text.with_next_step(describe_error(error))
    else:
        text = ui_text.unexpected_error(type(error).__name__, str(error))
    if log is not None:
        log.error("procedure failed: %s\n%s", text, traceback.format_exc())
    Gimp.message(text)
    return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(text))


def _dialog(procedure: Gimp.Procedure, config: Gimp.ProcedureConfig, title: str) -> bool:
    """Fallback: the generic GIMP dialog generated from the procedure arguments."""
    GimpUi.init("gimp-openfill")
    dialog = GimpUi.ProcedureDialog.new(procedure, config, title)
    dialog.fill(None)
    ok = dialog.run()
    dialog.destroy()
    return bool(ok)


# ------------------------------------------------------------- dialogs ----

KIND_ARGS = {
    "fill": ["prompt", "negative", "variations", "strength", "seed", "model", "remove"],
    "expand": ["prompt", "negative", "left", "top", "right", "bottom", "variations", "seed", "model"],
    "txt2img": ["prompt", "negative", "width", "height", "target", "variations", "seed", "model"],
}
KIND_CAPABILITY = {"fill": ("fill", "remove"), "expand": ("expand",), "txt2img": ("txt2img",)}
LEGACY_NICKS = ("fast", "quality")


def _test_hooks() -> dict:
    """Test hooks for GUI runs (screenshots): a JSON file named by
    OPENFILL_TEST_UI_FILE (re-read on every call, so one GIMP session can open
    several dialogs with different hooks) or the older OPENFILL_TEST_AUTOCLOSE /
    OPENFILL_SETTINGS_TAB variables. Keys: autoclose (seconds, answers Cancel),
    settings_tab ("models"), fake_variations (count), progress_demo (seconds),
    expand (Generative Expand editor: margins, preset, anchor, drag, hover;
    see ``dialogs.apply_expand_hooks``), advanced_steps ([seconds, open] pairs),
    prompt_free (active model name shown as removal-only), dialog_dump
    (seconds) and dialog_out (JSON lines file the generation dialog
    writes its sizes and state to)."""
    import json

    hooks: dict = {}
    path = os.environ.get("OPENFILL_TEST_UI_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                hooks = json.load(fh) or {}
        except (OSError, ValueError):
            hooks = {}
    if "autoclose" not in hooks and os.environ.get("OPENFILL_TEST_AUTOCLOSE"):
        try:
            hooks["autoclose"] = float(os.environ["OPENFILL_TEST_AUTOCLOSE"])
        except ValueError:
            pass
    if "settings_tab" not in hooks and os.environ.get("OPENFILL_SETTINGS_TAB"):
        hooks["settings_tab"] = os.environ["OPENFILL_SETTINGS_TAB"]
    return hooks


def _pspec(config: Gimp.ProcedureConfig, prop: str) -> GObject.ParamSpec | None:
    """The ParamSpec of argument ``prop``, or None when there is no such argument."""
    try:
        return config.find_property(prop)
    except Exception:  # noqa: BLE001
        return None


def _choice_options(config: Gimp.ProcedureConfig, prop: str) -> list[tuple[str, str]] | None:
    """(nick, label) pairs of a choice argument, or None for other argument types."""
    pspec = _pspec(config, prop)
    if pspec is None or "Choice" not in pspec.__gtype__.name:
        return None
    try:
        choice = Gimp.param_spec_choice_get_choice(pspec)
        return [(nick, (choice.get_label(nick) or nick).replace("__", "_")) for nick in choice.list_nicks()]
    except Exception:  # noqa: BLE001
        return None


def _model_options(
    config: Gimp.ProcedureConfig, kind: str, settings: Settings
) -> tuple[list[tuple[str, str]], str, bool, frozenset]:
    """Model dropdown: the active model first, then the catalog models that
    can do ``kind`` (downloaded ones before the others). Also: the active model's
    name, whether it only removes objects, and the dropdown ids that are FLUX models
    ("active" included when the active model is one)."""
    from pathlib import Path

    from openfill import catalog as cat

    nicks = [n for n, _ in (_choice_options(config, "model") or [("active", "Active model")])]
    active_name, prompt_free = settings.active_model, False
    installed_rows, other_rows = [], []
    flux_ids: set[str] = set()
    try:
        from openfill import local_models

        base = Paths.for_profile(Gimp.directory()).base
        catalog = cat.load_merged(plugin_dir=Path(__file__).resolve().parent, base=base)
        root = cat.default_models_root(settings.comfy_dir)
        nodes = cat.default_custom_nodes_dir(settings.comfy_dir)
        # the user's own model files, and models chosen from the hubs, are offered too
        catalog = local_models.with_local(catalog, root, base) if settings.comfy_dir else catalog
        active_id = cat.effective_active(catalog, root, nodes, settings.active_model)
        active_name = active_id or "none installed"
        for entry in catalog:
            if entry.arch == "flux":
                flux_ids.add(entry.id)
            if entry.id == active_id:
                active_name = entry.name
                prompt_free = "remove" in entry.capabilities
                if entry.arch == "flux":
                    flux_ids.add("active")
                continue
            if (entry.id not in nicks and entry.source == "bundled") or not entry.runnable:
                continue
            if not any(c in entry.capabilities for c in KIND_CAPABILITY[kind]):
                continue
            installed = cat.install_status(entry, root, nodes, catalog).installed
            label = ui_text.model_option_label(entry.name, installed, False)
            if cat.is_opt_in(entry) and cat.tier_label(entry):
                label += f", {cat.tier_label(entry)}"  # FLUX: "Quality (slow)", only used when picked
            (installed_rows if installed else other_rows).append((entry.id, label))
    except Exception:  # noqa: BLE001 - the dropdown still offers the active model
        pass
    options = [("active", ui_text.model_option_label(active_name, True, True))]
    return options + installed_rows + other_rows, active_name, prompt_free, frozenset(flux_ids)


_PICKED_MODEL: str | None = None  # a dialog pick outside the registered model ids (one run)


def _model_arg(config: Gimp.ProcedureConfig) -> str:
    """The model of this run: the dialog's pick of a local or hub model, else the argument."""
    global _PICKED_MODEL
    picked, _PICKED_MODEL = _PICKED_MODEL, None
    return picked or config.get_property("model")


def _image_sizes(image: Gimp.Image | None) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """(image size, selection bounds size) for the dialog status lines."""
    if image is None:
        return None, None
    from openfill import gimp_io

    size = (image.get_width(), image.get_height())
    try:
        sel = gimp_io.selection_rect(image)
    except Exception:  # noqa: BLE001
        sel = None
    return size, ((sel.width, sel.height) if sel is not None else None)


def _generation_dialog(
    kind: str, procedure: Gimp.Procedure, config: Gimp.ProcedureConfig, image: Gimp.Image | None, hooks: dict
) -> bool:
    """The redesigned dialog; writes the user's choices back into ``config``
    (the same properties NONINTERACTIVE callers set)."""
    gi.require_version("Gtk", "3.0")
    from openfill import dialogs

    GimpUi.init("gimp-openfill")
    paths = Paths.for_profile(Gimp.directory()).ensure()
    settings = Settings.load(paths.config)
    history = ui_text.parse_history(settings.prompt_history)
    values = {key: config.get_property(key) for key in KIND_ARGS[kind]}
    # Expand starts empty unless its own last prompt is remembered (the placeholder says empty is fine).
    values["prompt"] = ui_text.initial_prompt(values.get("prompt"), [] if kind == "expand" else history)
    if values.get("model") in LEGACY_NICKS:
        values["model"] = "active"
    if hooks.get("mode") == "remove":
        values["remove"] = True
    values.update(hooks.get("values") or {})  # test hook: start values as if picked under Advanced
    models, active_name, prompt_free, flux_models = _model_options(config, kind, settings)
    if hooks.get("prompt_free"):  # test hook: as if the active model only removed objects
        active_name, prompt_free = str(hooks["prompt_free"]), True
    image_size, selection_size = _image_sizes(image)
    thumbnail = None
    if kind == "expand" and image is not None:
        try:
            thumbnail = image.get_thumbnail(360, 360, Gimp.PixbufTransparency.SMALL_CHECKS)
        except Exception:  # noqa: BLE001 - the editor draws a grey box instead
            thumbnail = None
    result = dialogs.run_generation_dialog(
        kind,
        values,
        history,
        models,
        autoclose_seconds=float(hooks.get("autoclose") or 0),
        image_size=image_size,
        selection_size=selection_size,
        default_size=(settings.txt2img_width, settings.txt2img_height),
        active_prompt_free=prompt_free,
        active_name=active_name,
        flux_models=flux_models,
        show_advanced=bool(hooks.get("advanced")),
        test_text=hooks.get("type"),
        test_key=hooks.get("press"),
        test_open_recent=bool(hooks.get("open_recent")),
        test_click=hooks.get("click"),
        test_expand=hooks.get("expand"),
        test_advanced_steps=hooks.get("advanced_steps"),
        test_dump=hooks.get("dialog_dump"),
        test_out=str(hooks.get("dialog_out") or ""),
        thumbnail=thumbnail,
    )
    if result is None:
        return False
    global _PICKED_MODEL
    _PICKED_MODEL = None
    if result.get("model") and result["model"] not in [n for n, _ in (_choice_options(config, "model") or [])]:
        # Not one of the ids registered with the procedure (a local file or a hub model, which
        # can change while GIMP runs): this run uses it; the procedure argument says "active".
        _PICKED_MODEL = result["model"]
        result["model"] = "active"
    for key, value in result.items():
        config.set_property(key, value)
    if result.get("prompt") and not result.get("remove"):
        fresh = Settings.load(paths.config)
        fresh.prompt_history = ui_text.dump_history(
            ui_text.remember_prompt(ui_text.parse_history(fresh.prompt_history), result["prompt"])
        )
        try:
            fresh.save(paths.config)
        except OSError as err:  # the history is a convenience: generate anyway
            log = _plugin_log()
            if log is not None:
                log.warning("prompt history not saved: %s", err)
    return True


def _dry_run(kind: str, config: Gimp.ProcedureConfig, hooks: dict) -> bool:
    """Test hook: record what the dialog wrote into the config instead of generating."""
    if not hooks.get("dry_run"):
        return False
    import json

    values = {key: config.get_property(key) for key in KIND_ARGS[kind]}
    with open(
        hooks.get("dry_run_out") or os.path.join(Gimp.directory(), "openfill", "dry-run.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump({"kind": kind, "values": values}, fh, indent=2)
    return True


def _ask(
    kind: str,
    title: str,
    procedure: Gimp.Procedure,
    config: Gimp.ProcedureConfig,
    image: Gimp.Image | None,
    hooks: dict,
) -> bool:
    """Run the dialog for ``kind``; False when cancelled."""
    try:
        return _generation_dialog(kind, procedure, config, image, hooks)
    except Exception:  # noqa: BLE001 - never leave the user without a dialog
        log = _plugin_log()
        if log is not None:
            log.error("custom dialog failed, using the generic one:\n%s", traceback.format_exc())
        return _dialog(procedure, config, title)


def _pick_variation(
    image: Gimp.Image | None,
    layers: list[Gimp.Layer] | None,
    title: str,
    autoclose: float = 0,
    test_steps: list[str] | None = None,
    restore_canvas: tuple[int, int, int, int] | None = None,
) -> str:
    """After several variations were inserted (in a layer group): let the
    user keep one, keep all, or discard them. Returns the outcome.

    Previewing a variation only toggles layer visibility. The undo stack is
    frozen meanwhile, so browsing the thumbnails does not bury the
    generation's own undo step under one visibility step per click; Discard
    thaws it first and is a single undo step of its own.

    ``restore_canvas`` (width, height, offset x, offset y), given after
    Generative Expand: Discard also resizes the canvas back to the size it had
    before the expand, in the same undo step, so the image is left as it was.
    """
    if image is None or not layers or len(layers) < 2:
        return "keep"
    gi.require_version("Gtk", "3.0")
    from openfill.ui_picker import run_picker, scaled

    GimpUi.init("gimp-openfill")
    thumbs = []
    for layer in layers:
        try:
            pixbuf = layer.get_thumbnail(320, 320, Gimp.PixbufTransparency.SMALL_CHECKS)
        except Exception:  # noqa: BLE001
            pixbuf = None
        thumbs.append((layer.get_name(), scaled(pixbuf)))

    def show(index: int) -> None:
        for i, layer in enumerate(layers):
            layer.set_visible(i == index)
        image.set_selected_layers([layers[index]])
        Gimp.displays_flush()

    frozen = [False]

    def thaw() -> None:
        if frozen[0]:
            frozen[0] = False
            image.undo_thaw()

    def discard() -> None:
        thaw()
        parent = layers[0].get_parent()
        image.undo_group_start()
        try:
            if (
                parent is not None
                and all(layer.get_parent() == parent for layer in layers)
                and len(parent.get_children()) == len(layers)
            ):
                image.remove_layer(parent)
            else:
                for layer in layers:
                    image.remove_layer(layer)
            if restore_canvas is not None:
                image.resize(*restore_canvas)
        finally:
            image.undo_group_end()
        Gimp.displays_flush()

    try:
        frozen[0] = bool(image.undo_freeze())
    except Exception:  # noqa: BLE001 - without a frozen stack the previews are ordinary undo steps
        frozen[0] = False
    try:
        return run_picker(
            title, thumbs, show, lambda: None, discard, autoclose_seconds=autoclose, test_steps=test_steps
        )
    finally:
        thaw()


def _canvas_state(image: Gimp.Image | None, layer: Gimp.Layer | None = None) -> tuple | None:
    """(width, height, offsets of ``layer``, layer): the canvas size and where a
    pre-existing layer sits, so the shift an expand applied can be undone."""
    try:
        layer = layer or image.get_layers()[0]
        offsets = layer.get_offsets()  # (ok, x, y)
        x, y = int(offsets[-2]), int(offsets[-1])
        return (image.get_width(), image.get_height(), (x, y), layer)
    except Exception:  # noqa: BLE001 - no image or no layer: nothing to restore
        return None


def _layer_ids(image: Gimp.Image | None) -> set[int]:
    try:
        return {layer.get_id() for layer in image.get_layers()}
    except Exception:  # noqa: BLE001
        return set()


def _undo_partial_expand(image: Gimp.Image | None, before: tuple | None, before_ids: set[int], log) -> None:  # noqa: ANN001
    """Remove the layers an interrupted expand added and give the canvas back its old size."""
    if image is None:
        return
    try:
        image.undo_group_start()
        try:
            for layer in image.get_layers():
                if layer.get_id() not in before_ids:
                    image.remove_layer(layer)
            restore = _canvas_restore(before, _canvas_state(image, before[3] if before else None))
            if restore is not None:
                image.resize(*restore)
        finally:
            image.undo_group_end()
        Gimp.displays_flush()
    except Exception:  # noqa: BLE001 - best effort; the error itself is still reported
        if log is not None:
            log.error("could not undo the partial expand: %s", traceback.format_exc())


def _canvas_restore(before: tuple | None, after: tuple | None) -> tuple[int, int, int, int] | None:
    """The ``image.resize`` arguments that take the canvas from ``after`` back
    to ``before`` (None when the canvas did not change)."""
    if before is None or after is None or (before[:2] == after[:2] and before[2] == after[2]):
        return None
    dx, dy = after[2][0] - before[2][0], after[2][1] - before[2][1]
    return (before[0], before[1], -dx, -dy)


def _after_insert(
    run_mode: Gimp.RunMode,
    image: Gimp.Image | None,
    layers: list[Gimp.Layer] | None,
    title: str,
    hooks: dict,
    log: logging.Logger | None = None,
    restore_canvas: tuple[int, int, int, int] | None = None,
) -> str:
    """INTERACTIVE only: the variations picker; returns its outcome ("keep"
    when there was none). Never fails the procedure."""
    if run_mode != Gimp.RunMode.INTERACTIVE:
        return "keep"
    try:
        return _pick_variation(
            image,
            layers,
            title,
            float(hooks.get("autoclose") or 0),
            test_steps=hooks.get("picker_steps"),
            restore_canvas=restore_canvas,
        )
    except Exception:  # noqa: BLE001
        if log is not None:
            log.warning("variations picker failed:\n%s", traceback.format_exc())


# ---------------------------------------------------------- test hooks ----


def _test_fake_variations(image: Gimp.Image, count: int, hooks: dict) -> None:
    """Screenshot hook: copies of the selected area (all but the first
    tinted) as variation layers in a group, then the picker. No backend."""
    from openfill import gimp_io
    from openfill.geometry import Rect

    rect = gimp_io.selection_rect(image) or gimp_io.image_rect(image)
    pad = max(16, max(rect.width, rect.height) // 4)
    full = gimp_io.image_rect(image)
    x0, y0 = max(0, rect.x - pad), max(0, rect.y - pad)
    x1, y1 = min(full.width, rect.x + rect.width + pad), min(full.height, rect.y + rect.height + pad)
    ctx = Rect(x0, y0, x1 - x0, y1 - y0)
    data = gimp_io.export_region_png(image, ctx)
    paths = Paths.for_profile(Gimp.directory()).ensure()
    layers = gimp_io.insert_results(
        image, [data] * max(2, count), ctx, "Generative Fill: test variations", 8.0, paths.temp
    )
    tints = [None, (200.0, 60.0, 0.0), (30.0, 70.0, 10.0), (120.0, 50.0, 0.0)]
    for layer, tint in zip(layers, tints, strict=False):
        if tint is not None:
            try:
                layer.colorize_hsl(*tint)
            except Exception:  # noqa: BLE001
                pass
    Gimp.displays_flush()
    _pick_variation(
        image, layers, "Generative Fill", float(hooks.get("autoclose") or 0), test_steps=hooks.get("picker_steps")
    )


def _test_progress_demo(image: Gimp.Image | None, seconds: float) -> None:
    """Screenshot hook: the real progress window fed with scripted steps."""
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from openfill.progress_window import ProgressWindow

    GimpUi.init("gimp-openfill")
    handle = None
    try:
        data = Gimp.progress_get_window_handle()
        handle = data.get_data() if data is not None else None
    except Exception:  # noqa: BLE001
        handle = None
    settings = Settings.load(Paths.for_profile(Gimp.directory()).config)
    win = ProgressWindow(
        "Generative Fill",
        "SD 1.5 Inpainting",
        on_cancel=None,
        position=(settings.progress_window_x, settings.progress_window_y),
        anchor_handle=handle,
    )
    pixbuf = None
    if image is not None:
        try:
            pixbuf = image.get_layers()[0].get_thumbnail(320, 320, Gimp.PixbufTransparency.SMALL_CHECKS)
        except Exception:  # noqa: BLE001
            pixbuf = None
    script = [(0.0, 0.05, "Starting the AI backend... 8 s")] + [
        (2.5 + i * 0.35, 0.05 + 0.9 * i / 25, f"Generating... step {i}/25") for i in range(1, 13)
    ]

    def at(fraction: float, text: str, show_preview: bool) -> bool:
        win._set_step(fraction, text)
        if show_preview and pixbuf is not None:
            win.show_pixbuf(pixbuf)
        return False

    for t, fraction, text in script:
        GLib.timeout_add(int(t * 1000), at, fraction, text, text.startswith("Generating"))
    GLib.timeout_add(int(max(seconds, 8.0) * 1000), lambda: (win.close_now(), False)[1])
    Gtk.main()


# ------------------------------------------------------------------ runs ----


def _ensure_model(operation: str, run_mode: Gimp.RunMode, hooks: dict) -> bool:
    """First run: without ComfyUI show the download notice; without a model that can do ``operation`` open the download manager."""
    if run_mode != Gimp.RunMode.INTERACTIVE or hooks.get("skip_model_check"):
        return True
    from openfill.app import App

    try:
        app = App()
        ready = app.comfy_ready()
        if ready and app.has_model_for(operation):
            return True
    except Exception:  # noqa: BLE001 - the generation path reports backend problems itself
        return True
    from openfill import catalog as cat
    from openfill.models_ui import run_models_dialog
    from openfill.ui_setup import ensure_comfy

    GimpUi.init("gimp-openfill")
    if not ready and not ensure_comfy(app):
        return False
    if app.has_model_for(operation):
        return True
    run_models_dialog(app, cat.NO_MODEL_MESSAGE)
    return app.has_model_for(operation)


def run_fill(
    procedure: Gimp.Procedure,
    run_mode: Gimp.RunMode,
    image: Gimp.Image,
    _drawables: list[Gimp.Drawable],
    config: Gimp.ProcedureConfig,
    _data: object,
) -> Gimp.ValueArray:
    """Filters > OpenFill > Generative Fill: dialog (interactive), generation, variations picker."""
    hooks = _test_hooks() if run_mode == Gimp.RunMode.INTERACTIVE else {}
    if hooks.get("fake_variations"):
        _test_fake_variations(image, int(hooks["fake_variations"]), hooks)
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    if hooks.get("progress_demo"):
        _test_progress_demo(image, float(hooks["progress_demo"]))
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    if not _ensure_model("remove", run_mode, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if run_mode == Gimp.RunMode.INTERACTIVE and not _ask("fill", "Generative Fill", procedure, config, image, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if _dry_run("fill", config, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    from openfill.app import App

    app = None
    try:
        app = App(interactive=run_mode == Gimp.RunMode.INTERACTIVE)
        layers = app.generative_fill(
            image,
            config.get_property("prompt"),
            config.get_property("negative"),
            config.get_property("variations"),
            config.get_property("strength"),
            config.get_property("seed"),
            _model_arg(config),
            remove=bool(config.get_property("remove")),
        )
    except Exception as err:  # noqa: BLE001 - every failure must reach the user
        return _fail(procedure, err, app.log if app else None)
    _after_insert(run_mode, image, layers, "Generative Fill", hooks, app.log)
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


def _ask_expand_switch(picked: str, better: str, hooks: dict) -> bool:
    """Offer the Expand default instead of the picked model; True to switch."""
    if "expand_switch" in hooks:  # test hook: answer without a dialog
        return bool(hooks["expand_switch"])
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    dialog = Gtk.MessageDialog(
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        text="Use a different model for Expand?",
    )
    dialog.format_secondary_text(ui_text.expand_model_note(better))
    dialog.add_button(f"Keep {picked}", Gtk.ResponseType.CANCEL)
    dialog.add_button(f"Use {better}", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    answer = dialog.run()
    dialog.destroy()
    return answer == Gtk.ResponseType.OK


def run_expand(
    procedure: Gimp.Procedure,
    run_mode: Gimp.RunMode,
    image: Gimp.Image,
    _drawables: list[Gimp.Drawable],
    config: Gimp.ProcedureConfig,
    _data: object,
) -> Gimp.ValueArray:
    """Filters > OpenFill > Generative Expand: grow the canvas and outpaint the new border."""
    hooks = _test_hooks() if run_mode == Gimp.RunMode.INTERACTIVE else {}
    if not _ensure_model("expand", run_mode, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if run_mode == Gimp.RunMode.INTERACTIVE and not _ask(
        "expand", "Generative Expand", procedure, config, image, hooks
    ):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if _dry_run("expand", config, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    from openfill.app import App

    app = None
    before = _canvas_state(image)
    before_ids = _layer_ids(image)
    try:
        app = App(interactive=run_mode == Gimp.RunMode.INTERACTIVE)
        model_choice = _model_arg(config)
        grows = any(config.get_property(side) > 0 for side in ("left", "top", "right", "bottom"))
        if run_mode == Gimp.RunMode.INTERACTIVE and grows:  # a pure crop generates nothing
            suggestion = app.expand_suggestion(model_choice)
            if suggestion and _ask_expand_switch(suggestion[0].name, suggestion[1].name, hooks):
                model_choice = suggestion[1].id
        layers = app.generative_expand(
            image,
            config.get_property("prompt"),
            config.get_property("negative"),
            config.get_property("left"),
            config.get_property("top"),
            config.get_property("right"),
            config.get_property("bottom"),
            config.get_property("variations"),
            config.get_property("seed"),
            model_choice,
        )
    except Exception as err:  # noqa: BLE001
        # Cancelled or failed part-way: earlier rounds already grew the canvas
        # and added layers. Put the image back the way it was.
        _undo_partial_expand(image, before, before_ids, app.log if app else None)
        return _fail(procedure, err, app.log if app else None)
    restore = _canvas_restore(before, _canvas_state(image, before[3] if before else None))
    outcome = _after_insert(run_mode, image, layers, "Generative Expand", hooks, app.log, restore_canvas=restore)
    if outcome != "discard":
        # Cropped sides are removed for good: the layers are cut to the new canvas. Done after the
        # picker, so Discard can still bring the whole image back.
        try:
            from openfill import gimp_io

            gimp_io.clip_layers_to_canvas(image)
        except Exception:  # noqa: BLE001 - the expand itself succeeded
            app.log.warning("layers not cut to the canvas:\n%s", traceback.format_exc())
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


def run_txt2img(
    procedure: Gimp.Procedure,
    run_mode: Gimp.RunMode,
    image: Gimp.Image | None,
    _drawables: list[Gimp.Drawable],
    config: Gimp.ProcedureConfig,
    _data: object,
) -> Gimp.ValueArray:
    """Filters > OpenFill > Text to Image: a new layer (or a new image) from a prompt."""
    hooks = _test_hooks() if run_mode == Gimp.RunMode.INTERACTIVE else {}
    if not _ensure_model("txt2img", run_mode, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if run_mode == Gimp.RunMode.INTERACTIVE and not _ask("txt2img", "Text to Image", procedure, config, image, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    if _dry_run("txt2img", config, hooks):
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    from openfill.app import App

    app = None
    try:
        app = App(interactive=run_mode == Gimp.RunMode.INTERACTIVE)
        app.text_to_image(
            image,
            config.get_property("prompt"),
            config.get_property("negative"),
            config.get_property("width"),
            config.get_property("height"),
            config.get_property("variations"),
            config.get_property("seed"),
            _model_arg(config),
            config.get_property("target"),
        )
    except Exception as err:  # noqa: BLE001
        return _fail(procedure, err, app.log if app else None)
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


SETTINGS_KEYS = [
    "backend",
    "comfy_dir",
    "url",
    "a1111_url",
    "autostart",
    "idle_timeout_minutes",
    "quality",
    "progress_window",
    "checkpoint",
    "sdxl_checkpoint",
    "fooocus_patch",
    "steps",
    "cfg",
    "sampler",
    "scheduler",
    "grow_percent",
    "feather_percent",
    "grow",
    "feather",
    "prefill",
    "max_side_sd15",
    "max_side_sdxl",
    "txt2img_width",
    "txt2img_height",
    "startup_timeout",
    "request_timeout",
    "extra_server_args",
    "debug",
]


def _settings_test_backend(app: App, values: dict, report: Callable[[str], None]) -> str:
    """Runs in the Settings dialog's worker thread (no GIMP calls): apply the
    values on screen to a private App, start the backend if needed, check it."""
    for key, value in values.items():
        setattr(app.settings, key, value)
    try:
        backend = app.backend(lambda _f, text: report(ui_text.plain_step_text(text)))
        health = backend.health()
    except BackendError as err:
        return ui_text.health_text(False, str(err), 0, False)
    if not health.ok:
        return ui_text.health_text(False, health.detail, 0, False)
    try:
        app.refresh_models_cache(health)
    except Exception as err:  # noqa: BLE001 - the cache only fills the legacy checkpoint lists
        app.log.warning("models cache not refreshed: %s", err)
    return ui_text.health_text(
        True, health.detail, len(health.checkpoints), health.inpaint_nodes, health.device, health.device_kind
    )


def _settings_dialog(procedure: Gimp.Procedure, config: Gimp.ProcedureConfig) -> bool:
    """Settings: General (what a user needs, the rest behind expanders) and
    Models. Writes the values back into ``config``."""
    gi.require_version("Gtk", "3.0")
    from openfill import dialogs
    from openfill.app import App
    from openfill.models_ui import ModelsPanel

    GimpUi.init("gimp-openfill")
    hooks = _test_hooks()
    values, choices, ranges = {}, {}, {}
    for key in SETTINGS_KEYS:
        prop = key.replace("_", "-")
        values[key] = config.get_property(prop)
        options = _choice_options(config, prop)
        if options is not None:
            choices[key] = options
        pspec = _pspec(config, prop)
        if isinstance(values[key], (int, float)) and not isinstance(values[key], bool) and pspec is not None:
            try:
                ranges[key] = (pspec.minimum, pspec.maximum)
            except AttributeError:
                pass
    test_app = App()  # created here: App() reads the profile path through GIMP (main thread only)
    result = dialogs.run_settings_dialog(
        values,
        choices,
        ranges,
        models_panel_factory=lambda: ModelsPanel(App()),
        test_backend=lambda v, report: _settings_test_backend(test_app, v, report),
        open_logs=test_app.open_log_folder,
        autoclose_seconds=float(hooks.get("autoclose") or 0),
        tab=str(hooks.get("settings_tab") or "") or ("" if test_app.has_model_for("remove") else "models"),
        show_advanced=bool(hooks.get("advanced")),
        select_model=str(hooks.get("select_model") or ""),
        test_steps=hooks.get("settings_steps"),
        test_out=str(hooks.get("settings_out") or ""),
    )
    if result is None:
        return False
    for key, value in result.items():
        config.set_property(key.replace("_", "-"), value)
    config.set_property("check-backend", False)  # the dialog has its own Check backend button
    return True


def run_settings(procedure: Gimp.Procedure, config: Gimp.ProcedureConfig, _data: object) -> Gimp.ValueArray:
    """Filters > OpenFill > Generative Fill Settings: edit and save the profile's config.json."""
    run_mode = config.get_property("run-mode")
    paths = Paths.for_profile(Gimp.directory()).ensure()
    settings = Settings.load(paths.config)
    for key in SETTINGS_KEYS:  # dialog always starts from the saved file, not last-used values
        try:
            config.set_property(key.replace("_", "-"), getattr(settings, key))
        except Exception:  # noqa: BLE001 - a hand-edited value of the wrong type keeps the default
            pass
    if run_mode == Gimp.RunMode.INTERACTIVE and not _settings_dialog(procedure, config):
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
    settings = Settings.load(paths.config)  # the Models tab may have changed active_model / token meanwhile
    for key in SETTINGS_KEYS:
        setattr(settings, key, config.get_property(key.replace("_", "-")))
    try:
        settings.save(paths.config)
    except OSError as err:
        text = ui_text.save_failed(str(paths.config), str(err))
        Gimp.message(text)
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(text))
    if config.get_property("check-backend"):
        from openfill.app import App

        app = App()
        health = app.check_backend()
        if health.ok:
            Gimp.message(
                f"Backend OK: {health.detail}\n"
                f"Checkpoints: {', '.join(health.checkpoints) or 'none'}\n"
                f"Inpaint nodes: {'yes' if health.inpaint_nodes else 'no'}"
            )
        else:
            Gimp.message(ui_text.with_next_step(f"Backend check failed: {health.detail}"))
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


def run_stop(procedure: Gimp.Procedure, _config: Gimp.ProcedureConfig, _data: object) -> Gimp.ValueArray:
    """Filters > OpenFill > Stop AI Backend: ask the supervisor to stop the managed ComfyUI."""
    from openfill.app import App

    try:
        Gimp.message(App().stop_backend())
    except Exception as err:  # noqa: BLE001
        return _fail(procedure, err)
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


# --------------------------------------------------------------- plug-in ----


class OpenFill(Gimp.PlugIn):
    """The plug-in: registers the five Filters > OpenFill procedures."""

    def do_set_i18n(self, _name: str) -> tuple[bool, str | None, str | None]:
        """No translations are shipped."""
        return False, None, None

    def do_query_procedures(self) -> list[str]:
        """Names of the procedures this plug-in provides."""
        return [PROC_FILL, PROC_EXPAND, PROC_TXT2IMG, PROC_SETTINGS, PROC_STOP]

    def do_create_procedure(self, name: str) -> Gimp.Procedure | None:
        """Build the procedure ``name`` with its arguments."""
        if name == PROC_FILL:
            return self._fill()
        if name == PROC_EXPAND:
            return self._expand()
        if name == PROC_TXT2IMG:
            return self._txt2img()
        if name == PROC_SETTINGS:
            return self._settings()
        if name == PROC_STOP:
            return self._stop()
        return None

    @staticmethod
    def _common_prompt_args(procedure: Gimp.Procedure, variations_default: int = 1) -> None:
        procedure.add_string_argument("prompt", "_Prompt", "What should appear in the selection", "", RW)
        procedure.add_string_argument(
            "negative", "_Negative prompt", "What to avoid", "blurry, low quality, watermark, text", RW
        )
        procedure.add_int_argument(
            "variations", "_Variations", "Number of results (layers)", 1, 8, variations_default, RW
        )

    @staticmethod
    def _seed_quality_args(procedure: Gimp.Procedure) -> None:
        procedure.add_int_argument("seed", "_Seed", "-1 for random", -1, 2**31 - 1, -1, RW)
        procedure.add_choice_argument(
            "model",
            "_Model",
            "Which model to use ('active' = the one chosen in Settings > Models)",
            _model_choice(),
            "active",
            RW,
        )

    def _image_procedure(
        self, name: str, func: Callable[..., Gimp.ValueArray], label: str, blurb: str, help_text: str
    ) -> Gimp.ImageProcedure:
        procedure = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, func, None)
        procedure.set_image_types("RGB*, GRAY*")
        procedure.set_sensitivity_mask(
            Gimp.ProcedureSensitivityMask.DRAWABLE
            | Gimp.ProcedureSensitivityMask.DRAWABLES
            | Gimp.ProcedureSensitivityMask.NO_DRAWABLES
        )
        procedure.set_menu_label(label)
        procedure.add_menu_path(MENU)
        procedure.set_documentation(blurb, help_text, name)
        procedure.set_attribution(AUTHOR, AUTHOR, "2022-2026")
        return procedure

    def _fill(self) -> Gimp.ImageProcedure:
        p = self._image_procedure(
            PROC_FILL,
            run_fill,
            "Generative _Fill...",
            "Fill the selection with AI-generated content",
            "Crops a context region around the selection, sends it to the AI backend "
            "(ComfyUI by default, started automatically), and adds the result as a new "
            "layer with a feathered layer mask. OpenFill " + __version__,
        )
        self._common_prompt_args(p)
        p.add_double_argument(
            "strength",
            "_Strength",
            "1.0 replaces the selection entirely; lower values keep more of the original",
            0.1,
            1.0,
            1.0,
            RW,
        )
        p.add_boolean_argument(
            "remove",
            "_Remove object (ignore prompt)",
            "Erase what is selected and continue the background. With the active model an "
            "installed LaMa runs at the image's own resolution (then MAT); a model picked "
            "by id is used as picked (erasers with their own prompt, generation models "
            "with a background prompt).",
            False,
            RW,
        )
        self._seed_quality_args(p)
        return p

    def _expand(self) -> Gimp.ImageProcedure:
        p = self._image_procedure(
            PROC_EXPAND,
            run_expand,
            "Generative _Expand...",
            "Grow the canvas and fill the new area with AI-generated content",
            "Resizes the canvas by the given margins and outpaints the new border, "
            "returning it as a new masked layer. OpenFill " + __version__,
        )
        self._common_prompt_args(p)
        for side in ("left", "top", "right", "bottom"):
            p.add_int_argument(
                side,
                f"_{side.capitalize()} (px)",
                f"Pixels to add on the {side} (negative: crop that side)",
                -4096,
                4096,
                256 if side in ("left", "right") else 0,
                RW,
            )
        self._seed_quality_args(p)
        return p

    def _txt2img(self) -> Gimp.ImageProcedure:
        p = self._image_procedure(
            PROC_TXT2IMG,
            run_txt2img,
            "_Text to Image...",
            "Generate an image from a text prompt",
            "Generates images from text into a new layer (sized to the selection when "
            "there is one) or a new image. OpenFill " + __version__,
        )
        p.set_sensitivity_mask(
            Gimp.ProcedureSensitivityMask.DRAWABLE
            | Gimp.ProcedureSensitivityMask.DRAWABLES
            | Gimp.ProcedureSensitivityMask.NO_DRAWABLES
            | Gimp.ProcedureSensitivityMask.NO_IMAGE
        )
        self._common_prompt_args(p)
        p.add_int_argument("width", "_Width", "0 = selection or configured default", 0, 4096, 0, RW)
        p.add_int_argument("height", "_Height", "0 = selection or configured default", 0, 4096, 0, RW)
        p.add_choice_argument(
            "target",
            "_Result as",
            "Where the result goes",
            _choice([("layer", "New layer"), ("image", "New image")]),
            "layer",
            RW,
        )
        self._seed_quality_args(p)
        return p

    def _settings(self) -> Gimp.Procedure:
        p = Gimp.Procedure.new(self, PROC_SETTINGS, Gimp.PDBProcType.PLUGIN, run_settings, None)
        p.set_menu_label("Generative Fill _Settings...")
        p.add_menu_path(MENU)
        p.set_documentation(
            "Configure the AI backend and generation defaults",
            "Backend location, model choice, sampling defaults, mask feathering "
            "and the idle timeout. Saved to the GIMP profile. OpenFill " + __version__,
            PROC_SETTINGS,
        )
        p.set_attribution(AUTHOR, AUTHOR, "2022-2026")
        p.add_enum_argument("run-mode", "Run mode", "The run mode", Gimp.RunMode, Gimp.RunMode.INTERACTIVE, RW)
        s = Settings()
        cache = _load_models_cache()
        p.add_choice_argument(
            "backend",
            "_Backend",
            "Generation server",
            _choice([("comfyui", "ComfyUI (managed, recommended)"), ("a1111", "AUTOMATIC1111 WebUI (external)")]),
            s.backend,
            RW,
        )
        p.add_string_argument("comfy-dir", "Co_mfyUI folder", "Folder of the ComfyUI portable build", s.comfy_dir, RW)
        p.add_string_argument("url", "Com_fyUI URL", "Base URL of ComfyUI", s.url, RW)
        p.add_string_argument("a1111-url", "_A1111 URL", "Base URL of the WebUI API", s.a1111_url, RW)
        p.add_boolean_argument("autostart", "S_tart backend automatically", "", s.autostart, RW)
        p.add_int_argument(
            "idle-timeout-minutes",
            "_Idle timeout (min)",
            "Stop the backend after this many idle minutes; 0 = until GIMP exits",
            0,
            24 * 60,
            s.idle_timeout_minutes,
            RW,
        )
        p.add_choice_argument(
            "quality",
            "L_egacy tier for 'fast'/'quality' calls",
            "",
            _choice([("fast", "Fast (SD 1.5 inpainting)"), ("quality", "Quality (SDXL + Fooocus)")]),
            s.quality,
            RW,
        )
        p.add_boolean_argument(
            "progress-window",
            "Float_ing progress window",
            "Show a floating window with progress, live preview and downloads",
            s.progress_window,
            RW,
        )
        self._model_arg(p, "checkpoint", "SD 1.5 _checkpoint", cache.get("checkpoints"), s.checkpoint)
        self._model_arg(p, "sdxl-checkpoint", "SD_XL checkpoint", cache.get("checkpoints"), s.sdxl_checkpoint)
        self._model_arg(p, "fooocus-patch", "Fooocus _patch", cache.get("fooocus_patches"), s.fooocus_patch)
        p.add_int_argument("steps", "Ste_ps", "", 1, 150, s.steps, RW)
        p.add_double_argument("cfg", "CF_G scale", "", 0.0, 30.0, s.cfg, RW)
        p.add_choice_argument("sampler", "Sampler", "", _choice([(x, x) for x in SAMPLERS]), s.sampler, RW)
        p.add_choice_argument("scheduler", "Sche_duler", "", _choice([(x, x) for x in SCHEDULERS]), s.scheduler, RW)
        p.add_double_argument(
            "grow-percent",
            "Mask grow (% of selection)",
            "Dilation of the selection sent to the model, as percent of its larger side",
            0.0,
            50.0,
            s.grow_percent,
            RW,
        )
        p.add_double_argument(
            "feather-percent",
            "Mask feather (% of selection)",
            "Soft edge of the layer mask, as percent of the selection's larger side",
            0.0,
            50.0,
            s.feather_percent,
            RW,
        )
        p.add_int_argument("grow", "Mask grow cap (px)", "Upper limit for the dilation", 0, 256, s.grow, RW)
        p.add_int_argument(
            "feather", "Mask feather cap (px)", "Upper limit for the layer mask feather", 0, 256, s.feather, RW
        )
        p.add_boolean_argument(
            "prefill",
            "Pre-fill masked area",
            "Hide old content under the mask before sampling (needs comfyui-inpaint-nodes)",
            s.prefill,
            RW,
        )
        p.add_int_argument("max-side-sd15", "Max _working size SD 1.5", "", 512, 2048, s.max_side_sd15, RW)
        p.add_int_argument("max-side-sdxl", "Max worki_ng size SDXL", "", 768, 2048, s.max_side_sdxl, RW)
        p.add_int_argument("txt2img-width", "Text to Image width", "", 64, 4096, s.txt2img_width, RW)
        p.add_int_argument("txt2img-height", "Text to Image height", "", 64, 4096, s.txt2img_height, RW)
        p.add_int_argument("startup-timeout", "Start_up timeout (s)", "", 30, 1800, s.startup_timeout, RW)
        p.add_int_argument(
            "request-timeout", "Re_quest timeout (s)", "0 = estimate per request", 0, 7200, s.request_timeout, RW
        )
        p.add_string_argument(
            "extra-server-args", "Extra ComfyUI ar_guments", "For example --lowvram", s.extra_server_args, RW
        )
        p.add_boolean_argument("debug", "_Verbose log", "", s.debug, RW)
        p.add_boolean_argument(
            "check-backend", "Chec_k backend now", "Start the backend if needed and list its models", True, RW
        )
        return p

    @staticmethod
    def _model_arg(procedure: Gimp.Procedure, name: str, nick: str, options: list | None, default: str) -> None:
        options = [str(o) for o in (options or [])]
        if default and default not in options:
            options.insert(0, default)
        if options:
            # a single underscore would become a mnemonic in the radio label
            procedure.add_choice_argument(
                name, nick, "", _choice([(o, o.replace("_", "__")) for o in options]), default, RW
            )
        else:
            procedure.add_string_argument(name, nick, "", default, RW)

    def _stop(self) -> Gimp.Procedure:
        p = Gimp.Procedure.new(self, PROC_STOP, Gimp.PDBProcType.PLUGIN, run_stop, None)
        p.set_menu_label("Stop AI _Backend")
        p.add_menu_path(MENU)
        p.set_documentation(
            "Shut down the managed ComfyUI backend to free GPU memory",
            "Asks the OpenFill supervisor to terminate the ComfyUI process it "
            "started. The backend restarts automatically on the next request.",
            PROC_STOP,
        )
        p.set_attribution(AUTHOR, AUTHOR, "2022-2026")
        p.add_enum_argument("run-mode", "Run mode", "The run mode", Gimp.RunMode, Gimp.RunMode.INTERACTIVE, RW)
        return p


Gimp.main(OpenFill.__gtype__, sys.argv)
