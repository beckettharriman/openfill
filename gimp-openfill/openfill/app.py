# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Orchestration: image in, backend request out, layers back in.

The image editor is behind ``App.io``: GIMP's ``gimp_io`` (imported only when no
other is given, since it needs ``gi``) or the PhotoCraft companion's bridge.
Everything else here is plain Python.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import caption, comfy_setup, geometry, hardware, local_models, quality, ui_text, workflow
from . import catalog as cat
from .backend import (
    SETTINGS_MENU,
    Backend,
    BackendError,
    BackendUnavailable,
    HealthStatus,
    InpaintRequest,
    ModelMissing,
    ModelSpec,
    Sampling,
    Txt2ImgRequest,
)
from .backends.a1111 import A1111Backend
from .backends.comfy import ComfyBackend
from .comfy_api import ComfyApiError, ComfyClient, unsupported_gpu_line
from .config import Paths, Settings, comfy_layout, comfy_problem
from .geometry import Rect
from .log import get_logger
from .progress import OverallProgress
from .server import ServerManager, _split_host_port, port_answers
from .workflow import model_display_name

NO_OPENCV_MESSAGE = (
    "ComfyUI has no OpenCV, so this result started from a plain colour instead of the surrounding "
    "picture and may not blend in (Generative Expand shows it most). "
)
NO_OPENCV_INSTALLING = (
    "OpenFill is now installing OpenCV into ComfyUI in the background (about 50 MB); run the same "
    "operation again in a minute for a better result."
)
NO_OPENCV_MANUAL = (
    "Install it into ComfyUI's Python (pip install opencv-python-headless), then run the same operation again."
)

# Model ids the old "fast"/"quality" choices meant without a catalog; with
# one they resolve to the tier's installed default (catalog.TIER_PREFERENCE).
LEGACY_CHOICES = {"fast": "sd15-inpainting", "quality": "sdxl-base"}
MODEL_LIST_NODES = (
    ("CheckpointLoaderSimple", "ckpt_name"),
    ("UNETLoader", "unet_name"),
    ("UnetLoaderGGUF", "unet_name"),
    ("INPAINT_LoadInpaintModel", "model_name"),
)


class NothingSelected(BackendError):
    """The command needs a selection and there is none."""


class UnsupportedImage(BackendError):
    """The image cannot take generated pixels as it is (indexed colors)."""


INDEXED_MESSAGE = (
    "This image uses indexed colors, which cannot hold generated pixels without destroying "
    "them. Convert it with Image > Mode > RGB (or Grayscale) and run the command again."
)
NO_SELECTION_MESSAGE = (
    "Nothing is selected. Select the area to fill first (for example with the Rectangle or "
    "Free Select tool), then run Generative Fill again. To regenerate the whole image, use "
    "Select > All."
)


def _layer_name(kind: str, prompt: str) -> str:
    text = " ".join(prompt.split())[:48].strip()
    return f"{kind}: {text}" if text else kind


