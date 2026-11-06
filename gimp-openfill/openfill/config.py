# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Settings persisted as JSON in the GIMP profile (``<profile>/openfill``).

No GIMP imports: the caller passes the profile directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import ModelSpec

CONFIG_DIRNAME = "openfill"
LEGACY_DIRNAME = "stable-boy"  # folder name before the rename (settings are copied once)
CONFIG_FILENAME = "config.json"
STATE_FILENAME = "server-state.json"
LOG_FILENAME = "openfill.log"
SERVER_LOG_FILENAME = "comfyui.log"
MODELS_CACHE_FILENAME = "models-cache.json"
HUB_CACHE_FILENAME = "hub-cache.json"
HUB_SELECTED_FILENAME = "hub-selected.json"


@dataclass(frozen=True)
class BackendChoice:
    """One entry of the "where images are made" choice. Add a backend here and every
    settings screen (GIMP and PhotoCraft) offers it."""

    id: str  # the value of Settings.backend
    label: str  # in menus
    online: bool  # sends the user's pictures to another computer: screens say so
    short: str = ""  # in narrow switches (OpenFill for PhotoCraft)
    photocraft: bool = True  # offered by OpenFill for PhotoCraft too


BACKENDS = (
    BackendChoice("comfyui", "This computer (ComfyUI)", False, "This computer"),
    BackendChoice("a1111", "AUTOMATIC1111 WebUI (started by you with --api)", False, "AUTOMATIC1111", photocraft=False),
    BackendChoice("cloud", "Cloud service (paid, sends your image online)", True, "Cloud service"),
)


def backend_choice(backend_id: str) -> BackendChoice | None:
    return next((b for b in BACKENDS if b.id == backend_id), None)


def default_comfy_dir() -> str:
    """The ComfyUI folder assumed when the config does not name one."""
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local:
        return str(Path(local) / "OpenFill" / "ComfyUI" / "ComfyUI_windows_portable")
    return str(Path.home() / "ComfyUI")


