# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""ComfyUI backend: builds graphs, uploads inputs, streams progress and previews."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

from .. import hardware, workflow
from ..backend import (
    Backend,
    BackendUnavailable,
    CancelCallback,
    GenerationCancelled,
    GenerationResult,
    GenerationTimeout,
    HealthStatus,
    InpaintRequest,
    ModelMissing,
    ModelSpec,
    OutOfMemory,
    PreviewCallback,
    Progress,
    ProgressCallback,
    Txt2ImgRequest,
    UnsupportedGpu,
    WorkflowRejected,
)
from ..comfy_api import ComfyApiError, ComfyClient, ComfyExecutionError
from ..geometry import estimate_timeout, seed_or_random

# While the server keeps sending progress, previews or node events, the
# deadline moves to at least this many seconds after the last one: the
# estimate only has to cover a silent stretch, not the whole job.
ACTIVITY_EXTENSION = 180.0
# FLUX and Qwen on a partly offloaded 8 GB card ran 87 to 151 s warm (median
# 112 s); the old 134 to 166 s estimate cut 3 of 31 FLUX runs. Minimum timeout
# for these families (any progress message still extends the deadline).
SLOW_ARCHS = ("flux", "qwen_image")
SLOW_MODEL_FLOOR = 240.0
# Model folders searched for the size of a request's model files.
MODEL_FOLDERS = ("checkpoints", "diffusion_models", "unet", "clip", "text_encoders", "vae", "inpaint")

CONNECTION_LOST = (
    "ComfyUI stopped while it was generating (it crashed or was closed), so the image was not changed. "
    "Run the command again to restart it; if this keeps happening, read comfyui.log in the openfill "
    "folder of your GIMP profile."
)


