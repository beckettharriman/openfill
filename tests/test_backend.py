# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""describe_error: every failure reaches the user in plain words with a next step."""

import inspect
import re
import unittest

from openfill import backend
from openfill.backend import (
    BackendError,
    BackendUnavailable,
    GenerationCancelled,
    GenerationTimeout,
    ModelMissing,
    OutOfMemory,
    WorkflowRejected,
    describe_error,
    has_next_step,
)

NEXT_STEP = re.compile(r"settings|download|select|try|close|install", re.IGNORECASE)

# Raw texts the backends really raise (backends/comfy.py, backends/a1111.py).
RAW = {
    BackendUnavailable: "connection refused",
    ModelMissing: "ckpt_name: 'x.safetensors' not in []",
    OutOfMemory: "CUDA out of memory. Tried to allocate 2.00 GiB",
    GenerationTimeout: "no result after 240 s",
    GenerationCancelled: "cancelled by the user",
    WorkflowRejected: "KSampler: Required input is missing: model",
}


class NothingSelected(BackendError):
    """Same shape as app.NothingSelected (that module needs GIMP)."""


class UnsupportedImage(BackendError):
    """Same shape as app.UnsupportedImage."""


def backend_error_classes():
    return [
        obj
        for _, obj in inspect.getmembers(backend, inspect.isclass)
        if issubclass(obj, BackendError) and obj.__module__ == backend.__name__
    ]


class DescribeErrorTests(unittest.TestCase):
    def test_every_backend_error_class_has_a_next_step(self):
        classes = backend_error_classes()
        self.assertGreaterEqual(len(classes), 7)
        for cls in classes:
            for raw in (RAW.get(cls, "something went wrong"), ""):
                with self.subTest(cls=cls.__name__, raw=raw):
                    text = describe_error(cls(raw))
                    self.assertRegex(text, NEXT_STEP)
                    self.assertNotIn(f"{cls.__name__}:", text)

    def test_messages_with_their_own_next_step_are_shown_unchanged(self):
        nothing = (
            "Nothing is selected. Select the area to fill first (for example with the Rectangle or "
            "Free Select tool), then run Generative Fill again."
        )
        indexed = "This image uses indexed colors. Convert it with Image > Mode > RGB and run the command again."
        self.assertEqual(describe_error(NothingSelected(nothing)), nothing)
        self.assertEqual(describe_error(UnsupportedImage(indexed)), indexed)

    def test_dedicated_wordings(self):
        self.assertIn("ran out of memory", describe_error(OutOfMemory("x")))
        self.assertIn("close other programs", describe_error(OutOfMemory("x")))
        self.assertEqual(
            describe_error(GenerationCancelled("x")),
            "Generation was cancelled. Run the command again to try once more.",
        )
        timeout = describe_error(GenerationTimeout("no result after 240 s"))
        self.assertTrue(timeout.startswith("The request timed out (no result after 240 s)."))
        self.assertIn("Generation time limit", timeout)
        rejected = describe_error(WorkflowRejected("KSampler: Required input is missing: model"))
        self.assertIn("ComfyUI rejected the request: KSampler", rejected)
        self.assertIn("Models", rejected)

    def test_model_missing_keeps_the_app_wording(self):
        msg = (
            "'Stable Diffusion 1.5 Inpainting' is not downloaded yet. Open Filters > OpenFill > Generative "
            "Fill Settings > Models and press Download."
        )
        self.assertEqual(describe_error(ModelMissing(msg)), f"A model file is missing on the backend: {msg}")
        bare = describe_error(ModelMissing("ckpt_name: 'x' not in []"))
        self.assertIn("press Download", bare)

    def test_backend_unavailable_with_and_without_next_step(self):
        port = "Port 8199 is used by another program (it is not ComfyUI). Close that program."
        self.assertEqual(describe_error(BackendUnavailable(port)), f"The AI backend is not available: {port}")
        bare = describe_error(BackendUnavailable("connection refused"))
        self.assertTrue(bare.startswith("The AI backend is not available: connection refused. "))
        self.assertIn("Generative Fill Settings", bare)

    def test_unexpected_exception_keeps_class_name_and_gets_a_step(self):
        text = describe_error(KeyError("seed"))
        self.assertTrue(text.startswith("KeyError: 'seed'"))
        self.assertTrue(has_next_step(text))