@dataclass
class Settings:
    """User settings stored in ``config.json``.

    ``hf_token`` and ``cloud_api_key`` are secrets: they are left out of ``repr()``
    so they cannot end up in a log line or a traceback, and :meth:`save` makes the
    file readable by its owner only on POSIX systems.
    """

    backend: str = "comfyui"  # one of BACKENDS: "comfyui", "a1111" or "cloud"
    comfy_dir: str = field(default_factory=default_comfy_dir)
    url: str = "http://127.0.0.1:8188"  # ComfyUI (or A1111) base URL
    a1111_url: str = "http://127.0.0.1:7860"
    autostart: bool = True  # start ComfyUI when needed
    idle_timeout_minutes: int = 20  # 0 = keep running until GIMP exits
    startup_timeout: int = 240  # seconds to wait for the server
    request_timeout: int = 0  # 0 = estimate per request
    extra_server_args: str = ""  # appended to the ComfyUI command
    cpu_comfy_dir: str = ""  # the ComfyUI folder Download ComfyUI added --cpu for (no NVIDIA card)
    checkpoint: str = "sd-v1-5-inpainting.ckpt"  # default (SD 1.5) model
    sdxl_checkpoint: str = "sd_xl_base_1.0.safetensors"
    fooocus_head: str = "fooocus_inpaint_head.pth"
    fooocus_patch: str = "inpaint_v26.fooocus.patch"
    quality: str = "fast"  # "fast" (SD 1.5) or "quality" (SDXL)
    steps: int = 25
    cfg: float = 7.0
    sampler: str = "dpmpp_2m"
    scheduler: str = "karras"
    denoise: float = 1.0
    grow: int = 48  # mask dilation cap (px); actual = grow_percent of selection
    feather: int = 24  # layer mask feather cap (px); actual = feather_percent
    grow_percent: float = 15.0  # dilation as percent of the selection's larger side (covers object rims)
    feather_percent: float = 6.0  # layer mask feather as percent of the larger side
    prefill: bool = True
    # Second pass at full resolution over the regenerated area when the fill
    # was worked below 1:1 (SD 1.5 and SDXL tiers), at this denoise. Off: in
    # the A/B it made fills softer (SD 1.5) or changed their content (SDXL).
    refine: bool = False
    refine_denoise: float = 0.4
    # Generative Expand worked below 1:1 (SD 1.5 and SDXL): a second pass at the image's own
    # resolution over the new area, in windows, at ``expand_detail_denoise``. Off until an
    # A/B on real photos shows it helps (it is untested on hardware); turn on in config.json.
    expand_detail: bool = False
    expand_detail_denoise: float = 0.4
    # Tiled fills: below 1.0 the whole context is generated first in one pass
    # (layout) and the tiles add detail to it at this denoise.
    tile_denoise: float = 0.65
    layout_min_scale: float = 0.45
    # Prompt-free removal (LaMa/MAT) of a context larger than this many px runs
    # in overlapping windows of this size at 1:1 (0 = one downscaled pass).
    removal_tile: int = 512
    removal_tile_overlap: int = 128
    # Add a scene description of the context (light, colour, grain) to prompted fills.
    scene_caption: bool = False
    # Add noise to a fill that is cleaner than the grain around it.
    grain_match: bool = True
    # Remove the faint outline a fill leaves on smooth areas (quality.seam_field).
    seam_correction: bool = True
    # Order several variations best first by cheap quality metrics.
    rank_variations: bool = True
    max_side_sd15: int = 768
    max_side_sdxl: int = 1024
    txt2img_width: int = 768
    txt2img_height: int = 512
    debug: bool = False
    active_model: str = ""  # catalog id set active by hand; "" = the automatic default (catalog.auto_active)
    hf_token: str = field(default="", repr=False)  # Hugging Face access token for gated files (local only)
    progress_window: bool = True  # floating progress/preview window
    progress_window_x: int = -1  # last position; -1 = top-right of the image window
    progress_window_y: int = -1
    prompt_history: str = ""  # JSON list of recent prompts, newest first (dialogs)
    hub_search: bool = True  # Models tab: also list current models from Hugging Face and Civitai (read-only API calls)
    hub_civitai: bool = True  # include Civitai in that search (Hugging Face only when False)
    # Cloud service (backend "cloud", see backends/cloud.py and docs/cloud.md). Only used when picked.
    cloud_provider: str = "openrouter"  # "openrouter" or "openai" (any OpenAI-compatible images API)
    cloud_url: str = ""  # API address; "" = the provider's own (https://openrouter.ai/api/v1)
    cloud_model: str = ""  # "" = the provider's default (OpenRouter: google/gemini-2.5-flash-image)
    cloud_api: str = "images"  # OpenRouter only: "images" (/images) or "chat" (/chat/completions)
    cloud_api_key: str = field(default="", repr=False)  # secret, stored only in this file
    # The service and address (scheme, host, port) the key was entered for (backends.cloud.key_scope).
    # The key is only ever sent where this still matches: changing either needs the key again.
    cloud_key_for: str = ""
    # Not persisted: why the last :meth:`load` fell back to defaults (None when it did not).
    load_problem: str | None = field(default=None, repr=False, compare=False, metadata={"persist": False})
    # Not persisted: a damaged settings file that could not be moved aside; :meth:`save` refuses to overwrite it.
    _protected_path: str | None = field(default=None, repr=False, compare=False, metadata={"persist": False})

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path, backup: bool = True) -> Settings:
        """Read ``path``; a missing file, unknown keys and mistyped values fall back to defaults.

        A file that exists but is not a JSON object (damaged, half written, edited
        by hand) is moved aside to ``config.json.corrupt-<YYYYmmdd-HHMMSS>`` so a
        later :meth:`save` cannot destroy the user's settings and token, and
        :attr:`load_problem` says so in plain words. ``backup=False`` (for
        processes other than the plug-in) only reads and never moves anything.
        """
        settings = cls()
        path = Path(path)
        try:  # utf-8-sig: tolerate the BOM that PowerShell's Set-Content adds
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return settings
        except OSError as err:
            settings.load_problem = (
                f"Your settings file '{path}' could not be opened ({err.strerror or err}); defaults are in use "
                "for now and will not be saved over it. Check the file's permissions, then restart GIMP."
            )
            settings._protected_path = str(path)
            return settings
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if backup:
                settings._move_aside(path)
            else:
                settings._protected_path = str(path)
            return settings
        known = {f.name for f in fields(cls) if f.metadata.get("persist", True)}
        for key, value in data.items():
            if key in known:
                try:
                    setattr(settings, key, _coerce(getattr(settings, key), value))
                except (TypeError, ValueError):
                    pass
        return settings

    def _move_aside(self, path: Path) -> None:
        """Rename an unreadable settings file to a fresh ``.corrupt-<timestamp>`` name and record why."""
        target = corrupt_backup_path(path)
        try:
            os.rename(path, target)  # never replaces: the name was checked free, and rename fails on Windows if taken
        except OSError as err:
            self._protected_path = str(path)
            self.load_problem = (
                f"Your settings file '{path}' could not be read, and moving it aside failed "
                f"({err.strerror or err}); defaults are in use and the file was left untouched. "
                "Fix or delete the file, then open Filters > OpenFill > Generative Fill Settings again."
            )
            return
        self.load_problem = (
            f"Your settings file could not be read and was moved aside to '{target}'; defaults are in use. "
            "Open Filters > OpenFill > Generative Fill Settings to set the ComfyUI folder and your options again."
        )

    def drop_stale_cpu_flag(self, setup_dir: str | Path | None = None) -> bool:
        """Remove the ``--cpu`` that Download ComfyUI added for its own folder once another
        ComfyUI folder is in use; True when the settings changed.

        Settings from before ``cpu_comfy_dir`` existed name no folder: then the folder
        Download ComfyUI installs to (``setup_dir``) is taken as the one, if it exists. A
        ``--cpu`` typed by the user for the folder in use is kept.
        """
        args = self.extra_server_args.split()
        if "--cpu" not in args:
            return False
        origin = self.cpu_comfy_dir or (str(setup_dir) if setup_dir and Path(setup_dir).is_dir() else "")
        if not origin or _same_dir(origin, self.comfy_dir):
            return False
        self.extra_server_args = " ".join(a for a in args if a != "--cpu")
        self.cpu_comfy_dir = ""
        return True

    def save(self, path: Path) -> None:
        """Write atomically (temporary file, then rename); owner-only permissions on POSIX."""
        path = Path(path)
        if self._protected_path and os.path.abspath(path) == os.path.abspath(self._protected_path):
            raise OSError(f"not saving: '{path}' could not be read and was not backed up, so it is kept as it is")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.metadata.get("persist", True)}
        text = json.dumps(data, indent=2) + "\n"
        # Created owner-only from the start (the file holds the Hugging Face token and the
        # cloud API key), so no other user can read it even for a moment.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if os.name == "posix":
            os.chmod(tmp, 0o600)  # a .tmp left over from before keeps its old mode otherwise
        os.replace(tmp, path)

    # derived -----------------------------------------------------------

    @property
    def active_url(self) -> str:
        """Base URL of the selected backend."""
        if self.backend == "cloud":
            from .backends.cloud import base_url

            return base_url(self.cloud_provider, self.cloud_url)
        return self.a1111_url if self.backend == "a1111" else self.url

    @property
    def uses_cloud(self) -> bool:
        """The cloud service is the backend: pictures leave this computer."""
        return self.backend == "cloud"

    def set_cloud(self, provider: str, url: str, model: str, key: str = "") -> None:
        """Apply the cloud fields of a settings screen. A new ``key`` is bound to this service
        and address; an empty one leaves the saved key as it is, bound where it was entered."""
        from .backends.cloud import key_scope

        self.cloud_provider, self.cloud_url, self.cloud_model = provider, url.strip(), model.strip()
        if key.strip():
            self.cloud_api_key = key.strip()
            self.cloud_key_for = key_scope(provider, url)

    @property
    def cloud_key_usable(self) -> bool:
        """A key is saved for exactly the service and address now set."""
        from .backends.cloud import key_scope

        return bool(self.cloud_api_key) and self.cloud_key_for == key_scope(self.cloud_provider, self.cloud_url)

    def cloud_key_problem(self) -> str:
        """Why the saved key is not used ("" when it is, or when none is saved)."""
        if not self.cloud_api_key or self.cloud_key_usable:
            return ""
        from .backends.cloud import destination

        if not self.cloud_key_for:  # saved by a test build before keys were tied to their service
            return (
                "Please paste your API key again in the cloud settings: OpenFill now remembers which "
                "service each key is for, and sends it only there."
            )

        return (
            f"The saved API key was entered for another service or address, so it is not sent to "
            f"{destination(self.cloud_provider, self.cloud_url)}. Paste the API key for this service in the "
            "cloud settings, then try again."
        )

    def cloud_note(self, what: str = "image") -> str:
        """The "Cloud: your image is sent to ..." line; "" when the backend is not the cloud."""
        if not self.uses_cloud:
            return ""
        from .backends.cloud import cloud_note

        return cloud_note(self.cloud_provider, self.cloud_url, what)

    def model_for(self, quality: str | None = None) -> ModelSpec:
        """Return a ``ModelSpec`` for the requested quality tier."""
        from .backend import ModelSpec

        tier = quality or self.quality
        if tier == "quality" and self.sdxl_checkpoint:
            return ModelSpec(self.sdxl_checkpoint, "sdxl", self.fooocus_head or None, self.fooocus_patch or None)
        return ModelSpec(self.checkpoint, "sd15")