class ComfyBackend(Backend):
    name = "comfyui"

    def __init__(
        self,
        url: str = "http://127.0.0.1:8188",
        client: ComfyClient | None = None,
        request_timeout: float = 0.0,
        logger=None,
        models_root: Path | str | None = None,
    ) -> None:
        self.url = url
        self.models_root = Path(models_root) if models_root else None
        self.client = client or ComfyClient(url)
        self.request_timeout = request_timeout  # 0 = estimate per request
        self.log = logger
        self._caps: workflow.Capabilities | None = None
        self._progress = Progress()
        self._first_step = 0.0  # monotonic time of the first sampling step of the current job

    # ------------------------------------------------------------------

    def capabilities(self, refresh: bool = False) -> workflow.Capabilities:
        if self._caps is None or refresh:
            info = self.client.object_info("INPAINT_ExpandMask")
            self._caps = workflow.Capabilities(inpaint_nodes=bool(info))
        return self._caps

    def health(self) -> HealthStatus:
        try:
            stats = self.client.system_stats()
        except ComfyApiError as err:
            return HealthStatus(False, str(err))
        devices = stats.get("devices") or [{}]
        version = str(stats.get("system", {}).get("comfyui_version", ""))
        try:
            checkpoints = self.client.combo_options("CheckpointLoaderSimple", "ckpt_name")
            caps = self.capabilities(refresh=True)
            heads = self.client.combo_options("INPAINT_LoadFooocusInpaint", "head") if caps.inpaint_nodes else []
            patches = self.client.combo_options("INPAINT_LoadFooocusInpaint", "patch") if caps.inpaint_nodes else []
        except ComfyApiError as err:
            return HealthStatus(False, f"server answered but node info failed: {err}", version)
        device_kind, device = hardware.backend_device(stats)
        return HealthStatus(
            True,
            f"ComfyUI {version}",
            version,
            checkpoints,
            caps.inpaint_nodes,
            heads,
            patches,
            int(devices[0].get("vram_total", 0)),
            int(devices[0].get("vram_free", 0)),
            device,
            device_kind,
        )

    def progress(self) -> Progress:
        return self._progress

    def cancel(self) -> None:
        for action in (self.client.interrupt, self.client.clear_queue):
            try:
                action()
            except ComfyApiError:
                pass

    # ------------------------------------------------------------------

    def inpaint(
        self,
        request: InpaintRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        caps = self.capabilities()
        if request.model.uses_fooocus and not caps.inpaint_nodes:
            raise ModelMissing("the Fooocus inpaint patch needs comfyui-inpaint-nodes on the server")
        seed = seed_or_random(request.sampling.seed)
        self._report(progress, 0.0, "Uploading selection...")
        started = time.monotonic()
        try:
            image_data = workflow.probe_safe_png(request.image_png)
            mask_data = workflow.probe_safe_png(request.mask_png)
            # Named after the content: the same crop and mask (a re-run with another seed or prompt)
            # get the same file names, so ComfyUI reuses its cached outputs of the nodes that only
            # depend on them (image and mask preparation, VAE encode) instead of running them again.
            job = _content_id(image_data, mask_data)
            image_ref = self.client.upload_image(f"sb-{job}-image.png", image_data)
            mask_ref = self.client.upload_image(f"sb-{job}-mask.png", mask_data)
        except ComfyApiError as err:
            raise BackendUnavailable(f"upload failed: {err}") from err
        if self.log:
            self.log.info(
                "timing: upload image=%d mask=%d bytes in %.3f s",
                len(image_data),
                len(mask_data),
                time.monotonic() - started,
            )
        prefix = f"openfill/{request.mode}-{job}"
        try:
            graph = workflow.build_fill(request, image_ref, mask_ref, seed, caps, filename_prefix=prefix)
        except ValueError as err:
            raise WorkflowRejected(str(err)) from err
        pixels = int(getattr(request, "work_pixels", 0) or request.work_width * request.work_height)
        fallback = False
        try:
            images = self._run(
                graph, request.sampling.steps, request.sampling.batch, pixels, progress, preview, cancel, request.model
            )
        except WorkflowRejected as err:
            # The telea / navier-stokes pre-fill modes need OpenCV on the server.
            # Fall back to the dependency-free neutral fill instead of failing.
            if "cv2" not in str(err) or request.prefill_mode == "neutral":
                raise
            if self.log:
                self.log.warning("server has no OpenCV; retrying with neutral pre-fill")
            request.prefill_mode = "neutral"
            fallback = True
            graph = workflow.build_fill(request, image_ref, mask_ref, seed, caps, filename_prefix=prefix)
            images = self._run(
                graph, request.sampling.steps, request.sampling.batch, pixels, progress, preview, cancel, request.model
            )
        info = {"job": job, "graph": graph, "backend": self.name}
        if fallback:
            info["prefill_fallback"] = "no-opencv"
        return GenerationResult(images, seed, info)

    def txt2img(
        self,
        request: Txt2ImgRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        seed = seed_or_random(request.sampling.seed)
        job = uuid.uuid4().hex[:12]
        try:
            graph = workflow.build_txt2img(request, seed, filename_prefix=f"openfill/txt2img-{job}")
        except ValueError as err:
            raise WorkflowRejected(str(err)) from err
        images = self._run(
            graph,
            request.sampling.steps,
            request.sampling.batch,
            request.width * request.height,
            progress,
            preview,
            cancel,
            request.model,
        )
        return GenerationResult(images, seed, {"job": job, "graph": graph, "backend": self.name})

    # ------------------------------------------------------------------

    def _run(
        self,
        graph: dict,
        steps: int,
        batch: int,
        pixels: int,
        progress: ProgressCallback | None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
        model: ModelSpec | None = None,
    ) -> list[bytes]:
        timeout = self.request_timeout or self._estimate(steps, batch, pixels, model)
        started = time.monotonic()
        deadline = started + timeout
        self._first_step = 0.0
        self._report(progress, 0.02, "Submitting workflow...")
        try:
            prompt_id = self.client.queue_prompt(graph)
        except ComfyApiError as err:
            raise self._translate_queue_error(err, graph) from err
        self._progress = Progress(0.0, "queued", True)
        queued = time.monotonic()
        if self.log:
            self.log.info("queued prompt %s (timeout %.0fs, %d steps x %d)", prompt_id, timeout, steps, batch)
        try:
            entry = self.client.wait_for(
                prompt_id,
                workflow.total_steps(graph),
                deadline,
                self._wrap(progress),
                preview=preview,
                cancel=cancel,
                activity_extension=0.0 if self.request_timeout else ACTIVITY_EXTENSION,
            )
        except ComfyExecutionError as err:
            if err.is_unsupported_gpu:
                raise UnsupportedGpu(str(err)) from err
            if err.is_out_of_memory:
                raise OutOfMemory(str(err)) from err
            raise WorkflowRejected(f"{err.node_type or 'node'}: {err}") from err
        except ComfyApiError as err:
            text = str(err)
            if "cancelled" in text or "interrupted" in text:
                raise GenerationCancelled(text) from err
            if "timed out" in text:
                self.cancel()
                raise GenerationTimeout(f"no result after {time.monotonic() - started:.0f} s") from err
            if self._connection_lost(err):
                raise BackendUnavailable(CONNECTION_LOST) from err
            raise BackendUnavailable(text) from err
        finally:
            self._progress = Progress(1.0, "done", False)
        finished = time.monotonic()
        self._report(progress, 0.99, "Downloading result...")
        try:
            images = self.client.output_images(entry, workflow.OUTPUT_NODE)
        except ComfyApiError as err:
            if self._connection_lost(err):
                raise BackendUnavailable(CONNECTION_LOST) from err
            raise BackendUnavailable(f"could not download the result: {err}") from err
        if self.log:
            first = self._first_step or finished
            self.log.info(
                "timing: queue->first step %.3f s, sampling %.3f s, download %d bytes in %.3f s",
                first - queued,
                finished - first,
                sum(len(i) for i in images),
                time.monotonic() - finished,
            )
        if not images:
            raise WorkflowRejected("the workflow finished without producing an image")
        return images

    def _estimate(self, steps: int, batch: int, pixels: int, model: ModelSpec | None) -> float:
        """Automatic timeout: sampling time plus the load time of the model
        files, with the cold floor when the server's last prompt used other
        model files (or there was none, e.g. right after a start)."""
        names = model_files(model)
        cold = False
        if names:
            try:
                cold = not names <= self.client.last_model_files()
            except (ComfyApiError, AttributeError, TypeError, ValueError):
                cold = True  # unknown: allow for a load
        size = model_bytes(self.models_root, names)
        floor = SLOW_MODEL_FLOOR if model is not None and model.arch in SLOW_ARCHS else 120.0
        timeout = estimate_timeout(steps, batch, pixels, floor=floor, model_bytes=size, cold=cold)
        if self.log:
            self.log.info(
                "timeout estimate %.0f s (model files %.1f GB, %s)", timeout, size / 1e9, "cold" if cold else "warm"
            )
        return timeout

    def _connection_lost(self, err: ComfyApiError) -> bool:
        """A transport failure (no HTTP status) while ComfyUI no longer answers: it stopped mid-job."""
        return not err.status and not self.client.is_alive(timeout=2.0)

    def _wrap(self, progress: ProgressCallback | None) -> ProgressCallback:
        def cb(fraction: float, text: str) -> None:
            if not self._first_step and "step" in text:
                self._first_step = time.monotonic()  # for the timing log line
            self._progress = Progress(fraction, text, fraction < 1.0)
            if progress:
                progress(fraction, text)

        return cb

    @staticmethod
    def _report(progress: ProgressCallback | None, fraction: float, text: str) -> None:
        if progress:
            progress(fraction, text)

    @staticmethod
    def _translate_queue_error(err: ComfyApiError, graph: dict) -> Exception:
        if err.node_errors:
            text = workflow.describe_node_errors(err.node_errors, graph)
            lowered = text.lower()
            if "not in list" in lowered or (
                "invalid" in lowered and ("ckpt" in lowered or "patch" in lowered or "head" in lowered)
            ):
                return ModelMissing(text)
            return WorkflowRejected(text)
        if err.status == 0:
            return BackendUnavailable(str(err))
        return WorkflowRejected(str(err))


def _content_id(*parts: bytes) -> str:
    """12 hex digits identifying ``parts`` (used in upload and output file names)."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()[:12]


def model_files(model: ModelSpec | None) -> set[str]:
    """File names a request loads (checkpoint, UNet, text encoders, VAE,
    removal model, Fooocus files)."""
    if model is None:
        return set()
    names = (
        model.checkpoint,
        model.unet,
        model.clip1,
        model.clip2,
        model.vae,
        model.inpaint_model,
        model.fooocus_head if model.uses_fooocus else "",
        model.fooocus_patch if model.uses_fooocus else "",
    )
    return {n for n in names if n}


def model_bytes(models_root: Path | None, names: set[str]) -> int:
    """Total size on disk of ``names`` under the usual model folders of
    ``models_root`` (0 for files not found, or without a root)."""
    if models_root is None:
        return 0
    total = 0
    for name in names:
        for folder in MODEL_FOLDERS:
            path = models_root / folder / name
            try:
                if path.is_file():
                    total += path.stat().st_size
                    break
            except OSError:
                continue
    return total
