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

    def test_app_gives_a_local_comfyui_backend_the_death_watch(self):
        # comfy-died-feedback after the Remote GPU and Cloud merge: the local backend still gets
        # the managed ComfyUI's death watch (and no Remote GPU link check).
        import logging
        import tempfile
        from unittest import mock

        from openfill.app import App
        from openfill.config import Settings

        watch = object()
        app = App.__new__(App)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        app.settings = Settings(backend="comfyui", comfy_dir=folder.name)
        app.server = mock.Mock(ensure_running=mock.Mock(return_value="http://127.0.0.1:1"))
        app.server.death_watch.return_value = watch
        app.log = logging.getLogger("test")
        app._restart_for_shared_models = lambda: None
        app._check_device = lambda url: None
        backend_ = app.backend()
        self.assertIs(backend_.server_gone, watch)
        self.assertEqual(backend_.client.link_path, "")

    def test_a_timed_out_request_to_a_gone_server_reads_as_stopped(self):
        # Remote GPU's R6 checks the deadline first, then a lost connection, then "timed out":
        # a /history request that timed out against a ComfyUI that is gone is still "stopped".
        from openfill.backends.comfy import CONNECTION_LOST
        from openfill.comfy_api import ComfyApiError

        client = self.FakeClient(alive=False)
        client.wait_for = lambda *a, **k: (_ for _ in ()).throw(ComfyApiError("GET /history/p1 failed: timed out"))
        self.assertEqual(self.run_with(client), CONNECTION_LOST)

    def test_server_still_up_keeps_detail(self):
        self.assertIn("/history", self.run_with(self.FakeClient(alive=True)))

    def test_process_check_reaches_the_wait(self):
        from unittest import mock

        from openfill.backends.comfy import ComfyBackend
        from openfill.comfy_api import ComfyApiError

        client = self.FakeClient(alive=False)
        client.wait_for = mock.Mock(side_effect=ComfyApiError("GET /history/p1 failed: refused"))

        def gone():
            return True

        backend_ = ComfyBackend("http://x", client=client, request_timeout=30, server_gone=gone)
        with self.assertRaises(BackendUnavailable):
            backend_._run({}, 20, 1, 512 * 512, None)
        self.assertIs(client.wait_for.call_args.kwargs["server_gone"], gone)


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


class ServerSeparatorTests(unittest.TestCase):
    """ComfyUI lists a file in a sub folder with its own system's separator (os.path.relpath:
    ``flux\\clip_l.safetensors`` on Windows) and accepts only that exact name."""

    GRAPH = {
        "clip": {
            "class_type": "DualCLIPLoader",
            "inputs": {"clip_name1": "t5.gguf", "clip_name2": "flux/clip_l.safetensors"},
        },
        "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "mine/fill.gguf"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": "a/b", "clip": ["clip", 0]}},
    }

    def test_names_follow_the_server(self):
        from openfill.backends import comfy

        win = comfy.with_server_separator(self.GRAPH, True)
        self.assertEqual(win["clip"]["inputs"]["clip_name2"], "flux\\clip_l.safetensors")
        self.assertEqual(win["unet"]["inputs"]["unet_name"], "mine\\fill.gguf")
        self.assertEqual(win["pos"]["inputs"]["text"], "a/b")  # prompts are not names
        self.assertEqual(win["pos"]["inputs"]["clip"], ["clip", 0])
        self.assertIs(comfy.with_server_separator(self.GRAPH, False), self.GRAPH)
        self.assertEqual(self.GRAPH["unet"]["inputs"]["unet_name"], "mine/fill.gguf")  # not changed in place
        self.assertTrue(comfy.has_sub_folder_names(self.GRAPH))
        self.assertFalse(comfy.has_sub_folder_names({"pos": self.GRAPH["pos"]}))

    def test_server_system_is_asked_once(self):
        from openfill.backends import comfy

        class Client:
            calls = 0

            def __init__(self, os_name):
                self.os_name = os_name

            def system_stats(self):
                Client.calls += 1
                return {"system": {"os": self.os_name}}

        be = comfy.ComfyBackend("http://127.0.0.1:9", client=Client("win32"))
        self.assertTrue(be.server_on_windows())
        self.assertTrue(be.server_on_windows())
        self.assertEqual(Client.calls, 1)
        for os_name, expected in (("linux", False), ("darwin", False), ("nt", True)):
            be = comfy.ComfyBackend("http://127.0.0.1:9", client=Client(os_name))
            self.assertEqual(be.server_on_windows(), expected, os_name)