def corrupt_backup_path(path: Path, now: float | None = None) -> Path:
    """A name next to ``path`` that does not exist yet: ``<name>.corrupt-<YYYYmmdd-HHMMSS>[-N]``."""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    return candidate


def _coerce(current: Any, value: Any) -> Any:
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return str(value)


@dataclass
class Paths:
    """All files the plug-in owns inside the GIMP profile."""

    base: Path

    @classmethod
    def for_profile(cls, profile_dir: str | Path) -> Paths:
        """Paths under ``<profile_dir>/openfill``. The first run after the
        rename copies the settings of the old ``stable-boy`` folder over."""
        base = Path(profile_dir) / CONFIG_DIRNAME
        legacy = Path(profile_dir) / LEGACY_DIRNAME
        if not base.exists() and legacy.is_dir():
            try:
                base.mkdir(parents=True)
                for name in (CONFIG_FILENAME, HUB_SELECTED_FILENAME):
                    if (legacy / name).is_file():
                        shutil.copy2(legacy / name, base / name)
            except OSError:
                pass  # start with defaults; the old folder stays untouched
        return cls(base)

    @property
    def config(self) -> Path:
        """Settings file (holds the Hugging Face token)."""
        return self.base / CONFIG_FILENAME

    @property
    def state(self) -> Path:
        """Supervisor state: pids, URL, status."""
        return self.base / STATE_FILENAME

    @property
    def log(self) -> Path:
        """Plug-in and supervisor log."""
        return self.base / LOG_FILENAME

    @property
    def server_log(self) -> Path:
        """ComfyUI stdout and stderr."""
        return self.base / SERVER_LOG_FILENAME

    @property
    def models_cache(self) -> Path:
        """Model files the server listed last time (no secrets)."""
        return self.base / MODELS_CACHE_FILENAME

    @property
    def hub_cache(self) -> Path:
        """Hugging Face / Civitai search results, kept for 24 hours (no secrets)."""
        return self.base / HUB_CACHE_FILENAME

    @property
    def hub_selected(self) -> Path:
        """Models from the live search the user chose to download (re-validated on every read)."""
        return self.base / HUB_SELECTED_FILENAME

    @property
    def temp(self) -> Path:
        """Scratch folder (debug dumps)."""
        return self.base / "tmp"

    def ensure(self) -> Paths:
        """Create the folders; returns ``self``."""
        self.base.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)
        return self


