# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Backend interface shared by the ComfyUI and AUTOMATIC1111 implementations.

The GIMP side only ever talks to a :class:`Backend`; it never knows which
server is behind it. Requests carry already-encoded PNG bytes so the backend
modules stay free of GIMP imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

ProgressCallback = Callable[[float, str], None]
PreviewCallback = Callable[[bytes], None]  # raw ComfyUI binary frame (see progress.parse_preview_frame)
CancelCallback = Callable[[], bool]  # polled; True aborts the running job


class BackendError(RuntimeError):
    """Base class for everything the backend can report to the user."""


class BackendUnavailable(BackendError):
    """The server is not running, cannot be reached or failed to start."""


class ModelMissing(BackendError):
    """A checkpoint or patch file the workflow needs is not on the server."""


class OutOfMemory(BackendError):
    """The GPU ran out of memory while generating."""


class UnsupportedGpu(BackendError):
    """ComfyUI's PyTorch has no code for this graphics card (for example a GTX 10 card with a CUDA 13 build)."""


class GenerationTimeout(BackendError):
    """The request exceeded the configured timeout."""


class GenerationCancelled(BackendError):
    """The user or the plug-in cancelled the request."""


class WorkflowRejected(BackendError):
    """The server refused the submitted workflow (invalid inputs)."""


@dataclass
class Sampling:
    """Sampler settings shared by all backends."""

    steps: int = 25
    cfg: float = 7.0
    sampler: str = "dpmpp_2m"
    scheduler: str = "karras"
    denoise: float = 1.0
    seed: int = -1  # negative = random
    batch: int = 1  # number of variations


@dataclass
class ModelSpec:
    """The model files a request runs with (file names as the server lists them)."""

    checkpoint: str = ""
    arch: str = "sd15"  # sd15 | sdxl | flux | qwen_image | inpaint_model
    fooocus_head: str | None = None  # SDXL only
    fooocus_patch: str | None = None  # SDXL only
    template: str = ""  # catalog template id; "" = derive from arch
    unet: str = ""  # diffusion model file (FLUX / Qwen, safetensors or gguf)
    clip1: str = ""  # text encoder 1 (T5 / Qwen2.5-VL)
    clip2: str = ""  # text encoder 2 (CLIP-L)
    vae: str = ""  # separate VAE file
    inpaint_model: str = ""  # LaMa / MAT file for prompt-free removal
    lora: str = ""  # LoRA applied to the diffusion model (the FLUX Fill object-removal LoRA)

    @property
    def uses_fooocus(self) -> bool:
        """SDXL with both Fooocus inpaint files configured."""
        return self.arch == "sdxl" and bool(self.fooocus_head and self.fooocus_patch)

    @property
    def effective_template(self) -> str:
        """Workflow template id: the explicit one, else derived from ``arch``."""
        if self.template:
            return self.template
        return {
            "sdxl": "sdxl_fooocus",
            "flux": "flux_fill",
            "qwen_image": "qwen_edit",
            "inpaint_model": "inpaint_model",
        }.get(self.arch, "sd15_inpaint")

    @property
    def prompt_free(self) -> bool:
        """LaMa / MAT style models that ignore the prompt."""
        return self.effective_template == "inpaint_model"

    @property
    def eraser(self) -> bool:
        """Erase-trained diffusion models (RORem, the FLUX Fill removal LoRA): every
        run with them is an object removal, with their own prompt."""
        return self.effective_template in ("sdxl_eraser", "flux_eraser")

    @property
    def native_lama(self) -> bool:
        """LaMa, which OpenFill's own node runs at the image's own resolution (MAT is
        fixed at 512 px and keeps comfyui-inpaint-nodes' node)."""
        return self.prompt_free and "lama" in self.inpaint_model.lower()


@dataclass
class InpaintRequest:
    """A fill or expand job: encoded crop and mask plus generation parameters."""

    image_png: bytes  # context crop, RGB or RGBA PNG
    mask_png: bytes  # 8-bit gray PNG, 255 = generate here
    width: int  # crop size in image pixels
    height: int
    work_width: int  # resolution the model samples at
    work_height: int
    prompt: str
    negative: str
    model: ModelSpec
    sampling: Sampling = field(default_factory=Sampling)
    mode: str = "fill"  # "fill" or "expand"
    grow: int = 16  # mask dilation in working pixels
    feather: int = 24  # mask blur in working pixels
    prefill: bool = True  # pre-fill masked area before sampling
    prefill_mode: str = "navier-stokes"  # border pre-fill algorithm: navier-stokes | telea | neutral
    prefill_style: str = "blur"  # "blur": soften existing content (fill); "border": paint the area
    # from its surroundings first (expand, object removal)
    color_match: float = 1.0  # strength of the post color correction (0 = off)

    @property
    def needs_scaling(self) -> bool:
        """The model works at a different resolution than the crop."""
        return (self.work_width, self.work_height) != (self.width, self.height)


@dataclass
class Txt2ImgRequest:
    """A text-to-image job."""

    width: int
    height: int
    prompt: str
    negative: str
    model: ModelSpec
    sampling: Sampling = field(default_factory=Sampling)