class App:
    def __init__(self, interactive: bool = True, io: Any = None, runner_cls: Any = None) -> None:
        # interactive=False (scripts, batch): no progress window, so a failure never waits for a click
        self.interactive = interactive
        # The image editor: GIMP's gimp_io by default; the PhotoCraft companion passes its own
        # module with the same functions (export_region, insert_results, resize_canvas, ...).
        if io is None:
            from . import gimp_io as io
        if runner_cls is None:
            from .runner import Runner

            runner_cls = Runner
        self.io = io
        self.runner_cls = runner_cls
        self._opencv_noted = False
        self._device_checked = False
        self.paths = Paths.for_profile(self.io.profile_dir()).ensure()
        self.settings = Settings.load(self.paths.config)
        self.log = get_logger(self.paths.log, debug=self.settings.debug)
        if self.settings.load_problem:
            self.log.warning("settings: %s", self.settings.load_problem)
            try:
                self.io.message(self.settings.load_problem)
            except Exception:  # noqa: BLE001 - the warning is in the log either way
                pass
        self.server = ServerManager(self.settings, self.paths, self.log)
        self.plugin_dir = Path(__file__).resolve().parent.parent
        try:
            self.catalog = cat.load_merged(plugin_dir=self.plugin_dir, base=self.paths.base)
        except cat.CatalogError as err:
            self.log.warning("catalog unavailable: %s", err)
            self.catalog = []
        if self.catalog and self.settings.comfy_dir:
            # The user's own SD 1.5 inpainting, SDXL and FLUX.1 Fill files in ComfyUI's folders.
            try:
                found, skipped = local_models.scan(self.models_root, self.catalog, self.paths.base)
            except Exception as err:  # noqa: BLE001 - a broken folder must not hide the catalog
                self.log.warning("own model files not scanned: %s", err)
                found, skipped = [], []
            self.catalog = cat.merge_entries(self.catalog, found)
            for e in found:
                self.log.debug("own model file: %s (%s)", e.files[0].dest, e.family)
            for item in skipped:
                self.log.info("model file not used: %s (%s)", item["file"], item["reason"])

    # paths ---------------------------------------------------------------

    @property
    def models_root(self) -> Path:
        return cat.default_models_root(self.settings.comfy_dir)

    @property
    def custom_nodes_dir(self) -> Path:
        return cat.default_custom_nodes_dir(self.settings.comfy_dir)

    # backend -------------------------------------------------------------

    def backend(self, progress=None, cancel=None) -> Backend:
        url = self.server.ensure_running(progress, cancel)
        timeout = float(self.settings.request_timeout or 0)
        if self.settings.backend == "a1111":
            return A1111Backend(url, timeout, self.log)
        self._check_device(url)
        return ComfyBackend(url, request_timeout=timeout, logger=self.log, models_root=self.models_root)

    def _check_device(self, url: str) -> None:
        """Log the device ComfyUI computes on; when it is the processor, say so loudly
        (once per backend start): every generation is then many times slower."""
        if self._device_checked:
            return
        self._device_checked = True
        try:
            kind, desc = hardware.backend_device(ComfyClient(url, timeout=3).system_stats())
        except (ComfyApiError, OSError, ValueError) as err:
            self.log.info("backend device: unknown (%s)", err)
            return
        self.log.info("backend device: %s", desc or "none listed")
        if kind == "gpu":
            # PyTorch warns at start-up when it has no code for the card ("... with CUDA capability
            # sm_61 is not compatible with the current PyTorch installation"); every job would then
            # fail with a CUDA error. Say so before the first one.
            warning = unsupported_gpu_line(self.server.server_log_tail(400))
            if warning:
                self.log.warning("ComfyUI's PyTorch does not support this graphics card: %s", warning)
                self._warn_once("gpu-warned", warning, ui_text.unsupported_gpu_message(warning))
            return
        if kind != "cpu":
            return
        flags = " ".join(a for a in self.settings.extra_server_args.split() if a in ui_text.CPU_FLAGS)
        self.log.warning("ComfyUI runs on the processor, not a graphics card (%s)", flags or "its PyTorch has no GPU")
        self._warn_once("cpu-warned", desc, ui_text.cpu_backend_message(flags))

    def _warn_once(self, prefix: str, detail: str, message: str) -> None:
        """Show ``message`` once per backend start (a marker file named after ComfyUI's pid)."""
        marker = self.paths.base / f"{prefix}-{self.server.read_state().get('server_pid', 0)}"
        if marker.exists():
            return
        try:
            marker.write_text(detail, encoding="utf-8")
        except OSError:
            pass
        if threading.current_thread() is threading.main_thread():  # GIMP calls only from the main thread
            self.io.message(message)

    def refresh_models_cache(self, health: HealthStatus) -> None:
        if not health.ok:
            return
        data = {
            "updated": time.time(),
            "checkpoints": health.checkpoints,
            "fooocus_heads": health.fooocus_heads,
            "fooocus_patches": health.fooocus_patches,
            "inpaint_nodes": health.inpaint_nodes,
            "version": health.version,
        }
        try:
            self.paths.models_cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def server_model_files(self) -> list[str] | None:
        """Model files the running server lists (None when it is not up)."""
        if self.settings.backend != "comfyui":
            return None
        client = ComfyClient(self.server.url(), timeout=5)
        if not client.is_alive(timeout=2.0):
            return None
        names: list[str] = []
        for node, inp in MODEL_LIST_NODES:
            try:
                names += client.combo_options(node, inp)
            except ComfyApiError:
                pass
        return names

    # models --------------------------------------------------------------

    def resolve_model(self, choice: str | None) -> tuple[cat.ModelEntry | None, ModelSpec]:
        """Map a dialog choice ('active', a legacy tier or a catalog id) to a model."""
        self.require_backend_install()
        choice = (choice or "active").strip()
        if choice == "active":
            model_id = self.active_model_id()
            if not model_id:
                raise ModelMissing(self._no_default_message())
        elif choice in LEGACY_CHOICES:
            model_id = self.tier_model_id(choice)
        else:
            model_id = choice
        try:
            entry = cat.by_id(self.catalog, model_id)
        except cat.CatalogError:
            if choice in LEGACY_CHOICES or not self.catalog:
                return None, self.settings.model_for("quality" if choice == "quality" else "fast")
            raise ModelMissing(f"unknown model '{model_id}'; pick one in {SETTINGS_MENU} > Models") from None
        if not entry.runnable:
            raise ModelMissing(
                f"'{model_display_name(entry.name)}' is a companion file, not a model you can generate with"
            )
        status = cat.install_status(entry, self.models_root, self.custom_nodes_dir, self.catalog)
        if not status.installed:
            if not self.has_model_for("fill"):
                raise ModelMissing(cat.NO_MODEL_MESSAGE)
            raise ModelMissing(
                f"'{model_display_name(entry.name)}' is not downloaded yet. Open Filters > OpenFill > Generative Fill "
                "Settings > Models and press Download."
            )
        if status.missing_models:
            raise ModelMissing(
                f"'{model_display_name(entry.name)}' also needs {', '.join(status.missing_models)}; download it from the Models tab."
            )
        if status.missing_nodes:
            raise ModelMissing(
                f"'{model_display_name(entry.name)}' needs the custom node pack(s) {', '.join(status.missing_nodes)}; "
                "press Install nodes in the Models tab."
            )
        self._fetch_missing_previews(entry)
        return entry, cat.model_spec_for(entry, self.catalog)

    def _fetch_missing_previews(self, entry: cat.ModelEntry) -> None:
        """Queue the model's live-preview decoder (a few MB) when it is missing, e.g. for a model
        downloaded before OpenFill fetched them. The job runs anyway; the preview appears once it is there."""
        missing = cat.missing_previews(entry, self.models_root)
        if not missing or self.settings.backend != "comfyui":
            return
        try:
            current = self.server.task_status().get("current") or {}
            busy = [current, *self.server.pending_tasks()]
            if any(
                t.get("type") == "download"
                and {f.get("dest") for f in t.get("files") or []} & {f.dest for f in missing}
                for t in busy
            ):
                return
            self.server.queue_task(
                {
                    "type": "download",
                    "model_id": entry.id,
                    "models_root": str(self.models_root),
                    "use_token": False,
                    "files": [
                        {
                            "url": f.url,
                            "dest": f.dest,
                            "size": f.size,
                            "sha256": f.sha256,
                            "gated": False,
                            "optional": True,
                            "role": f.role,
                        }
                        for f in missing
                    ],
                }
            )
            self.log.info("queued the live-preview file(s) %s", ", ".join(f.filename for f in missing))
        except (OSError, ValueError) as err:
            self.log.warning("could not queue the live-preview file: %s", err)

    def profile_for(self, model: ModelSpec) -> geometry.ModelProfile:
        base = geometry.PROFILES.get(model.arch, geometry.SD15)
        if model.arch == "sd15":
            max_side = self.settings.max_side_sd15
        elif model.arch == "inpaint_model":
            max_side = base.max_side
        else:
            max_side = self.settings.max_side_sdxl
        return replace(base, max_side=max(base.min_side, int(max_side)))

    def sampling(self, model: ModelSpec, variations: int, seed: int, denoise: float | None = None) -> Sampling:
        steps, cfg = int(self.settings.steps), float(self.settings.cfg)
        sampler, scheduler = self.settings.sampler, self.settings.scheduler
        if model.arch == "flux":
            cfg, sampler, scheduler, steps = 30.0, "euler", "normal", max(20, min(steps, 30))
        elif model.effective_template == "sdxl_eraser":
            # RORem's own settings: no classifier-free guidance, 15 to 50 steps.
            cfg, sampler, scheduler, steps = 1.0, "euler", "normal", max(20, min(steps, 30))
        elif model.arch == "qwen_image":
            cfg, sampler, scheduler, steps = 2.5, "euler", "simple", max(20, min(steps, 30))
        batch = 1 if model.prompt_free else max(1, min(8, int(variations)))
        return Sampling(
            steps=steps,
            cfg=cfg,
            sampler=sampler,
            scheduler=scheduler,
            denoise=float(self.settings.denoise if denoise is None else denoise),
            seed=int(seed),
            batch=batch,
        )

    def require_backend_install(self) -> None:
        """With the managed ComfyUI backend, a missing ComfyUI folder is the real
        problem; report it before any model check (which would otherwise say a
        model is missing). A server that answers anyway (started by hand) is fine."""
        if self.settings.backend != "comfyui" or comfy_layout(self.settings.comfy_dir) is not None:
            return
        if ComfyClient(self.server.url(), timeout=3).is_alive(timeout=2.0):
            return
        raise BackendUnavailable(
            f"{comfy_problem(self.settings.comfy_dir)} Set the ComfyUI folder in {SETTINGS_MENU}, "
            "or install ComfyUI there (see the README, Install)."
        )

    def model_rows(self, server_files: list[str] | None = None) -> list[dict]:
        self.require_backend_install()
        active = self.active_model_id()
        rows = []
        for e in self.catalog:
            st = cat.install_status(e, self.models_root, self.custom_nodes_dir, self.catalog, server_files)
            label = st.label
            if label == "Available" and e.gated and not self.settings.hf_token:
                label = "Needs token"
            rows.append(
                {
                    "id": e.id,
                    "name": e.name,
                    "tier": e.tier,
                    "status": label,
                    "installed": st.installed,
                    "size": cat.human_size(e.total_size),
                    "bytes": e.total_size,
                    "license": e.license_name,
                    "license_url": e.license_url,
                    "page": e.page,
                    "source": "first-party" if e.source_type == "first-party" else "community quant",
                    "uploader": e.uploader,
                    "gated": e.gated,
                    "runnable": e.runnable,
                    "active": e.id == active,
                    "min_vram_gb": e.min_vram_gb,
                    "missing_nodes": st.missing_nodes,
                    "missing_models": st.missing_models,
                    "partial": st.partial,
                    "capabilities": list(e.capabilities),
                    "seen_by_server": st.seen_by_server,
                }
            )
        return rows

    def set_active(self, model_id: str) -> cat.ModelEntry:
        entry = cat.by_id(self.catalog, model_id)
        if not entry.runnable:
            raise ModelMissing(f"'{model_display_name(entry.name)}' cannot be the active model (companion file)")
        self.settings.active_model = model_id
        self._save_settings()
        self.log.info("active model set to %s", model_id)
        return entry

    def save_token(self, token: str) -> None:
        self.settings.hf_token = token.strip()
        self._save_settings()

    def _save_settings(self) -> None:
        """Save, turning a refused or failed write into a message the user can act on."""
        try:
            self.settings.save(self.paths.config)
        except OSError as err:
            self.log.error("settings not saved: %s", err)
            raise BackendError(f"The settings could not be saved: {err}") from err

    def has_model_for(self, operation: str) -> bool:
        """True when a downloaded model can run ``operation`` (fill, expand, txt2img, remove, edit)."""
        return bool(cat.runnable_installed(self.catalog, self.models_root, self.custom_nodes_dir, operation))

    def installed_models(self, operation: str = "remove") -> list[cat.ModelEntry]:
        """Installed models that can run ``operation`` ("remove" lists every runnable one)."""
        return cat.runnable_installed(self.catalog, self.models_root, self.custom_nodes_dir, operation)

    def active_model_id(self) -> str:
        """The model a dialog's "active model" means: the one set active by hand,
        else the automatic default among the installed models ("" for none)."""
        return cat.effective_active(self.catalog, self.models_root, self.custom_nodes_dir, self.settings.active_model)

    def tier_model_id(self, tier: str, operation: str = "fill") -> str:
        """The installed default of ``tier`` (fast, quality, remove), else the
        tier's first choice (so the error names the model to download)."""
        best = cat.tier_default(self.installed_models(), tier, operation)
        return best.id if best is not None else cat.TIER_PREFERENCE[tier][0]

    def _no_default_message(self) -> str:
        opt_in = [e for e in self.installed_models() if cat.is_opt_in(e)]
        if opt_in:
            return (
                f"'{model_display_name(opt_in[0].name)}' is only used when you pick it: choose it in the Model "
                f"list or press Set active in {SETTINGS_MENU} > Models."
            )
        return cat.NO_MODEL_MESSAGE

    def queue_download(self, model_id: str, include_dependencies: bool = True) -> list[str]:
        entry = cat.by_id(self.catalog, model_id)
        if cat.gated_files_missing(entry, self.models_root) and not self.settings.hf_token:
            pages = ", ".join(cat.login_pages(entry, self.models_root)) or entry.page
            raise ModelMissing(
                f"'{model_display_name(entry.name)}' needs a free Hugging Face login: accept the license at {pages} "
                "and paste your access key in the Models tab (the Download button shows the steps)."
            )
        self.models_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.models_root).free
        need = sum(f.size or 0 for f in entry.files if not cat.file_installed(self.models_root, f))
        if need and free < need + 512 * 1024 * 1024:
            raise ModelMissing(
                f"not enough disk space for '{model_display_name(entry.name)}': needs {cat.human_size(need)}, {cat.human_size(free)} free"
            )
        ids = []
        if include_dependencies:
            for dep in entry.requires_models:
                dep_entry = cat.by_id(self.catalog, dep)
                if not cat.install_status(dep_entry, self.models_root, None).installed:
                    ids += self.queue_download(dep, include_dependencies=False)
        task = {
            "type": "download",
            "model_id": entry.id,
            "models_root": str(self.models_root),
            "use_token": entry.gated,
            "files": [
                {
                    "url": f.url,
                    "dest": f.dest,
                    "size": f.size,
                    "sha256": f.sha256,
                    "gated": f.gated,
                    "optional": f.optional,
                    "role": f.role,
                }
                for f in entry.files
            ],
        }
        ids.append(self.server.queue_task(task))
        return ids

    def queue_node_install(self, model_id: str) -> str | None:
        entry = cat.by_id(self.catalog, model_id)
        layout = comfy_layout(self.settings.comfy_dir)
        if layout is None:
            raise ModelMissing(f"ComfyUI was not found at '{self.settings.comfy_dir}'")
        nodes = [n for n in entry.requires_nodes if cat.node_missing(self.custom_nodes_dir, n)]
        if not nodes:
            return None
        task = {
            "type": "install_nodes",
            "model_id": entry.id,
            "custom_nodes_dir": str(self.custom_nodes_dir),
            "python": str(layout["python"]),
            "restart_backend": True,
            "nodes": [{"repo": n.repo, "folder": n.folder, "commit": n.commit} for n in nodes],
        }
        return self.server.queue_task(task)

    def queue_opencv_install(self) -> str | None:
        """Queue the pinned OpenCV for ComfyUI's interpreter, unless it is already queued or running."""
        layout = comfy_layout(self.settings.comfy_dir) if self.settings.comfy_dir else None
        packages = list(comfy_setup.load_manifest().pip)
        if layout is None or not packages:
            return None
        current = self.server.task_status().get("current") or {}
        if current.get("type") == "install_pip" or any(
            t.get("type") == "install_pip" for t in self.server.pending_tasks()
        ):
            return None
        return self.server.queue_task({"type": "install_pip", "python": str(layout["python"]), "packages": packages})

    def _after_generation(self, result) -> None:
        """Tell the user, once per run, when the result was made without OpenCV, and fix that."""
        if result.info.get("prefill_fallback") != "no-opencv" or self._opencv_noted:
            return
        self._opencv_noted = True  # once per run: a multi-round expand falls back in every round
        try:
            queued = self.queue_opencv_install()
        except (OSError, ValueError) as err:
            self.log.warning("could not queue the OpenCV install: %s", err)
            queued = None
        self.io.message(NO_OPENCV_MESSAGE + (NO_OPENCV_INSTALLING if queued else NO_OPENCV_MANUAL))

    def nodes_to_install(self, model_id: str) -> list[cat.NodeSpec]:
        entry = cat.by_id(self.catalog, model_id)
        return [n for n in entry.requires_nodes if cat.node_missing(self.custom_nodes_dir, n)]

    def remove_model(self, model_id: str) -> str:
        entry = cat.by_id(self.catalog, model_id)
        shared = {
            f.dest
            for e in self.catalog
            if e.id != model_id
            for f in e.files
            if cat.install_status(e, self.models_root, None).installed
        }
        files = [f.dest for f in entry.files if f.dest not in shared]
        task = {"type": "remove", "model_id": entry.id, "models_root": str(self.models_root), "files": files}
        return self.server.queue_task(task)

    def open_models_folder(self) -> None:
        _open_folder(self.models_root)

    def open_log_folder(self) -> None:
        """The folder with openfill.log and comfyui.log (ComfyUI's console output)."""
        _open_folder(self.paths.base)

    def task_status(self) -> dict:
        return self.server.task_status()

    # ComfyUI setup -------------------------------------------------------

    def comfy_ready(self) -> bool:
        """True when a ComfyUI folder is configured and usable, or a server already answers."""
        if self.settings.backend != "comfyui" or comfy_layout(self.settings.comfy_dir) is not None:
            return True
        return ComfyClient(self.server.url(), timeout=3).is_alive(timeout=2.0)

    def reload_settings(self) -> None:
        """Re-read config.json (the setup task changes comfy_dir in another process)."""
        fresh = Settings.load(self.paths.config, backup=False)
        self.settings.__dict__.update(fresh.__dict__)

    def use_comfy_dir(self, path: str) -> None:
        """Make ``path`` the ComfyUI folder; raises ``BackendError`` when it holds no ComfyUI."""
        if comfy_layout(path) is None:
            raise BackendError(
                f"{comfy_problem(path)} Choose the folder that holds python_embeded and ComfyUI "
                "(the unpacked ComfyUI_windows_portable folder), or a ComfyUI checkout that holds main.py "
                "and a venv (or .venv) folder."
            )
        self.settings.comfy_dir = str(path)
        self._save_settings()

    def found_comfy(self) -> list[comfy_setup.Candidate]:
        """ComfyUI installs already on this PC (see comfy_setup.detect_comfy)."""
        return comfy_setup.detect_comfy()

    def setup_preflight(self, install_dir: Path) -> tuple[comfy_setup.SetupManifest, comfy_setup.Preflight]:
        """The pinned manifest and the pre-download checks for ``install_dir``."""
        manifest = comfy_setup.load_manifest()
        hw = hardware.detect(install_dir)
        host, port = _split_host_port(self.settings.url)
        return manifest, comfy_setup.preflight(
            manifest,
            hw,
            comfy_setup.video_controller_names(),
            hw.disk_free_bytes,
            install_dir,
            port,
            port_answers(host, port, timeout=0.5),
        )

    def queue_comfy_setup(self, install_dir: Path, cpu: bool = False, older_gpu: bool = False) -> str:
        """Queue the download and install of the pinned ComfyUI as a background task."""
        return self.server.queue_task(
            {"type": "setup_comfy", "install_dir": str(install_dir), "cpu": bool(cpu), "older_gpu": bool(older_gpu)}
        )

    # features ------------------------------------------------------------

    REMOVAL_PROMPT = (
        "seamless continuation of the surrounding background, same material, texture, "
        "lighting and color as the area around it, nothing added"
    )
    REMOVAL_NEGATIVE = "object, button, logo, text, watermark, person, blurry, smudge, artifacts, seam"

    def removal_model_available(self) -> cat.ModelEntry | None:
        """An installed prompt-free removal model (LaMa/MAT), best first."""
        # LaMa first: on the judged removal cases it won 19 of 33 against MAT and
        # almost never invents an object; MAT invents glyphs and figures.
        for mid in cat.REMOVE_PREFERENCE:
            try:
                entry = cat.by_id(self.catalog, mid)
            except cat.CatalogError:
                continue
            st = cat.install_status(entry, self.models_root, self.custom_nodes_dir, self.catalog)
            if st.installed and not st.missing_nodes:
                return entry
        return None

    def _is_default_choice(self, choice: str | None) -> bool:
        """True for "active", a legacy tier, or the active model itself (PhotoCraft's menus
        name it by id): the choices that mean "the default model", not a pick by hand."""
        choice = (choice or "active").strip()
        if choice == "active" or choice in LEGACY_CHOICES:
            return True
        try:
            return choice == self.active_model_id()
        except Exception:  # noqa: BLE001 - no catalog or ComfyUI folder: treat as a pick
            return False

    def expand_suggestion(self, model_choice: str) -> tuple[cat.ModelEntry, cat.ModelEntry] | None:
        """(picked, better) when the Expand default is installed and is not the
        model picked; None otherwise. The pick is always respected unless the
        user agrees to the switch."""
        entry, _model = self.resolve_model(model_choice)
        try:
            installed = cat.runnable_installed(self.catalog, self.models_root, self.custom_nodes_dir, "expand")
        except Exception:  # noqa: BLE001 - no suggestion when the catalog cannot be read
            return None
        best = cat.expand_suggestion(installed, entry)
        return (entry, best) if best is not None else None

    def removal_fallback(self, model_choice: str) -> str:
        """Model for a removal (empty prompt or Remove object) when no LaMa/MAT
        is installed: the chosen model, unless it is FLUX or Qwen (they repaint
        the object they were asked to remove); then the Fast, else the Quality
        default. Raises ModelMissing when only opt-in models are installed."""
        try:
            entry, _model = self.resolve_model(model_choice)
        except ModelMissing:
            return model_choice  # resolve_model reports the real problem again
        if entry is None or not cat.is_opt_in(entry):
            return model_choice
        best = cat.removal_substitute(self.installed_models(), entry)
        if best is None:
            raise ModelMissing(
                f"Removing an object needs LaMa (0.2 GB) or another fill model, not "
                f"'{model_display_name(entry.name)}'. Download LaMa in {SETTINGS_MENU} > Models."
            )
        self.log.info("fill: removal not sent to %s -> using %s", entry.id, best.id)
        return best.id

    def generative_fill(
        self,
        image,
        prompt: str,
        negative: str,
        variations: int,
        strength: float,
        seed: int,
        model_choice: str,
        remove: bool = False,
    ) -> list:
        # Checked before anything else (no backend start, no model lookup): without a
        # selection there is nothing to fill, and filling the whole image is never
        # what was meant.
        self._check_image(image)
        selection = self.io.selection_rect(image)
        if selection is None:
            self.log.info("fill: refused, nothing selected")
            raise NothingSelected(NO_SELECTION_MESSAGE)
        if (remove or not prompt.strip()) and self._is_default_choice(model_choice):
            # Remove object, or an empty prompt, with the default model: LaMa (then MAT)
            # whenever one is installed; generation checkpoints (SDXL in 10 of 13 judged
            # cases) repaint the object they were asked to remove. A model picked by
            # hand is used as picked: any installed model can remove.
            removal = self.removal_model_available()
            if removal is not None:
                self.log.info("fill: %s -> using %s", "Remove object" if remove else "empty prompt", removal.id)
                model_choice = removal.id
            else:
                model_choice = self.removal_fallback(model_choice)
                self.log.info(
                    "fill: removal requested but no LaMa/MAT installed; using %s with a background prompt",
                    model_choice,
                )
        entry, model = self.resolve_model(model_choice)
        profile = self.profile_for(model)
        full = self.io.image_rect(image)
        target = selection
        removal_intent = remove or not prompt.strip() or model.prompt_free or model.eraser
        refine = (
            bool(self.settings.refine)
            and model.effective_template in workflow.REFINE_TEMPLATES
            and float(self.settings.refine_denoise) > 0
        )
        plan = geometry.plan_fill(
            target,
            full,
            profile,
            model.arch,
            removal_intent,
            geometry.fill_padding(removal_intent, model.prompt_free),
            refine=refine,
            layout=float(self.settings.tile_denoise) < 1.0,
            layout_min_scale=float(self.settings.layout_min_scale),
            # Erasers work at full image resolution, in tiles of their own working size.
            min_scale=1.0 if model.eraser else None,
        )
        if model.native_lama:
            # LaMa at the image's own pixels: OpenFill's node fits it to the graphics memory.
            plan = geometry.plan_native_removal(target, full)
        elif model.prompt_free and int(self.settings.removal_tile) > 0:
            tiled = geometry.plan_removal(
                target, full, int(self.settings.removal_tile), int(self.settings.removal_tile_overlap)
            )
            plan = tiled or plan
        ctx, ws = plan.context, plan.work
        mp = geometry.mask_params(
            target,
            ws.scale,
            self.settings.grow_percent,
            self.settings.feather_percent,
            self.settings.grow,
            self.settings.feather,
        )
        layer_grow = geometry.layer_mask_grow(mp, removal_intent)
        color_match = geometry.color_match_strength(target, ctx, full, removal_intent)
        prefill_style, prefill_mode = geometry.prefill_for(removal_intent, strength, model.arch)
        if model.prompt_free:
            prompt, negative = "", ""
        elif model.eraser:
            # Erase-trained: their own prompt (what the user typed is not used).
            prompt, negative = workflow.ERASER_PROMPTS.get(model.effective_template, ("", ""))
            strength = 1.0
        elif removal_intent:
            # Empty prompt on a generation model: ask for background, not "anything".
            prompt = self.REMOVAL_PROMPT
            negative = (negative + ", " if negative.strip() else "") + self.REMOVAL_NEGATIVE
            strength = 1.0
        self.log.info(
            "fill: selection=%s context=%s working=%dx%d scale=%.2f mask(grow=%d/%dpx feather=%d/%dpx "
            "layer grow=%dpx) prefill=%s/%s color_match=%.1f model=%s template=%s",
            target,
            ctx,
            ws.width,
            ws.height,
            ws.scale,
            mp.grow_image,
            mp.grow_work,
            mp.feather_image,
            mp.blur_work,
            layer_grow,
            prefill_style,
            prefill_mode,
            color_match,
            entry.id if entry else model.checkpoint,
            model.effective_template,
        )
        if plan.tiles:
            self.log.info(
                "fill: tiles=%d at scale %.2f%s: %s",
                len(plan.tiles),
                plan.tile_scale,
                f" after a layout pass, denoise {float(self.settings.tile_denoise):.2f}" if plan.layout else "",
                [(t.rect.x, t.rect.y, t.rect.width, t.rect.height, t.work_width, t.work_height) for t in plan.tiles],
            )
        if plan.refine_tiles:
            self.log.info(
                "fill: refine tiles=%d denoise=%.2f: %s",
                len(plan.refine_tiles),
                float(self.settings.refine_denoise),
                [(t.rect.x, t.rect.y, t.rect.width, t.rect.height) for t in plan.refine_tiles],
            )
        image_png, image_rgba = self.io.export_region(image, ctx)
        mask_png, mask_raw = self.io.export_selection_mask(image, ctx)
        if self.settings.scene_caption and not removal_intent and not model.prompt_free:
            sw, sh, small = caption.sample(ctx.width, ctx.height, image_rgba, 4)
            _, _, small_mask = caption.sample(ctx.width, ctx.height, mask_raw, 1)
            description = caption.describe(sw, sh, small, 4, small_mask)
            if description:
                prompt = caption.augment(prompt, description)
                self.log.info("fill: scene description added: %s", description)
        request = workflow.PlannedInpaintRequest(
            image_png=image_png,
            mask_png=mask_png,
            width=ctx.width,
            height=ctx.height,
            work_width=ws.width,
            work_height=ws.height,
            prompt=prompt,
            negative=negative,
            model=model,
            sampling=self.sampling(model, variations, seed, strength),
            mode="fill",
            grow=mp.grow_work,
            feather=mp.blur_work,
            prefill=self.settings.prefill,
            prefill_style=prefill_style,
            prefill_mode=prefill_mode,
            color_match=color_match,
            tiles=plan.tiles,
            refine_tiles=plan.refine_tiles,
            refine_denoise=float(self.settings.refine_denoise),
            tile_denoise=float(self.settings.tile_denoise) if plan.layout else 1.0,
            full_grow=mp.grow_image,
            full_blur=min(mp.grow_image, mp.feather_image),
        )
        title = "Object Removal" if model.prompt_free else "Generative Fill"
        runner = self.runner_cls(
            self, title, model_display_name(entry.name) if entry else model.checkpoint, interactive=self.interactive
        )
        try:
            result = runner.run(
                lambda progress, preview, cancel: self.backend(progress, cancel).inpaint(
                    request, progress, preview, cancel
                )
            )
            self.server.touch()
            self._after_generation(result)
            self._debug_dump("fill", request, result, ctx)
            layers = self.io.insert_results(
                image,
                result.images,
                ctx,
                _layer_name(title, prompt),
                float(mp.feather_image),
                self.paths.temp,
                grow=layer_grow,
            )
        except BaseException as err:
            runner.fail(err)
            raise
        if self.settings.seam_correction and not model.prompt_free:
            self._correct_seams(image, layers, ctx, image_rgba, mask_raw, layer_grow)
        if self.settings.grain_match and not model.prompt_free:
            self._match_grain(image, layers, ctx, image_rgba, mask_raw, result.seed)
        if len(layers) > 1 and self.settings.rank_variations:
            layers = self._rank_variations(image, layers, ctx, image_rgba, mask_raw, removal_intent, title, prompt)
        runner.done(f"Added layer: {layers[0].get_name()}" if layers else "Done")
        self.log.info("fill: done, seed=%s, %d layer(s)", result.seed, len(layers))
        return layers

    def _correct_seams(
        self,
        image,
        layers: list,
        ctx: Rect,
        image_rgba: bytes,
        mask_raw: bytes,
        band: int,
        original_rect: tuple[int, int, int, int] | None = None,
        kind: str = "fill",
    ) -> None:
        """Remove the faint outline a fill leaves on smooth areas: the layer's low-frequency
        colour difference to the original just outside the selection, spread over the fill
        (``quality.seam_field``). Cosmetic: any failure keeps the layer as it came."""
        try:
            started = time.monotonic()
            image.undo_group_start()
            try:
                for index, layer in enumerate(layers):
                    if (layer.get_width(), layer.get_height()) != (ctx.width, ctx.height):
                        continue
                    pixels = self.io.layer_rgba(layer)
                    field = quality.seam_field(
                        ctx.width, ctx.height, image_rgba, pixels, mask_raw, 4, band=band, original_rect=original_rect
                    )
                    if field is None:
                        self.log.info(
                            "%s: layer %d meets the original at the edge; no seam correction", kind, index + 1
                        )
                        continue
                    largest = max(max(abs(v) for v in cell) for cell in field.offsets)
                    self.io.set_layer_rgba(layer, quality.apply_seam_field(pixels, ctx.width, ctx.height, 4, field))
                    self.log.info(
                        "%s: seam corrected on layer %d (largest offset %.1f levels)", kind, index + 1, largest
                    )
            finally:
                image.undo_group_end()
            self.log.info("%s: seam correction in %.2f s", kind, time.monotonic() - started)
        except Exception as err:  # noqa: BLE001 - cosmetic; never lose the result
            self.log.warning("%s: seam correction skipped: %s", kind, err)

    def _match_grain(self, image, layers: list, ctx: Rect, image_rgba: bytes, mask_raw: bytes, seed: int) -> None:
        """Generated fills come back cleaner than a grainy photo. Measure the
        noise in flat areas around the selection and inside each generated
        layer and add the missing part (monochrome Gaussian) to the layer; the
        layer mask limits it to the fill. Nothing is added on clean photos."""
        try:
            started = time.monotonic()
            ring = quality.noise_sigma(ctx.width, ctx.height, image_rgba, 4, mask_raw, inside=False, step=3)
            if ring < 1.0:
                self.log.info("fill: grain %.2f around the selection; nothing to match", ring)
                return
            image.undo_group_start()
            try:
                for index, layer in enumerate(layers):
                    if (layer.get_width(), layer.get_height()) != (ctx.width, ctx.height):
                        continue
                    pixels = self.io.layer_rgba(layer)
                    inside = quality.noise_sigma(ctx.width, ctx.height, pixels, 4, mask_raw, inside=True, step=3)
                    add = quality.grain_to_add(ring, inside)
                    self.log.info("fill: grain ring %.2f, layer %d %.2f, adding %.2f", ring, index + 1, inside, add)
                    if add > 0:
                        self.io.set_layer_rgba(
                            layer, quality.add_grain(pixels, 4, add, seed=(int(seed) + index) & 0xFFFF)
                        )
            finally:
                image.undo_group_end()
            self.log.info("fill: grain matched in %.2f s", time.monotonic() - started)
        except Exception as err:  # noqa: BLE001 - grain is cosmetic; never lose the result
            self.log.warning("fill: grain matching skipped: %s", err)

    def _rank_variations(
        self, image, layers: list, ctx: Rect, image_rgba: bytes, mask_raw: bytes, removal: bool, title: str, prompt: str
    ) -> list:
        """Order the variation layers best first (``quality.rank`` on small
        copies of the context); the best becomes the visible one. Ranking is
        advisory: any failure keeps the original order."""
        try:
            started = time.monotonic()
            sw, sh, original = caption.sample(ctx.width, ctx.height, image_rgba, 4)
            _, _, small_mask = caption.sample(ctx.width, ctx.height, mask_raw, 1)
            candidates = []
            for layer in layers:
                if (layer.get_width(), layer.get_height()) != (ctx.width, ctx.height):
                    return layers
                cw, ch, data = caption.sample(ctx.width, ctx.height, self.io.layer_rgba(layer), 4)
                candidates.append(quality.Buffer(cw, ch, data, 4))
            base = quality.Buffer(sw, sh, original, 4)
            distance = quality.signed_distance(sw, sh, small_mask)
            scores = [quality.score(base, c, small_mask, removal, distance) for c in candidates]
            order = sorted(range(len(layers)), key=lambda i: (scores[i].total, i))
            self.log.info(
                "fill: ranked variations %s (scores %s) in %.2f s",
                [i + 1 for i in order],
                [round(sc.total, 3) for sc in scores],
                time.monotonic() - started,
            )
            return self.io.reorder_variations(image, layers, order, _layer_name(title, prompt))
        except Exception as err:  # noqa: BLE001 - ranking must never lose the result
            self.log.warning("fill: ranking skipped: %s", err)
            return layers

    def generative_expand(
        self,
        image,
        prompt: str,
        negative: str,
        left: int,
        top: int,
        right: int,
        bottom: int,
        variations: int,
        seed: int,
        model_choice: str,
    ) -> list:
        """Crop and/or grow the canvas by the given margins and generate the new area.

        A negative margin crops that side (the canvas shrinks; the layers are
        cut to it later, see ``gimp_io.clip_layers_to_canvas``); a positive one
        adds space that is generated. Every growing side is rendered in the
        same pass, with the kept image as context. Large expansions run in
        rounds (``geometry.expand_rounds``), each again all sides at once; each
        round reads the visible result of the previous one and only the last
        makes the requested variations. All rounds share one progress window.
        """
        self._check_image(image)
        width, height = image.get_width(), image.get_height()
        margins = geometry.fit_expand_margins((left, top, right, bottom), width, height)
        if margins != (left, top, right, bottom):
            self.log.info("expand: margins %s limited to %s", (left, top, right, bottom), margins)
        if not any(margins):
            raise ValueError("Nothing to do: all four margins are zero.")
        crop, grow = geometry.split_crop_grow(margins)
        kept_w, kept_h = width - crop[0] - crop[2], height - crop[1] - crop[3]
        if not any(grow):  # crop only: nothing to generate
            self.log.info("expand: crop only %s", crop)
            image.undo_group_start()
            try:
                self.io.resize_canvas(image, kept_w, kept_h, -crop[0], -crop[1])
            finally:
                image.undo_group_end()
            return []
        entry, model = self.resolve_model(model_choice)
        rounds = geometry.expand_rounds(kept_w, kept_h, *grow)
        canvases = geometry.round_canvases(kept_w, kept_h, rounds)
        tracker = OverallProgress([w * h for w, h in canvases])
        self.log.info("expand: margins=%s in %d round(s): %s", margins, len(rounds), rounds)
        runner = self.runner_cls(
            self,
            "Generative Expand",
            model_display_name(entry.name) if entry else model.checkpoint,
            interactive=self.interactive,
        )
        layers: list = []
        image.undo_group_start()
        try:
            for index, round_margins in enumerate(rounds):
                tracker.begin(index)
                if index:
                    runner.next_round()
                count = variations if index == len(rounds) - 1 else 1
                this_crop = crop if index == 0 else (0, 0, 0, 0)
                layers = self._expand_round(
                    image, prompt, negative, round_margins, this_crop, count, seed, entry, model, runner, tracker
                )
            runner.done(f"Canvas is now {image.get_width()} x {image.get_height()}")
        except BaseException as err:
            runner.fail(err)
            raise
        finally:
            image.undo_group_end()
        return layers

    def _expand_round(
        self,
        image,
        prompt: str,
        negative: str,
        margins: tuple[int, int, int, int],
        crop: tuple[int, int, int, int],
        variations: int,
        seed: int,
        entry: cat.ModelEntry | None,
        model: ModelSpec,
        runner: Any,
        tracker: OverallProgress,
    ) -> list:
        """One expand pass: all margins at once. ``crop`` (left, top, right,
        bottom) is cut off in the same canvas resize and is not part of the context."""
        left, top, right, bottom = margins
        old_width, old_height = image.get_width(), image.get_height()
        profile = self.profile_for(model)
        plan = geometry.plan_expand(
            image.get_width() - crop[0] - crop[2],
            image.get_height() - crop[1] - crop[3],
            left,
            top,
            right,
            bottom,
            profile,
        )
        ws = geometry.working_size(plan.context.width, plan.context.height, profile)
        mp = geometry.mask_params(
            plan.fill_rect,
            ws.scale,
            self.settings.grow_percent,
            self.settings.feather_percent,
            self.settings.grow,
            self.settings.feather,
        )
        if model.prompt_free:
            prompt, negative = "", ""
        else:
            prompt, negative = workflow.expand_prompts(prompt, negative)
        sampling = self.sampling(model, variations, seed, 1.0)
        sampling = replace(sampling, cfg=workflow.expand_cfg(model.arch, sampling.cfg))
        self.log.info(
            "expand: margins=%s canvas=%dx%d context=%s working=%dx%d model=%s prompt=%s cfg=%.1f",
            (left, top, right, bottom),
            plan.new_width,
            plan.new_height,
            plan.context,
            ws.width,
            ws.height,
            entry.id if entry else model.checkpoint,
            "user" if prompt else "continue the scene",
            sampling.cfg,
        )
        # Read the context from the unresized image (new-canvas coordinates shifted
        # back by the offset; pixels outside the image come back transparent) so a
        # backend failure leaves the image untouched. The canvas grows only on success.
        source_rect = plan.context.translate(crop[0] - plan.offset_x, crop[1] - plan.offset_y)
        image_png, image_rgba = self.io.export_region(image, source_rect, transparent_outside=True)
        mask_pixels = geometry.mask_for_expand(plan)
        mask_png = self.io.mask_png_from_bytes(plan.context, mask_pixels)
        request = InpaintRequest(
            image_png=image_png,
            mask_png=mask_png,
            width=plan.context.width,
            height=plan.context.height,
            work_width=ws.width,
            work_height=ws.height,
            prompt=prompt,
            negative=negative,
            model=model,
            sampling=sampling,
            mode="expand",
            grow=mp.grow_work,
            feather=mp.blur_work,
            prefill=True,
        )
        result = runner.run(
            lambda progress, preview, cancel: self.backend(progress, cancel).outpaint(
                request, tracker.wrap(progress), preview, cancel
            )
        )
        self.server.touch()
        self._after_generation(result)
        image.undo_group_start()
        try:
            self.io.resize_canvas(
                image, plan.new_width, plan.new_height, plan.offset_x - crop[0], plan.offset_y - crop[1]
            )
            layers = self.io.insert_results(
                image,
                result.images,
                plan.context,
                _layer_name("Generative Expand", prompt),
                float(mp.feather_image),
                self.paths.temp,
                mask_pixels=mask_pixels,
            )
        finally:
            image.undo_group_end()
        if self.settings.seam_correction and not model.prompt_free:
            # Where the old image's pixels are, in context coordinates (the crop is not part of it).
            old_rect = (
                crop[0] - source_rect.x,
                crop[1] - source_rect.y,
                old_width - crop[2] - source_rect.x,
                old_height - crop[3] - source_rect.y,
            )
            self._correct_seams(image, layers, plan.context, image_rgba, mask_pixels, 0, old_rect, "expand")
        self.log.info("expand: round done, seed=%s, %d layer(s)", result.seed, len(layers))
        return layers

    def text_to_image(
        self,
        image,
        prompt: str,
        negative: str,
        width: int,
        height: int,
        variations: int,
        seed: int,
        model_choice: str,
        target: str,
    ) -> None:
        if image is not None and target == "layer":
            self._check_image(image)
        entry, model = self.resolve_model(model_choice)
        if entry is not None and "txt2img" not in entry.capabilities:
            raise ModelMissing(
                f"'{model_display_name(entry.name)}' does not support Text to Image; pick an SD 1.5 or SDXL model"
            )
        profile = self.profile_for(model)
        origin = Rect(0, 0, 0, 0)
        if image is not None and target == "layer":
            selection = self.io.selection_rect(image)
            if selection is not None and (width <= 0 or height <= 0):
                origin = selection
                width, height = selection.width, selection.height
        if width <= 0 or height <= 0:
            width, height = self.settings.txt2img_width, self.settings.txt2img_height
        ws = geometry.working_size(width, height, profile)
        request = Txt2ImgRequest(
            ws.width, ws.height, prompt, negative, model, self.sampling(model, variations, seed, 1.0)
        )
        self.log.info("txt2img: %dx%d model=%s target=%s", ws.width, ws.height, model.checkpoint, target)
        runner = self.runner_cls(
            self,
            "Text to Image",
            model_display_name(entry.name) if entry else model.checkpoint,
            interactive=self.interactive,
        )
        try:
            result = runner.run(
                lambda progress, preview, cancel: self.backend(progress, cancel).txt2img(
                    request, progress, preview, cancel
                )
            )
            self.server.touch()
            name = _layer_name("Text to Image", prompt)
            if image is None or target == "image":
                self.io.new_image_from_pngs(result.images, name, self.paths.temp)
            else:
                # generated at the model's working size; the layer gets the requested size
                self.io.insert_results(
                    image,
                    result.images,
                    Rect(origin.x, origin.y, width, height),
                    name,
                    0.0,
                    self.paths.temp,
                    use_selection_mask=False,
                    scale_to_origin=True,
                )
        except BaseException as err:
            runner.fail(err)
            raise
        runner.done("Done")
        self.log.info("txt2img: done, seed=%s", result.seed)

    def _check_image(self, image) -> None:
        if self.io.is_indexed(image):
            self.log.info("refused: indexed image")
            raise UnsupportedImage(INDEXED_MESSAGE)

    def _debug_dump(self, kind: str, request: InpaintRequest, result, rect: Rect) -> None:
        """With debug on (settings or OPENFILL_DEBUG_DUMP=1) keep the exact bytes exchanged
        with the backend next to a JSON description, for bug reports."""
        if not (self.settings.debug or os.environ.get("OPENFILL_DEBUG_DUMP") == "1"):
            return
        try:
            folder = self.paths.temp / f"debug-{kind}-{time.strftime('%Y%m%d-%H%M%S')}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "sent-image.png").write_bytes(request.image_png)
            (folder / "sent-mask.png").write_bytes(request.mask_png)
            for i, data in enumerate(result.images):
                (folder / f"result-{i}.png").write_bytes(data)
            info = {
                "rect": [rect.x, rect.y, rect.width, rect.height],
                "work": [request.work_width, request.work_height],
                "mode": request.mode,
                "grow": request.grow,
                "feather": request.feather,
                "prefill": request.prefill,
                "prompt": request.prompt,
                "negative": request.negative,
                "model": request.model.__dict__,
                "sampling": request.sampling.__dict__,
                "seed": result.seed,
                "layer_feather": self.settings.feather,
                "graph_classes": sorted({n["class_type"] for n in (result.info.get("graph") or {}).values()}),
            }
            (folder / "request.json").write_text(json.dumps(info, indent=2, default=str), encoding="utf-8")
            self.log.info("debug dump: %s", folder)
        except Exception as err:  # noqa: BLE001
            self.log.warning("debug dump failed: %s", err)

    def check_backend(self) -> HealthStatus:
        try:
            backend = self.backend(self.io.GimpProgress("Checking the AI backend"))
        except BackendError as err:
            return HealthStatus(False, str(err))
        health = backend.health()
        self.refresh_models_cache(health)
        return health

    def stop_backend(self) -> str:
        state = self.server.read_state()
        if not state:
            return "No managed backend is running."
        if self.server.request_stop():
            return f"Asked the AI backend (pid {state.get('server_pid')}) to shut down."
        return "Could not write the stop request."


def _open_folder(path: Path) -> None:
    """Show ``path`` in the system file manager (created first if missing)."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
