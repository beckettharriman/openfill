# SPDX-License-Identifier: Apache-2.0
"""ComfyClient against a stub HTTP server (no websocket: exercises the polling path)."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from openfill.backend import InpaintRequest, ModelMissing, ModelSpec, OutOfMemory, Sampling, WorkflowRejected
from openfill.backends.comfy import ComfyBackend
from openfill.comfy_api import ComfyApiError, ComfyClient, ComfyExecutionError


class StubState:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []
        self.prompts: list[dict] = []
        self.history_ready_after = 1
        self.fail_prompt: dict | None = None
        self.history_error = False
        self.history_calls = 0
        self.interrupted = False


STATE = StubState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/system_stats":
            self._send(200, {"system": {"comfyui_version": "0.39.0"}, "devices": [{"vram_total": 8, "vram_free": 7}]})
        elif self.path == "/object_info/CheckpointLoaderSimple":
            self._send(
                200, {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["a.ckpt", "b.safetensors"]]}}}}
            )
        elif self.path == "/object_info/INPAINT_ExpandMask":
            self._send(200, {"INPAINT_ExpandMask": {"input": {"required": {}}}})
        elif self.path == "/object_info/INPAINT_LoadFooocusInpaint":
            self._send(
                200,
                {
                    "INPAINT_LoadFooocusInpaint": {
                        "input": {"required": {"head": [["head.pth"]], "patch": [["v26.patch"]]}}
                    }
                },
            )
        elif self.path.startswith("/object_info/"):
            self._send(404, {"error": "not found"})
        elif self.path.startswith("/history/"):
            STATE.history_calls += 1
            pid = self.path.rsplit("/", 1)[1]
            if STATE.history_calls < STATE.history_ready_after:
                self._send(200, {})
            elif STATE.history_error:
                self._send(
                    200,
                    {
                        pid: {
                            "status": {
                                "status_str": "error",
                                "messages": [
                                    [
                                        "execution_error",
                                        {
                                            "exception_type": "torch.OutOfMemoryError",
                                            "exception_message": "CUDA out of memory",
                                        },
                                    ]
                                ],
                            }
                        }
                    },
                )
            else:
                self._send(
                    200,
                    {
                        pid: {
                            "status": {"status_str": "success"},
                            "outputs": {
                                "save": {
                                    "images": [
                                        {"filename": "out.png", "subfolder": "sb", "type": "output"},
                                        {"filename": "tmp.png", "subfolder": "", "type": "temp"},
                                    ]
                                }
                            },
                        }
                    },
                )
        elif self.path.startswith("/view?"):
            self._send(200, b"PNGDATA:" + self.path.encode(), "image/png")
        elif self.path == "/queue":
            self._send(200, {"queue_running": [], "queue_pending": []})
        else:
            self._send(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/upload/image":
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            STATE.uploads.append((name, length))
            self._send(200, {"name": name, "subfolder": "openfill", "type": "input"})
        elif self.path == "/prompt":
            payload = json.loads(body)
            STATE.prompts.append(payload)
            if STATE.fail_prompt:
                self._send(400, STATE.fail_prompt)
            else:
                self._send(200, {"prompt_id": "pid-1", "number": 1, "node_errors": {}})
        elif self.path == "/interrupt":
            STATE.interrupted = True
            self._send(200, {})
        else:
            self._send(404, {"error": "nope"})


class ComfyClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        STATE.__init__()
        self.client = ComfyClient(self.url, timeout=5)

    def test_health_and_options(self):
        self.assertTrue(self.client.is_alive())
        self.assertEqual(self.client.combo_options("CheckpointLoaderSimple", "ckpt_name"), ["a.ckpt", "b.safetensors"])
        self.assertEqual(self.client.combo_options("Missing", "x"), [])
        self.assertEqual(self.client.object_info("Missing"), {})

    def test_upload_and_queue_and_wait(self):
        ref = self.client.upload_image("m.png", b"\x89PNG....")
        self.assertEqual(ref, "openfill/m.png")
        self.assertEqual(STATE.uploads[0][0], "m.png")
        pid = self.client.queue_prompt({"a": {"class_type": "X", "inputs": {}}})
        self.assertEqual(pid, "pid-1")
        self.assertEqual(STATE.prompts[0]["client_id"], self.client.client_id)
        STATE.history_ready_after = 2
        events = []
        entry = self.client.wait_for(
            pid, 10, time.monotonic() + 20, lambda f, t: events.append((f, t)), poll_interval=0.05
        )
        self.assertEqual(entry["status"]["status_str"], "success")
        self.assertEqual(events[-1], (1.0, "Done"))
        images = self.client.output_images(entry, "save")
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith(b"PNGDATA:/view?filename=out.png"))

    def test_wait_reports_execution_error(self):
        STATE.history_error = True
        with self.assertRaises(ComfyExecutionError) as ctx:
            self.client.wait_for("pid-1", 10, time.monotonic() + 20, poll_interval=0.05)
        self.assertTrue(ctx.exception.is_out_of_memory)

    def test_cancel_interrupts_and_clears_queue(self):
        STATE.history_ready_after = 10**6
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] >= 2

        with self.assertRaises(ComfyApiError) as ctx:
            self.client.wait_for("pid-1", 10, time.monotonic() + 20, cancel=cancel, poll_interval=0.05)
        self.assertIn("cancelled", str(ctx.exception))
        self.assertTrue(STATE.interrupted)

    def test_wait_times_out(self):
        STATE.history_ready_after = 10**6
        with self.assertRaises(ComfyApiError):
            self.client.wait_for("pid-1", 10, time.monotonic() + 0.3, poll_interval=0.05)

    def test_prompt_rejection_carries_node_errors(self):
        STATE.fail_prompt = {
            "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
            "node_errors": {
                "ckpt": {
                    "errors": [
                        {
                            "message": "Value not in list",
                            "details": "ckpt_name: 'x' not in list",
                            "extra_info": {"input_name": "ckpt_name"},
                        }
                    ]
                }
            },
        }
        with self.assertRaises(ComfyApiError) as ctx:
            self.client.queue_prompt({"ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {}}})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("ckpt", ctx.exception.node_errors)

    def test_unreachable_server(self):
        client = ComfyClient("http://127.0.0.1:1", timeout=1)
        self.assertFalse(client.is_alive())
        with self.assertRaises(ComfyApiError):
            client.system_stats()


class ComfyBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        STATE.__init__()
        self.backend = ComfyBackend(self.url, ComfyClient(self.url, timeout=5))

    def _request(self, **kw):
        base = dict(
            image_png=b"img",
            mask_png=b"mask",
            width=64,
            height=64,
            work_width=64,
            work_height=64,
            prompt="p",
            negative="n",
            model=ModelSpec("a.ckpt"),
            sampling=Sampling(steps=4, seed=1),
        )
        base.update(kw)
        return InpaintRequest(**base)

    def test_health(self):
        h = self.backend.health()
        self.assertTrue(h.ok)
        self.assertEqual(h.checkpoints, ["a.ckpt", "b.safetensors"])
        self.assertTrue(h.inpaint_nodes)
        self.assertEqual(h.fooocus_patches, ["v26.patch"])

    def test_inpaint_end_to_end(self):
        result = self.backend.inpaint(self._request(), lambda f, t: None)
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.seed, 1)
        self.assertEqual(len(STATE.uploads), 2)
        graph = STATE.prompts[0]["prompt"]
        self.assertEqual(graph["load_image"]["inputs"]["image"], "openfill/sb-{}-image.png".format(result.info["job"]))
        self.assertEqual(graph["mask_grow"]["class_type"], "INPAINT_ExpandMask")

    def test_missing_model_is_translated(self):
        STATE.fail_prompt = {
            "error": {"message": "validation"},
            "node_errors": {
                "ckpt": {
                    "errors": [
                        {
                            "message": "Value not in list",
                            "details": "ckpt_name: 'zzz' not in ['a.ckpt']",
                            "extra_info": {"input_name": "ckpt_name"},
                        }
                    ]
                }
            },
        }
        with self.assertRaises(ModelMissing):
            self.backend.inpaint(self._request())

    def test_oom_is_translated(self):
        STATE.history_error = True
        with self.assertRaises(OutOfMemory):
            self.backend.inpaint(self._request())

    def test_other_rejection(self):
        STATE.fail_prompt = {
            "error": {"message": "bad"},
            "node_errors": {
                "sampler": {
                    "errors": [
                        {"message": "Value bigger than max", "details": "steps", "extra_info": {"input_name": "steps"}}
                    ]
                }
            },
        }
        with self.assertRaises(WorkflowRejected):
            self.backend.inpaint(self._request())


if __name__ == "__main__":
    unittest.main()


class GpuErrorClassificationTests(unittest.TestCase):
    """Issue #2: a GTX 1060 with a CUDA 13 PyTorch failed with a CUDA error that was shown as out of memory."""

    def err(self, message, exception_type="RuntimeError", traceback=""):
        from openfill.comfy_api import ComfyExecutionError

        return ComfyExecutionError(message, exception_type, "INPAINT_InpaintWithModel", traceback)

    def test_no_kernel_image_is_not_out_of_memory(self):
        e = self.err("CUDA error: no kernel image is available for execution on the device")
        self.assertFalse(e.is_out_of_memory)
        self.assertTrue(e.is_unsupported_gpu)

    def test_real_out_of_memory(self):
        e = self.err("CUDA out of memory. Tried to allocate 2.00 GiB", "torch.OutOfMemoryError")
        self.assertTrue(e.is_out_of_memory)
        self.assertFalse(e.is_unsupported_gpu)

    def test_other_cuda_errors_are_neither(self):
        e = self.err("CUDA error: an illegal memory access was encountered")
        self.assertFalse(e.is_out_of_memory)
        self.assertFalse(e.is_unsupported_gpu)

    def test_startup_warning_in_the_log(self):
        from openfill.comfy_api import unsupported_gpu_line

        log = (
            "[INFO] Total VRAM 6144 MB\n"
            "UserWarning: NVIDIA GeForce GTX 1060 with Max-Q Design with CUDA capability sm_61 is not compatible "
            "with the current PyTorch installation.\n[INFO] Device: cuda:0"
        )
        self.assertIn("sm_61", unsupported_gpu_line(log))
        self.assertEqual(unsupported_gpu_line("[INFO] Device: cuda:0 NVIDIA GeForce RTX 4070"), "")