def _same_dir(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except (TypeError, ValueError):
        return False


def comfy_layout(comfy_dir: str | Path) -> dict[str, Path] | None:
    """Locate python + main.py for a ComfyUI install; None if not found.

    Supports the official Windows portable (``python_embeded/python.exe`` +
    ``ComfyUI/main.py``), a plain git checkout with a ``venv``/``.venv``,
    and a checkout where the interpreter is given explicitly via PATH.
    """
    root = Path(comfy_dir)
    candidates: list[tuple[Path, Path, Path]] = []
    if (root / "python_embeded" / "python.exe").exists() and (root / "ComfyUI" / "main.py").exists():
        candidates.append((root / "python_embeded" / "python.exe", root / "ComfyUI" / "main.py", root))
    main = root / "main.py"
    if main.exists():
        for venv in ("venv", ".venv", "env"):
            for py in ("Scripts/python.exe", "bin/python3", "bin/python"):
                exe = root / venv / py
                if exe.exists():
                    candidates.append((exe, main, root))
        # The interpreter running this code, when it can be started from here: not on macOS
        # when it is GIMP's own Python (GIMP.app has a launch constraint and no PyTorch).
        if not (sys.platform == "darwin" and in_app_bundle(sys.executable)):
            candidates.append((Path(sys.executable), main, root))
    if not candidates:
        return None
    python, main, cwd = candidates[0]
    return {"python": python, "main": main, "cwd": cwd}


def windows_path_problem(python: Path, platform: str | None = None) -> str:
    """Check the path to a deeply nested dependency before Windows startup.

    Keep the user's spelling: resolving a short junction defeats its purpose.
    This is a conservative compatibility check, not a claim that every Windows
    installation enforces MAX_PATH.
    """
    if (platform or sys.platform) != "win32":
        return ""
    site = python.parent
    if site.name.lower() == "scripts":
        site = site.parent
    dependency = (
        site
        / "Lib/site-packages/transformers/models/audio_spectrogram_transformer/feature_extraction_audio_spectrogram_transformer.py"
    )
    length = len(str(dependency.absolute()).encode("utf-16-le")) // 2
    if length < 260:
        return ""
    return (
        f"The ComfyUI install path is too long for reliable Windows startup ({length} characters "
        "for a Transformers file; the Windows compatibility limit is 259). "
        "Use a shorter folder such as C:\\AI\\ComfyUI, or a short directory junction to the existing "
        "installation, then choose that folder in OpenFill Settings. Keep your models with ComfyUI."
    )


def in_app_bundle(path: str | Path) -> bool:
    """``path`` lies inside a macOS ``.app`` bundle (``GIMP.app/Contents/...``)."""
    return any(part.endswith(".app") for part in Path(path).parts)


def comfy_problem(comfy_dir: str | Path) -> str:
    """Why ``comfy_dir`` is not a ComfyUI OpenFill can start, in plain words ("" when it is)."""
    if not comfy_dir:
        return "No ComfyUI folder is set."
    if comfy_layout(comfy_dir) is not None:
        return ""
    root = Path(comfy_dir)
    if not root.is_dir():
        return f"ComfyUI was not found at '{root}': the folder does not exist."
    if (root / "main.py").is_file():
        return (
            f"ComfyUI was not found at '{root}' in a form OpenFill can start: main.py is there but no venv. "
            "Create one in that folder (python3 -m venv venv, then install ComfyUI's requirements into it)."
        )
    return (
        f"ComfyUI was not found at '{root}': it holds neither main.py with a venv nor the portable build "
        "(python_embeded and ComfyUI)."
    )