if __name__ == "__main__":
    unittest.main()


class ConnectionLostTests(unittest.TestCase):
    """rob-backend-crash-mid-job: ComfyUI killed while sampling must read as a stopped backend."""

    class FakeClient:
        def __init__(self, alive, status=0):
            self.alive, self.status = alive, status

        def queue_prompt(self, graph):
            return "p1"

        def wait_for(self, *args, **kwargs):
            from openfill.comfy_api import ComfyApiError

            raise ComfyApiError("GET /history/p1 failed: <urlopen error [WinError 10061] refused>", self.status)

        def is_alive(self, timeout=3.0):
            return self.alive

        def interrupt(self):
            pass

        def clear_queue(self):
            pass

    def run_with(self, client):
        from openfill.backends.comfy import ComfyBackend

        backend_ = ComfyBackend("http://x", client=client, request_timeout=30)
        with self.assertRaises(BackendUnavailable) as ctx:
            backend_._run({}, 20, 1, 512 * 512, None)
        return str(ctx.exception)

    def test_server_gone_gives_plain_message(self):
        from openfill.backends.comfy import CONNECTION_LOST

        text = self.run_with(self.FakeClient(alive=False))
        self.assertEqual(text, CONNECTION_LOST)
        self.assertNotIn("urlopen", describe_error(BackendUnavailable(text)))
        self.assertTrue(has_next_step(text))

    def test_server_still_up_keeps_detail(self):
        self.assertIn("/history", self.run_with(self.FakeClient(alive=True)))


class NoOpenCvFallbackTests(unittest.TestCase):
    """Without OpenCV on the server the fill is redone with the neutral pre-fill, and the result says so."""

    def _inpaint(self, errors):
        from types import SimpleNamespace
        from unittest import mock

        from openfill import workflow
        from openfill.backends.comfy import ComfyBackend

        client = SimpleNamespace(
            object_info=lambda _n: {"x": 1}, upload_image=lambda name, _data: name, interrupt=None, clear_queue=None
        )
        backend_ = ComfyBackend("http://x", client=client)
        request = SimpleNamespace(
            model=SimpleNamespace(uses_fooocus=False),
            sampling=SimpleNamespace(seed=1, steps=20, batch=1),
            image_png=b"i",
            mask_png=b"m",
            mode="expand",
            prefill_mode="navier-stokes",
            work_width=512,
            work_height=512,
        )
        runs = []

        def run(*_a, **_k):
            runs.append(request.prefill_mode)
            if errors:
                raise errors.pop(0)
            return [b"png"]

        with (
            mock.patch.object(workflow, "probe_safe_png", side_effect=lambda d: d),
            mock.patch.object(workflow, "build_fill", return_value={}),
            mock.patch.object(backend_, "_run", side_effect=run),
        ):
            result = backend_.inpaint(request)
        return result, runs

    def test_missing_cv2_is_flagged(self):
        result, runs = self._inpaint([backend.WorkflowRejected("No module named 'cv2'")])
        self.assertEqual(runs, ["navier-stokes", "neutral"])
        self.assertEqual(result.info.get("prefill_fallback"), "no-opencv")

    def test_normal_result_has_no_flag(self):
        result, runs = self._inpaint([])
        self.assertEqual(runs, ["navier-stokes"])
        self.assertNotIn("prefill_fallback", result.info)


class GpuErrorWordingTests(unittest.TestCase):
    def test_out_of_memory_keeps_comfyui_text(self):
        text = backend.describe_error(backend.OutOfMemory("CUDA out of memory. Tried to allocate 2.00 GiB"))
        self.assertIn("ran out of memory", text)
        self.assertIn("ComfyUI said: CUDA out of memory", text)

    def test_unsupported_gpu_says_what_to_get(self):
        text = backend.describe_error(backend.UnsupportedGpu("CUDA error: no kernel image is available"))
        self.assertNotIn("out of memory", text)
        self.assertIn("ComfyUI_windows_portable_nvidia_cu126.7z", text)
        self.assertIn("no kernel image", text)

    def test_unsupported_amd_card_says_what_to_get(self):
        text = backend.describe_error(backend.UnsupportedGpu("HIP error: invalid device function"))
        self.assertIn("AMD", text)
        self.assertIn("RX 7000", text)
        self.assertNotIn("cu126", text)