@dataclass
class GenerationResult:
    """Images returned by a backend."""

    images: list[bytes]  # PNG bytes, one per variation
    seed: int
    info: dict = field(default_factory=dict)


@dataclass
class HealthStatus:
    """What a health check found out about the server."""

    ok: bool
    detail: str = ""
    version: str = ""
    checkpoints: list[str] = field(default_factory=list)
    inpaint_nodes: bool = False
    fooocus_heads: list[str] = field(default_factory=list)
    fooocus_patches: list[str] = field(default_factory=list)
    vram_total: int = 0
    vram_free: int = 0
    device: str = ""  # what ComfyUI computes on, e.g. "NVIDIA GeForce RTX 3060 (cuda, 12.0 GB)"
    device_kind: str = ""  # gpu, cpu or unknown (hardware.backend_device)


@dataclass
class Progress:
    """Snapshot of the running job."""

    fraction: float = 0.0
    text: str = ""
    running: bool = False


class Backend(ABC):
    """Abstract generation backend."""

    name: str = "abstract"

    @abstractmethod
    def health(self) -> HealthStatus:
        """Return server status and the models it can offer."""

    @abstractmethod
    def inpaint(
        self,
        request: InpaintRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Fill the masked part of ``request.image_png``."""

    def outpaint(
        self,
        request: InpaintRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Expand: same as inpaint with ``mode='expand'`` semantics."""
        request.mode = "expand"
        return self.inpaint(request, progress, preview, cancel)

    @abstractmethod
    def txt2img(
        self,
        request: Txt2ImgRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Generate from text only."""

    @abstractmethod
    def progress(self) -> Progress:
        """Current progress of the running job, if any."""

    @abstractmethod
    def cancel(self) -> None:
        """Interrupt the running job."""


SETTINGS_MENU = "Filters > OpenFill > Generative Fill Settings"

# Words that show a message already tells the user what to do next.
NEXT_STEP_WORDS = (
    "settings",
    "download",
    "select",
    "try",
    "close",
    "install",
    "start",
    "press",
    "run ",
    "convert",
    "open ",
    "check",
)


def has_next_step(text: str) -> bool:
    """True when ``text`` names something the user can do (see :data:`NEXT_STEP_WORDS`)."""
    lowered = text.lower()
    return any(word in lowered for word in NEXT_STEP_WORDS)


def _with_next_step(text: str, step: str) -> str:
    """``text`` plus ``step`` unless the text already says what to do."""
    text = text.strip()
    if has_next_step(text):
        return text
    if text and text[-1] not in ".!?":
        text += "."
    return f"{text} {step}".strip()


def _first_line(text: str, limit: int = 200) -> str:
    """The first line of an error text, shortened: enough to search for, short enough for a dialog."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def describe_error(error: BaseException) -> str:
    """Short, user-facing description for a backend failure: plain words plus a next step.

    Errors raised with a complete user message (``NothingSelected``, ``UnsupportedImage``
    and other :class:`BackendError` subclasses without a branch here) are shown as they are.
    """
    text = str(error).strip()
    if isinstance(error, BackendUnavailable):
        return _with_next_step(
            f"The AI backend is not available: {text}",
            f"Start ComfyUI, or check the ComfyUI folder and URL in {SETTINGS_MENU}, then try again.",
        )
    if isinstance(error, ModelMissing):
        return _with_next_step(
            f"A model file is missing on the backend: {text}",
            f"Open {SETTINGS_MENU} > Models and press Download for the model you use, then try again.",
        )
    if isinstance(error, OutOfMemory):
        return (
            "The GPU ran out of memory. Try a smaller selection, fewer variations, or the SD 1.5 model "
            "instead of SDXL, and close other programs that use the graphics card."
            + (f" (ComfyUI said: {_first_line(text)})" if text else "")
        )
    if isinstance(error, UnsupportedGpu):
        return (
            "ComfyUI cannot use this graphics card: its PyTorch was made for newer cards. NVIDIA cards older "
            "than the RTX 20 series (GTX 10 and earlier) need ComfyUI's build for older cards, "
            "ComfyUI_windows_portable_nvidia_cu126.7z on the ComfyUI releases page. Unpack it and choose its "
            f"folder in {SETTINGS_MENU}." + (f" (ComfyUI said: {_first_line(text)})" if text else "")
        )
    if isinstance(error, GenerationTimeout):
        return _with_next_step(
            f"The request timed out ({text})." if text else "The request timed out.",
            'Try a smaller selection or fewer variations, or raise "Generation time limit" in '
            f"{SETTINGS_MENU} > Connection and troubleshooting (0 means it is estimated per request).",
        )
    if isinstance(error, GenerationCancelled):
        return "Generation was cancelled. Run the command again to try once more."
    if isinstance(error, WorkflowRejected):
        return _with_next_step(
            f"ComfyUI rejected the request: {text}",
            f"Make sure the model and its custom nodes are installed ({SETTINGS_MENU} > Models), then try again.",
        )
    if isinstance(error, BackendError):
        return _with_next_step(text or "The AI backend reported an error.", f"Check {SETTINGS_MENU} and try again.")
    return _with_next_step(f"{type(error).__name__}: {error}", "Try again; if it keeps failing, see openfill.log.")
