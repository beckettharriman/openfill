# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""AUTOMATIC1111 WebUI backend (``--api`` mode).

This keeps the protocol the original Stable Boy spoke, behind the common
:class:`Backend` interface. It is kept compiling and documented, but it is
NOT tested on the development machine (A1111 is not installed there). The
request bodies follow ``/docs`` of the WebUI as of 2024-2026.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..backend import (
    Backend,
    BackendUnavailable,
    CancelCallback,
    GenerationCancelled,
    GenerationResult,
    GenerationTimeout,
    HealthStatus,
    InpaintRequest,
    OutOfMemory,
    PreviewCallback,
    Progress,
    ProgressCallback,
    Sampling,
    Txt2ImgRequest,
    WorkflowRejected,
)
from ..geometry import estimate_timeout, seed_or_random

# ComfyUI sampler names -> A1111 sampler titles (best effort).
SAMPLER_MAP = {
    "euler": "Euler",
    "euler_ancestral": "Euler a",
    "heun": "Heun",
    "dpm_2": "DPM2",
    "dpm_2_ancestral": "DPM2 a",
    "lms": "LMS",
    "dpmpp_2s_ancestral": "DPM++ 2S a",
    "dpmpp_2m": "DPM++ 2M",
    "dpmpp_sde": "DPM++ SDE",
    "dpmpp_2m_sde": "DPM++ 2M SDE",
    "dpmpp_2m_sde_gpu": "DPM++ 2M SDE",
    "ddim": "DDIM",
    "uni_pc": "UniPC",
}
SCHEDULER_MAP = {
    "normal": "Automatic",
    "karras": "Karras",
    "exponential": "Exponential",
    "sgm_uniform": "SGM Uniform",
    "simple": "Simple",
    "ddim_uniform": "DDIM",
}


class A1111Backend(Backend):
    """AUTOMATIC1111 WebUI over its ``/sdapi/v1`` REST API (no live previews)."""

    name = "a1111"

    def __init__(
        self, url: str = "http://127.0.0.1:7860", request_timeout: float = 0.0, logger: logging.Logger | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.request_timeout = request_timeout
        self.log = logger
        self._progress = Progress()

    # http helpers --------------------------------------------------------

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except HTTPError as err:
            try:
                detail = err.read()[:500].decode("utf-8", "replace")
            finally:
                err.close()
            if err.code >= 500 and "memory" in detail.lower():
                raise OutOfMemory(detail) from err
            raise WorkflowRejected(f"HTTP {err.code}: {detail}") from err
        except (URLError, OSError) as err:
            raise BackendUnavailable(str(err)) from err
        return json.loads(raw.decode("utf-8")) if raw else {}

    # backend interface ---------------------------------------------------

    def health(self) -> HealthStatus:
        """List the WebUI's checkpoints; not ok when it does not answer."""
        try:
            models = self._call("GET", "/sdapi/v1/sd-models", timeout=10)
        except (BackendUnavailable, WorkflowRejected) as err:
            return HealthStatus(False, str(err))
        names = [m.get("title") or m.get("model_name", "") for m in models]
        return HealthStatus(True, "AUTOMATIC1111 WebUI", "", names, inpaint_nodes=False)

    def progress(self) -> Progress:
        """Last progress polled from ``/sdapi/v1/progress``."""
        return self._progress

    def cancel(self) -> None:
        """Ask the WebUI to interrupt; errors are ignored."""
        try:
            self._call("POST", "/sdapi/v1/interrupt", {})
        except Exception:
            pass

    def inpaint(
        self,
        request: InpaintRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """img2img with a mask (``preview`` is unused: the WebUI API has no preview stream)."""
        seed = seed_or_random(request.sampling.seed)
        body = self._common(
            request.prompt, request.negative, request.sampling, seed, request.work_width, request.work_height
        )
        body.update(
            {
                "init_images": [_b64(request.image_png)],
                "mask": _b64(request.mask_png),
                "denoising_strength": float(request.sampling.denoise),
                "mask_blur": int(request.feather),
                "inpainting_fill": 1,  # original content under the mask
                "inpaint_full_res": False,  # we already cropped the context
                "inpainting_mask_invert": 0,  # white = inpaint
                "resize_mode": 0,
                "override_settings": {"sd_model_checkpoint": request.model.checkpoint}
                if request.model.checkpoint
                else {},
            }
        )
        pixels = request.work_width * request.work_height
        images = self._run("/sdapi/v1/img2img", body, request.sampling, pixels, progress, cancel)
        return GenerationResult(images, seed, {"backend": self.name})

    def txt2img(
        self,
        request: Txt2ImgRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Text-to-image (``preview`` is unused)."""
        seed = seed_or_random(request.sampling.seed)
        body = self._common(request.prompt, request.negative, request.sampling, seed, request.width, request.height)
        if request.model.checkpoint:
            body["override_settings"] = {"sd_model_checkpoint": request.model.checkpoint}
        images = self._run(
            "/sdapi/v1/txt2img", body, request.sampling, request.width * request.height, progress, cancel
        )
        return GenerationResult(images, seed, {"backend": self.name})

    # internals -----------------------------------------------------------

    @staticmethod
    def _common(prompt: str, negative: str, sampling: Sampling, seed: int, width: int, height: int) -> dict[str, Any]:
        return {
            "prompt": prompt,
            "negative_prompt": negative,
            "steps": int(sampling.steps),
            "cfg_scale": float(sampling.cfg),
            "seed": int(seed),
            "batch_size": max(1, int(sampling.batch)),
            "width": int(width),
            "height": int(height),
            "sampler_name": SAMPLER_MAP.get(sampling.sampler, "DPM++ 2M"),
            "scheduler": SCHEDULER_MAP.get(sampling.scheduler, "Automatic"),
        }

    def _run(
        self,
        path: str,
        body: dict[str, Any],
        sampling: Sampling,
        pixels: int,
        progress: ProgressCallback | None,
        cancel: CancelCallback | None = None,
    ) -> list[bytes]:
        timeout = self.request_timeout or estimate_timeout(sampling.steps, sampling.batch, pixels)
        result: dict[str, Any] = {}
        error: list[BaseException] = []

        def worker() -> None:
            try:
                result.update(self._call("POST", path, body, timeout=timeout))
            except BaseException as err:  # propagated to the caller below
                error.append(err)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        started = time.monotonic()
        while thread.is_alive():
            thread.join(1.0)
            if cancel and cancel():
                self.cancel()
                raise GenerationCancelled("cancelled by the user")
            if time.monotonic() - started > timeout:
                self.cancel()
                raise GenerationTimeout(f"no result after {timeout:.0f} s")
            try:
                info = self._call("GET", "/sdapi/v1/progress?skip_current_image=true", timeout=5)
                frac = float(info.get("progress", 0.0))
                text = str(info.get("state", {}).get("job", "") or "Generating...")
                self._progress = Progress(frac, text, True)
                if progress:
                    progress(min(0.99, frac), text)
            except Exception:
                pass
        self._progress = Progress(1.0, "done", False)
        if error:
            raise error[0]
        images = result.get("images") or []
        if not images:
            raise WorkflowRejected("the WebUI returned no images")
        return [base64.b64decode(img.split(",", 1)[-1]) for img in images]


def _b64(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
