# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""A generator in place of ComfyUI, for tests and the end-to-end check: a fill or expand
comes back as the context inverted, text to image as a flat colour. No GPU, no models."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageOps

from openfill.backend import GenerationResult, ModelSpec


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def inverted(png_bytes: bytes, variations: int = 1) -> list[bytes]:
    """``png_bytes`` with its colours inverted, once per variation (each a little darker, so
    variations differ)."""
    with Image.open(io.BytesIO(png_bytes)) as im:
        rgb = ImageOps.invert(im.convert("RGB"))
    return [_png(rgb.point(lambda v, i=i: max(0, v - 8 * i))) for i in range(variations)]


class StubBackend:
    """``Backend``'s three generation calls."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def inpaint(self, request, progress=None, preview=None, cancel=None) -> GenerationResult:
        self.requests.append(request)
        if progress:
            progress(1.0, "Stub generator")
        return GenerationResult(inverted(request.image_png, max(1, request.sampling.batch)), seed=7)

    outpaint = inpaint

    def txt2img(self, request, progress=None, preview=None, cancel=None) -> GenerationResult:
        self.requests.append(request)
        images = [
            _png(Image.new("RGB", (request.width, request.height), (40 * i, 120, 200)))
            for i in range(max(1, request.sampling.batch))
        ]
        return GenerationResult(images, seed=7)


def install(app) -> StubBackend:
    """Make ``app`` (an ``openfill.app.App``) generate with the stub: no ComfyUI start, a
    plain SD 1.5 model."""
    backend = StubBackend()
    app.resolve_model = lambda choice: (None, ModelSpec(checkpoint="stub.safetensors"))
    app.backend = lambda progress=None, cancel=None: backend
    app.server.touch = lambda: None
    return backend


class QuietRunner:
    """``App.runner_cls`` without a window: the job runs in the caller's thread."""

    def __init__(self, app, title: str, model_name: str, interactive: bool = True) -> None:
        self.title = title

    def run(self, job):
        return job(lambda fraction, text: None, lambda payload: None, lambda: False)

    def next_round(self) -> None:
        pass

    def done(self, message: str = "Done", close_after_ms: int = 1000) -> None:
        pass

    def fail(self, error: BaseException) -> None:
        pass
