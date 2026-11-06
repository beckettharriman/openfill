# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Remote GPU: the GPU server (proxy, pairing, limits, node installs) and the editor's side
(addresses, SSH tunnel, model and node checks, messages), against a fake ComfyUI."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError, URLError

from openfill import catalog as cat
from openfill import comfy_api, downloads, gpu_server, remote
from openfill.backend import BackendUnavailable
from openfill.comfy_api import ComfyApiError, ComfyClient
from openfill.config import Settings
from openfill.websocket import OP_TEXT, WebSocket

TOKEN = "T" * 32
PNG = b"\x89PNG\r\n\x1a\nfake"


# ------------------------------------------------------------- fake ComfyUI ----


class FakeComfyHandler(BaseHTTPRequestHandler):
    server: FakeComfy

    def log_message(self, *args):
        pass

    def _json(self, data, code=200):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        srv = self.server
        srv.seen.append((self.command, self.path, dict(self.headers)))
        path = self.path.partition("?")[0]
        if path == "/system_stats":
            self._json(
                {
                    "system": {"comfyui_version": "0.39.0", "ram_total": 32 * 2**30},
                    "devices": [
                        {
                            "name": "cuda:0 NVIDIA GeForce RTX 4070 Laptop GPU : cudaMallocAsync",
                            "type": "cuda",
                            "vram_total": 8 * 2**30,
                            "vram_free": 7 * 2**30,
                        }
                    ],
                }
            )
        elif path == "/models":
            self._json(sorted(srv.models))
        elif path.startswith("/models/"):
            key = path.split("/", 2)[2]
            if key in srv.models:
                self._json(sorted(srv.models[key]))
            else:
                self.send_response(404)
                self.end_headers()
        elif path.startswith("/object_info/"):
            cls = path.split("/", 2)[2]
            self._json({cls: {"input": {}}} if srv.loaded(cls) else {})
        elif path.startswith("/history/"):
            pid = path.split("/", 2)[2]
            out = {"9": {"images": [{"filename": "r.png", "subfolder": "", "type": "output"}]}}
            self._json({pid: {"status": {"status_str": "success"}, "outputs": out}} if srv.done else {})
        elif path == "/view":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        elif path == "/ws":
            self._websocket()
        else:
            self._json({"secret": "a page the GPU server must not pass"}, 404)

    def do_POST(self):  # noqa: N802
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        srv.seen.append((self.command, self.path, dict(self.headers)))
        if self.path == "/upload/image":
            srv.uploads.append(len(body))
            self._json({"name": "x.png", "subfolder": "openfill", "type": "input"})
        elif self.path == "/prompt":
            self._json({"prompt_id": "p1", "number": 1})
        elif self.path in ("/interrupt", "/queue", "/free"):
            self._json({})
        else:
            self._json({"error": "no such route"}, 404)

    def _websocket(self):
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        for msg in self.server.ws_messages:
            raw = json.dumps(msg).encode()
            self.wfile.write(bytes((0x81, len(raw))) + raw)
        self.wfile.flush()
        self.server.done = True
        time.sleep(0.3)
        self.close_connection = True


class FakeComfy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, nodes_dir: Path | None = None):
        super().__init__(("127.0.0.1", 0), FakeComfyHandler)
        self.seen: list = []
        self.uploads: list[int] = []
        self.done = False
        self.nodes_dir = nodes_dir
        self.models = {
            "checkpoints": {"sd-v1-5-inpainting.ckpt"},
            "diffusion_models": set(),
            "unet_gguf": {"flux1-fill-dev-Q6_K.gguf"},
            "text_encoders": {"clip_l.safetensors"},
            "clip_gguf": {"t5-v1_1-xxl-encoder-Q5_K_M.gguf"},
            "vae": {"ae.safetensors"},
            "inpaint": {"big-lama.pt"},
            "vae_approx": set(),
            "loras": set(),
        }
        self.classes = {"INPAINT_LoadInpaintModel", "UnetLoaderGGUF"}
        self.ws_messages = [
            {"type": "progress", "data": {"value": 1, "max": 1, "prompt_id": "p1"}},
            {"type": "executing", "data": {"node": None, "prompt_id": "p1"}},
        ]
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def loaded(self, cls: str) -> bool:
        if cls == "OpenFillLamaNative":
            return self.nodes_dir is not None and (self.nodes_dir / "openfill-nodes").is_dir()
        return cls in self.classes

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


class GpuServerCase(unittest.TestCase):
    """A real GPU server on 127.0.0.1 in front of a fake ComfyUI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.nodes = self.root / "custom_nodes"
        (self.nodes / "comfyui-inpaint-nodes").mkdir(parents=True)
        (self.nodes / "ComfyUI-GGUF").mkdir()
        self.comfy = FakeComfy(self.nodes)
        self.home = self.root / "home"
        gpu_server.write_state(self.home, {"token_sha256": gpu_server.token_digest(TOKEN)})
        self.logged: list[str] = []
        comfy = gpu_server.Comfy(self.comfy.url, log=self.logged.append)
        tasks = gpu_server.Tasks(self.nodes, None, comfy, self.logged.append)
        self.gate = gpu_server.Gate(gpu_server.token_digest(TOKEN))
        self.server = gpu_server.GpuServer(("127.0.0.1", 0), self.gate, comfy, tasks, 1024 * 1024, self.logged.append)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.comfy.stop()
        comfy_api.set_token(self.url, "")
        cat.register_remote_tree(None, self.root / "remote" / "models")
        self.tmp.cleanup()

    def get(self, path, token=TOKEN, method="GET", body=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url + path, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except HTTPError as err:
            with err:
                return err.code, err.read()


class ProxyTests(GpuServerCase):
    def test_requests_without_the_right_pairing_code_are_refused(self):
        self.assertEqual(self.get("/system_stats", token="")[0], 401)
        self.assertEqual(self.get("/system_stats", token="W" * 32)[0], 401)
        status, body = self.get("/system_stats")
        self.assertEqual(status, 200)
        self.assertIn("devices", json.loads(body))
        self.assertTrue(any("wrong or missing pairing code" in line for line in self.logged))
        self.assertFalse(any(TOKEN in line or "W" * 32 in line for line in self.logged))

    def test_the_code_also_works_as_x_openfill_token(self):
        self.assertEqual(self.get("/system_stats", token="", headers={"X-OpenFill-Token": TOKEN})[0], 200)

    def test_the_pairing_code_and_origin_never_reach_comfyui(self):
        self.get("/system_stats", headers={"Origin": "http://evil.example", "Cookie": "a=b"})
        _method, _path, headers = self.comfy.seen[-1]
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("origin", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertEqual(lowered["host"], f"127.0.0.1:{self.comfy.server_address[1]}")

    def test_only_the_endpoints_openfill_uses_are_passed(self):
        for path in (
            "/",
            "/userdata/x",
            "/api/manager/queue/install",
            "/extensions",
            "/models/../secret",
            "/models/%2e%2e",
            "/object_info/%2F..%2Fuserdata",
            "/view/../userdata",
        ):
            status, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"secret", body)
        self.assertEqual(self.get("/system_stats", method="DELETE")[0], 404)
        self.assertEqual(self.get("/models/checkpoints")[0], 200)
        self.assertEqual(self.get("/object_info/UnetLoaderGGUF")[0], 200)

    def raw(self, head: str) -> bytes:
        sock = socket.create_connection(self.server.server_address, timeout=5)
        try:
            sock.sendall(head.encode())
            return sock.recv(400)
        finally:
            sock.close()

    def test_uploads_over_the_limit_and_without_a_length_are_refused(self):
        big = 1024 * 1024 + 1
        answer = self.raw(
            f"POST /upload/image HTTP/1.1\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {big}\r\n\r\n"
        )
        self.assertIn(b" 413 ", answer)
        self.assertEqual(self.comfy.uploads, [])
        status, _ = self.get("/prompt", method="POST", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        answer = self.raw(
            f"POST /upload/image HTTP/1.1\r\nAuthorization: Bearer {TOKEN}\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        )
        self.assertIn(b" 411 ", answer)

    def test_repeated_wrong_codes_lock_the_address_out_for_a_minute(self):
        for _ in range(gpu_server.MAX_FAILURES):
            self.get("/system_stats", token="W" * 32)
        status, body = self.get("/system_stats")
        self.assertEqual(status, 429)
        self.assertIn(b"wait a minute", body)

    def test_the_lockout_ends_after_the_window(self):
        now = [0.0]
        gate = gpu_server.Gate(gpu_server.token_digest(TOKEN), clock=lambda: now[0])
        bad = {"Authorization": "Bearer " + "W" * 32}
        for _ in range(gpu_server.MAX_FAILURES):
            self.assertFalse(gate.check("1.2.3.4", bad))
        self.assertTrue(gate.locked("1.2.3.4"))
        self.assertFalse(gate.locked("5.6.7.8"))
        now[0] = gpu_server.FAILURE_WINDOW + 1
        self.assertFalse(gate.locked("1.2.3.4"))
        self.assertTrue(gate.check("1.2.3.4", {"Authorization": f"Bearer {TOKEN}"}))

    def test_the_progress_websocket_is_passed_through_with_the_code(self):
        ws = WebSocket(remote_ws(self.url), timeout=5, headers={"Authorization": f"Bearer {TOKEN}"})
        ws.connect()
        opcode, payload = ws.recv()
        self.assertEqual(opcode, OP_TEXT)
        self.assertEqual(json.loads(payload)["type"], "progress")
        ws.close()
        _m, _path, headers = next(s for s in self.comfy.seen if s[1].startswith("/ws"))
        self.assertNotIn("authorization", {k.lower() for k in headers})

    def test_the_websocket_without_the_code_is_refused(self):
        ws = WebSocket(remote_ws(self.url), timeout=5)
        with self.assertRaises(ConnectionError):
            ws.connect()

    def test_a_whole_job_runs_through_the_proxy(self):
        comfy_api.set_token(self.url, TOKEN)
        client = ComfyClient(self.url, timeout=10)
        self.assertTrue(client.is_alive())
        ref = client.upload_image("img.png", b"p" * 5000)
        self.assertEqual(ref, "openfill/x.png")
        self.assertEqual(len(self.comfy.uploads), 1)
        self.assertGreater(self.comfy.uploads[0], 5000)  # the image and its multipart wrapping
        pid = client.queue_prompt({"9": {"class_type": "SaveImage", "inputs": {}}})
        steps = []
        entry = client.wait_for(pid, 1, time.monotonic() + 20, lambda f, t: steps.append(t))
        self.assertTrue(any("step 1/1" in t for t in steps))
        self.assertEqual(client.output_images(entry, "9"), [PNG])

    def test_comfyui_down_answers_502_and_ping_says_so(self):
        self.comfy.stop()
        self.server.comfy._checked = 0.0
        status, body = self.get("/system_stats")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["comfy"], "down")
        status, body = self.get("/openfill/ping")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["comfy"], "down")
        self.comfy = FakeComfy(self.nodes)  # for tearDown

    def test_inventory_lists_node_folders_and_openfill_versions(self):
        (self.nodes / "openfill-nodes").mkdir()
        (self.nodes / "openfill-nodes" / "VERSION").write_text("1\n", encoding="utf-8")
        (self.nodes / "__pycache__").mkdir()
        (self.nodes / "old.disabled").mkdir()
        (self.nodes / "websocket_image_save.py").write_text("", encoding="utf-8")
        nodes = json.loads(self.get("/openfill/inventory")[1])["nodes"]
        self.assertEqual(nodes, {"ComfyUI-GGUF": "", "comfyui-inpaint-nodes": "", "openfill-nodes": "1"})

    def test_node_installs_take_the_packs_from_the_servers_catalog_only(self):
        status, body = self.get("/openfill/install-nodes", method="POST", body=b'{"model_id": "no-such-model"}')
        self.assertEqual(status, 400)
        self.assertIn(b"not in this computer's OpenFill catalog", body)
        status, body = self.get(
            "/openfill/install-nodes", method="POST", body=json.dumps({"model_id": "lama-big"}).encode()
        )
        self.assertEqual(status, 200, body)
        self.assertIn("openfill-nodes", json.loads(body)["message"])
        for _ in range(100):
            if not self.server.tasks.snapshot()["current"]:
                break
            time.sleep(0.05)
        last = self.server.tasks.snapshot()["history"][0]
        self.assertEqual(last["state"], "done", last)
        self.assertEqual(last["model_id"], "lama-big")
        self.assertIn("restart ComfyUI", last["message"])
        version = (self.nodes / "openfill-nodes" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, downloads.bundled_node_version())
        status, body = self.get("/openfill/install-nodes", method="POST", body=b'{"model_id": "lama-big"}')
        self.assertIn(b"everything this model needs is installed", body)

    def test_non_bundled_packs_need_comfyuis_python(self):
        (self.nodes / "comfyui-inpaint-nodes").rmdir()
        status, body = self.get("/openfill/install-nodes", method="POST", body=b'{"model_id": "mat-places512"}')
        self.assertEqual(status, 400)
        self.assertIn(b"ComfyUI's Python", body)


def remote_ws(url: str) -> str:
    return url.replace("http://", "ws://") + "/ws?clientId=c1"


# ------------------------------------------------------- pairing and binds ----


class PairingTests(unittest.TestCase):
    def test_a_new_pairing_stores_only_the_hash_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            token = gpu_server.new_pairing(home)
            self.assertRegex(token, remote.TOKEN_RE)
            text = (home / gpu_server.STATE_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn(token, text)
            self.assertIn(gpu_server.token_digest(token), text)
            if hasattr(__import__("os"), "geteuid"):
                self.assertEqual((home / gpu_server.STATE_FILENAME).stat().st_mode & 0o777, 0o600)
            again = gpu_server.new_pairing(home)
            self.assertNotEqual(again, token)
            self.assertNotIn(gpu_server.token_digest(token), (home / gpu_server.STATE_FILENAME).read_text())

    def test_the_pairing_line_round_trips(self):
        line = gpu_server.pairing_line(TOKEN, "100.114.250.87", 8390)
        self.assertEqual(line, f"ofgpu://{TOKEN}@100.114.250.87:8390")
        self.assertEqual(remote.parse_address(line), ("http://100.114.250.87:8390", TOKEN))
        self.assertEqual(
            remote.parse_address(gpu_server.pairing_line(TOKEN, "fd7a::1", 9000)), ("http://[fd7a::1]:9000", TOKEN)
        )

    def test_bind_addresses(self):
        self.assertEqual(gpu_server.classify("100.114.250.87"), "tailscale")
        self.assertEqual(gpu_server.classify("127.0.0.1"), "loopback")
        self.assertEqual(gpu_server.classify("192.168.1.20"), "lan")
        self.assertEqual(gpu_server.classify("0.0.0.0"), "any")
        self.assertEqual(gpu_server.classify("::"), "any")
        self.assertEqual(gpu_server.classify("8.8.8.8"), "public")
        self.assertEqual(gpu_server.choose_bind("100.114.250.87"), ("100.114.250.87", "tailscale"))
        for bad in ("0.0.0.0", "::", "8.8.8.8", "example.com"):
            with self.assertRaises(SystemExit, msg=bad):
                gpu_server.choose_bind(bad)

    def test_without_tailscale_the_server_asks_or_stops(self):
        with (
            mock.patch.object(gpu_server, "tailscale_ip", return_value=None),
            mock.patch.object(gpu_server, "local_addresses", return_value=["192.168.1.20"]),
        ):
            with self.assertRaises(SystemExit) as caught:
                gpu_server.choose_bind(None, None)
            self.assertIn("--bind", str(caught.exception))
            self.assertIn("192.168.1.20", str(caught.exception))
            self.assertEqual(gpu_server.choose_bind(None, lambda _q: "127.0.0.1"), ("127.0.0.1", "loopback"))
        with mock.patch.object(gpu_server, "tailscale_ip", return_value="100.64.0.5"):
            self.assertEqual(gpu_server.choose_bind(None), ("100.64.0.5", "tailscale"))

    def test_tailscale_address_from_its_command(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout="100.114.250.87\n"))
        with mock.patch.object(gpu_server.shutil, "which", return_value="/usr/bin/tailscale"):
            self.assertEqual(gpu_server.tailscale_ip(run), "100.114.250.87")
        self.assertEqual(run.call_args[0][0][1:], ["ip", "-4"])

    def test_comfyui_must_stay_on_this_computer(self):
        with self.assertRaises(SystemExit):
            gpu_server.Comfy("http://192.168.1.5:8188")
        self.assertEqual(gpu_server.Comfy("http://127.0.0.1:8188").url, "http://127.0.0.1:8188")

    def test_a_port_still_held_moves_comfyui_to_the_next(self):
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)  # accepts but never answers HTTP, like the socket Windows keeps after a kill
        port = held.getsockname()[1]
        started = []
        comfy = gpu_server.Comfy(
            f"http://127.0.0.1:{port}", ["py", "main.py", "--port", str(port)], log=lambda _t: None
        )
        with mock.patch.object(
            gpu_server.subprocess, "Popen", lambda cmd, **_k: started.append(list(cmd)) or mock.Mock()
        ):
            comfy.start(wait=False)
        held.close()
        self.assertNotEqual(comfy.port, port)
        self.assertEqual(started[0][-1], str(comfy.port))
        self.assertEqual(comfy.url, f"http://127.0.0.1:{comfy.port}")

    def test_managed_comfyui_listens_on_loopback_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main.py").write_text("", encoding="utf-8")
            (Path(tmp) / "venv" / "bin").mkdir(parents=True)
            (Path(tmp) / "venv" / "bin" / "python3").write_text("", encoding="utf-8")
            cmd, cwd, python = gpu_server.comfy_command(tmp, 8188, ["--lowvram"])
        self.assertEqual(cmd[cmd.index("--listen") + 1], "127.0.0.1")
        self.assertEqual(cmd[-1], "--lowvram")
        self.assertTrue(python.endswith("python3"))
        self.assertEqual(cwd, tmp)


# ------------------------------------------------------------ editor side ----


class AddressTests(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(remote.parse_address(""), ("", ""))
        self.assertEqual(remote.parse_address("100.1.2.3"), ("http://100.1.2.3:8390", ""))
        self.assertEqual(remote.parse_address("gpu-pc:9000"), ("http://gpu-pc:9000", ""))
        self.assertEqual(remote.parse_address(" http://gpu-pc:9000/ "), ("http://gpu-pc:9000", ""))
        for bad in ("ofgpu://short@h:1", "ofgpu://bad\r\nX: y@h:1", "ftp://h", "http://u:p@h:1", "http://h:x"):
            with self.assertRaises(remote.RemoteSetupError, msg=bad):
                remote.parse_address(bad)

    def test_ssh_targets_cannot_be_options(self):
        self.assertEqual(remote.parse_ssh_target("becke@blade14"), ("becke@blade14", None))
        self.assertEqual(remote.parse_ssh_target("becke@100.114.250.87:2222"), ("becke@100.114.250.87", 2222))
        self.assertEqual(remote.parse_ssh_target("ssh://gpu-pc"), ("gpu-pc", None))
        for bad in ("-oProxyCommand=evil", "a b", "user@-oX", "", "user@host;rm"):
            with self.assertRaises(remote.RemoteSetupError, msg=bad):
                remote.parse_ssh_target(bad)

    def test_a_pasted_pairing_code_moves_out_of_the_address(self):
        s = Settings(remote_url=f"ofgpu://{TOKEN}@100.114.250.87:8390")
        s.take_pairing_code()
        self.assertEqual((s.remote_url, s.remote_token), ("http://100.114.250.87:8390", TOKEN))
        self.assertNotIn(TOKEN, repr(s))
        s = Settings(remote_url="not a valid address at all", remote_token="keep")
        s.take_pairing_code()
        self.assertEqual((s.remote_url, s.remote_token), ("not a valid address at all", "keep"))

    def test_the_client_sends_the_code_only_to_its_server(self):
        comfy_api.set_token("http://100.1.2.3:8390/", TOKEN)
        try:
            self.assertEqual(comfy_api.auth_headers("http://100.1.2.3:8390"), {"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(comfy_api.auth_headers("http://127.0.0.1:8188"), {})
        finally:
            comfy_api.set_token("http://100.1.2.3:8390", "")
        self.assertEqual(comfy_api.auth_headers("http://100.1.2.3:8390"), {})


def link(url: str = "", token: str = "", ssh: str = "", **kw) -> remote.RemoteLink:
    settings = SimpleNamespace(remote_url=url, remote_token=token, remote_ssh=ssh, startup_timeout=30)
    return remote.RemoteLink(settings, **kw)


class LinkTests(GpuServerCase):
    def test_the_paired_server_is_used(self):
        rl = link(f"ofgpu://{TOKEN}@127.0.0.1:{self.server.server_address[1]}")
        self.assertEqual(rl.ensure(), self.url)
        self.assertEqual(rl.kind, "gpu-server")

    def test_a_wrong_code_says_pair_again(self):
        rl = link(self.url, token="W" * 32)
        with self.assertRaises(BackendUnavailable) as caught:
            rl.ensure()
        self.assertIn("did not accept the pairing code", str(caught.exception))

    def test_no_code_for_the_gpu_server(self):
        with self.assertRaises(BackendUnavailable) as caught:
            link(self.url).ensure()
        self.assertIn("pairing code", str(caught.exception))

    def test_server_not_running(self):
        port = self.server.server_address[1]
        self.server.shutdown()
        self.server.server_close()
        self.server = SimpleNamespace(shutdown=lambda: None, server_close=lambda: None)
        with self.assertRaises(BackendUnavailable) as caught:
            link(f"http://127.0.0.1:{port}", TOKEN).ensure()
        self.assertIn("GPU server is not running there", str(caught.exception))

    def test_comfyui_down_behind_the_server(self):
        self.comfy.stop()
        self.server.comfy._checked = 0.0
        with self.assertRaises(BackendUnavailable) as caught:
            link(self.url, TOKEN).ensure()
        self.assertIn("its ComfyUI does not", str(caught.exception))
        self.comfy = FakeComfy(self.nodes)

    def test_a_starting_comfyui_is_waited_for(self):
        answers = iter([("comfy-starting", {}), ("comfy-starting", {}), ("gpu-server", {"comfy": "up"})])
        steps = []
        with mock.patch.object(remote, "probe", lambda _url: next(answers)), mock.patch.object(remote.time, "sleep"):
            self.assertEqual(link(self.url, TOKEN).ensure(lambda f, t: steps.append(t)), self.url)
        self.assertTrue(any("starting ComfyUI" in t for t in steps))

    def test_an_open_comfyui_on_the_network_is_not_used(self):
        with mock.patch.object(remote, "probe", return_value=("comfyui", {"system": {}})):
            with self.assertRaises(BackendUnavailable) as caught:
                link("http://100.1.2.3:8188", "").ensure()
            self.assertIn("does not use an open ComfyUI over the network", str(caught.exception))
            # on this computer (a tunnel the user opened) it is fine
            self.assertEqual(link("http://127.0.0.1:18188").ensure(), "http://127.0.0.1:18188")

    def test_unset_and_asleep(self):
        with self.assertRaises(BackendUnavailable) as caught:
            link().ensure()
        self.assertIn("no GPU computer is set", str(caught.exception))

        def asleep(*_a, **_k):
            raise URLError(TimeoutError("timed out"))

        self.assertEqual(remote.probe("http://100.1.2.3:8390", opener=asleep)[0], "asleep")
        with (
            mock.patch.object(remote, "probe", return_value=("asleep", {})),
            self.assertRaises(BackendUnavailable) as caught,
        ):
            link("http://100.1.2.3:8390", TOKEN).ensure()
        self.assertIn("may be asleep", str(caught.exception))
        self.assertIn("100.1.2.3", str(caught.exception))

    def test_network_errors_have_names(self):
        def raising(err):
            def opener(*_a, **_k):
                raise err

            return opener

        self.assertEqual(remote.probe("http://h:1", opener=raising(URLError(socket.gaierror(-2, "x"))))[0], "no-name")
        self.assertEqual(remote.probe("http://h:1", opener=raising(URLError(ConnectionRefusedError())))[0], "refused")
        self.assertEqual(remote.probe("http://h:1", opener=raising(ConnectionResetError()))[0], "reset")

    def test_the_model_and_node_lists(self):
        comfy_api.set_token(self.url, TOKEN)
        root = self.root / "remote"
        catalog = cat.load_catalog()
        tree = remote.fetch_tree(
            ComfyClient(self.url), remote.catalog_folders(catalog), root / "models", root / "custom_nodes"
        )
        self.assertIn("flux1-fill-dev-Q6_K.gguf", tree.files["unet"])
        self.assertIn("t5-v1_1-xxl-encoder-Q5_K_M.gguf", tree.files["clip"])
        self.assertIn("clip_l.safetensors", tree.files["clip"])
        self.assertEqual(tree.nodes, {"comfyui-inpaint-nodes": "", "ComfyUI-GGUF": ""})
        cat.register_remote_tree(tree)
        flux = cat.by_id(catalog, "flux1-fill-dev-gguf-q6")
        st = cat.install_status(flux, root / "models", root / "custom_nodes", catalog)
        self.assertTrue(st.installed, st)
        self.assertEqual(st.missing_nodes, [])
        lama = cat.by_id(catalog, "lama-big")
        st = cat.install_status(lama, root / "models", root / "custom_nodes", catalog)
        self.assertTrue(st.installed)
        self.assertEqual(st.missing_nodes, ["openfill-nodes"])
        mat = cat.by_id(catalog, "mat-places512")
        self.assertFalse(cat.install_status(mat, root / "models", root / "custom_nodes", catalog).installed)
        # the bundled pack in another version counts as missing
        (self.nodes / "openfill-nodes").mkdir()
        (self.nodes / "openfill-nodes" / "VERSION").write_text("0\n", encoding="utf-8")
        tree = remote.fetch_tree(ComfyClient(self.url), ["inpaint"], root / "models", root / "custom_nodes")
        cat.register_remote_tree(tree)
        self.assertTrue(cat.node_missing(root / "custom_nodes", lama.requires_nodes[1]))
        # the disk under those names is never looked at
        self.assertFalse((root / "models").exists())

    def test_a_plain_comfyui_reports_nodes_by_their_classes(self):
        root = self.root / "remote"
        tree = remote.fetch_tree(ComfyClient(self.comfy.url), ["inpaint"], root / "models", root / "custom_nodes")
        self.assertEqual(tree.nodes, {"comfyui-inpaint-nodes": None, "ComfyUI-GGUF": None})
        cat.register_remote_tree(tree)
        lama = cat.by_id(cat.load_catalog(), "lama-big")
        self.assertFalse(cat.node_missing(root / "custom_nodes", lama.requires_nodes[0]))
        self.assertTrue(cat.node_missing(root / "custom_nodes", lama.requires_nodes[1]))

    def test_install_nodes_without_the_gpu_server(self):
        with self.assertRaises(BackendUnavailable) as caught:
            remote.install_nodes(ComfyClient(self.comfy.url), "lama-big")
        self.assertIn("without the OpenFill GPU server", str(caught.exception))


# ------------------------------------------------------------------- ssh ----


class FakeProc:
    def __init__(self, stderr: bytes = b"", exits: bool = True):
        self._exits = exits
        self.stderr = __import__("io").BytesIO(stderr)
        self.terminated = False

    def poll(self):
        return 255 if self._exits or self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 255

    def kill(self):
        self.terminated = True


class TunnelTests(unittest.TestCase):
    def test_the_command(self):
        t = remote.SshTunnel("becke@blade14:2222", "100.114.250.87", 8390, ssh="/usr/bin/ssh")
        cmd = t.command(40001)
        self.assertEqual(cmd[0], "/usr/bin/ssh")
        self.assertIn("-N", cmd)
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("ExitOnForwardFailure=yes", cmd)
        self.assertEqual(cmd[cmd.index("-L") + 1], "127.0.0.1:40001:100.114.250.87:8390")
        self.assertEqual(cmd[cmd.index("-p") + 1], "2222")
        self.assertEqual(cmd[-2:], ["--", "becke@blade14"])

    def test_ssh_errors_in_plain_words(self):
        cases = {
            "becke@blade14: Permission denied (publickey,password).": "SSH key",
            "Host key verification failed.": "Run 'ssh becke@blade14' once",
            "ssh: Could not resolve hostname blade14: Name or service not known": "was not found",
            "ssh: connect to host blade14 port 22: Connection refused": "no SSH server runs there",
            "ssh: connect to host blade14 port 22: Connection timed out": "may be asleep",
            "ssh: connect to host blade14 port 22: No route to host": "may be asleep",
        }
        for stderr, words in cases.items():
            popen = mock.Mock(return_value=FakeProc(stderr.encode() + b"\n"))
            t = remote.SshTunnel("becke@blade14", "127.0.0.1", 8188, popen=popen, ssh="ssh")
            with self.assertRaises(BackendUnavailable, msg=stderr) as caught:
                t.open(timeout=5)
            self.assertIn(words, str(caught.exception), stderr)

    def test_the_tunnel_is_ready_when_its_port_accepts(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        proc = FakeProc(exits=False)
        with mock.patch.object(remote, "_free_local_port", return_value=port):
            t = remote.SshTunnel("becke@blade14", "127.0.0.1", 8188, popen=mock.Mock(return_value=proc), ssh="ssh")
            self.assertEqual(t.open(timeout=5), f"http://127.0.0.1:{port}")
        self.assertTrue(t.alive())
        t.close()
        self.assertTrue(proc.terminated)
        self.assertFalse(t.alive())
        listener.close()

    def test_tunnels_left_by_a_dead_openfill_are_ended(self):
        from openfill import supervisor

        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp)
            (records / "4001.json").write_text(json.dumps({"owner": 999999}), encoding="utf-8")  # owner gone
            (records / "4002.json").write_text(json.dumps({"owner": __import__("os").getpid()}), encoding="utf-8")
            (records / "4003.json").write_text(json.dumps({"owner": 999999}), encoding="utf-8")  # not ssh now
            alive = {4001, 4002, 4003, __import__("os").getpid()}
            names = {4001: "ssh", 4002: "ssh", 4003: "firefox"}
            killed = []
            with (
                mock.patch.object(supervisor, "pid_alive", lambda pid: pid in alive),
                mock.patch.object(supervisor, "process_name", lambda pid: names.get(pid)),
            ):
                self.assertEqual(remote.reap_tunnels(records, killed.append), [4001])
            self.assertEqual(killed, [4001])
            self.assertEqual(sorted(p.name for p in records.iterdir()), ["4002.json"])

    def test_an_open_tunnel_is_noted_and_the_note_removed(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        proc = FakeProc(exits=False)
        proc.pid = 4242
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(remote, "_free_local_port", return_value=listener.getsockname()[1]),
        ):
            t = remote.SshTunnel("h", "127.0.0.1", 1, popen=lambda *a, **k: proc, ssh="ssh", records=Path(tmp))
            t.open(timeout=5)
            self.assertTrue((Path(tmp) / "4242.json").is_file())
            t.close()
            self.assertFalse((Path(tmp) / "4242.json").exists())
        listener.close()

    def test_no_ssh_program(self):
        with mock.patch.object(remote.sys, "platform", "linux"), self.assertRaises(BackendUnavailable) as caught:
            remote.ssh_executable(lambda _name: None)
        self.assertIn("OpenSSH Client", str(caught.exception))

    def test_nothing_behind_the_tunnel(self):
        rl = link(ssh="becke@blade14")
        rl.tunnel = SimpleNamespace(
            stderr_text=lambda: "channel 2: open failed: connect failed: Connection refused", close=lambda: None
        )
        self.assertIn("nothing answers behind it on port 8188", rl.explain("reset"))

    def test_the_ui_thread_does_not_open_the_tunnel(self):
        made = []

        class Tunnel:
            url = ""

            def __init__(self, *a, **k):
                made.append(self)
                self.opened = 0

            def alive(self):
                return False

            def open(self, **_k):
                self.opened += 1
                raise BackendUnavailable("asleep")

            def close(self):
                pass

        rl = link(ssh="becke@blade14", tunnel_factory=Tunnel)
        self.assertEqual(rl.url(), "http://127.0.0.1:9")  # this test runs on the main thread
        self.assertEqual(made[0].opened, 0)
        worker = threading.Thread(target=rl.url)
        worker.start()
        worker.join()
        self.assertEqual(made[0].opened, 1)
        worker = threading.Thread(target=rl.url)  # failed just now: not again before the pause
        worker.start()
        worker.join()
        self.assertEqual(made[0].opened, 1)


# ------------------------------------------------------------------- App ----


class AppTests(GpuServerCase):
    def setUp(self):
        super().setUp()
        from openfill import app as app_mod

        profile = self.root / "profile"
        (profile / "openfill").mkdir(parents=True)
        local_comfy = self.root / "local-comfy"  # a ComfyUI on this computer that must never be used
        (local_comfy / "models" / "checkpoints").mkdir(parents=True)
        (local_comfy / "main.py").write_text("raise SystemExit('must not run')", encoding="utf-8")
        (profile / "openfill" / "config.json").write_text(
            json.dumps(
                {
                    "backend": "remote",
                    "remote_url": self.url,
                    "remote_token": TOKEN,
                    "comfy_dir": str(local_comfy),
                    "autostart": True,
                }
            ),
            encoding="utf-8",
        )
        io = SimpleNamespace(profile_dir=lambda: str(profile), message=lambda _t: None)
        self.spawned = []
        with mock.patch.object(app_mod.local_models, "scan", side_effect=AssertionError("scanned locally")):
            self.app = app_mod.App(interactive=False, io=io, runner_cls=object)
        self.app.server._spawn_detached = self.spawned.append
        self.log_file = str(profile / "openfill" / "openfill.log")

    def tearDown(self):
        for handler in list(self.app.log.handlers):
            if getattr(handler, "baseFilename", None) == self.log_file:
                handler.close()
                self.app.log.removeHandler(handler)
        cat.register_remote_tree(None, self.app.paths.base / "remote-gpu" / "models")
        super().tearDown()

    def test_models_come_from_the_gpu_computer(self):
        rows = {r["id"]: r for r in self.app.model_rows()}
        self.assertTrue(rows["flux1-fill-dev-gguf-q6"]["installed"])
        self.assertTrue(rows["sd15-inpainting"]["installed"])
        self.assertFalse(rows["mat-places512"]["installed"])
        self.assertEqual(rows["lama-big"]["missing_nodes"], ["openfill-nodes"])
        _entry, spec = self.app.resolve_model("flux1-fill-dev-gguf-q6")
        self.assertEqual(spec.unet, "flux1-fill-dev-Q6_K.gguf")
        self.assertEqual(self.spawned, [])
        self.assertFalse((self.app.paths.base / "remote-gpu").exists())

    def test_node_installs_happen_on_the_gpu_computer(self):
        from openfill.backend import ModelMissing

        with self.assertRaises(ModelMissing):
            self.app.resolve_model("lama-big")
        self.assertFalse((self.root / "local-comfy" / "custom_nodes" / "openfill-nodes").exists())
        self.assertTrue(self.app.queue_node_install("lama-big"))
        for _ in range(100):
            if (self.nodes / "openfill-nodes" / "VERSION").is_file() and not self.server.tasks.snapshot()["current"]:
                break
            time.sleep(0.05)
        self.app.refresh_remote_tree(force=True)
        entry, _spec = self.app.resolve_model("lama-big")
        self.assertEqual(entry.id, "lama-big")
        self.app.server._remote_status = (0.0, {})
        history = self.app.task_status()["history"]
        self.assertEqual(history[0]["state"], "done")
        self.assertEqual(self.spawned, [])

    def test_nothing_is_downloaded_removed_or_started_here(self):
        from openfill.backend import BackendError, ModelMissing

        with self.assertRaises(ModelMissing) as caught:
            self.app.queue_download("mat-places512")
        self.assertIn("download mat-places512", str(caught.exception))
        with self.assertRaises(BackendError):
            self.app.remove_model("sd15-inpainting")
        with self.assertRaises(BackendUnavailable):
            self.app.server.start()
        with self.assertRaises(BackendUnavailable):
            self.app.server.queue_task({"type": "download"})
        self.assertIsNone(self.app.queue_opencv_install())
        self.assertFalse(self.app.server.request_stop())
        self.assertIn("GPU computer", self.app.stop_backend())
        self.assertEqual(self.app.server.pending_tasks(), [])
        self.assertEqual(self.spawned, [])

    def test_hardware_is_the_gpu_computers(self):
        hw = self.app.hardware()
        self.assertIn("RTX 4070", hw.gpu_name or "")
        self.assertEqual(hw.vram_bytes, 8 * 2**30)
        self.assertEqual(hw.gpu_source, "comfyui")

    def test_backend_check_and_a_down_server_never_fall_back(self):
        backend = self.app.backend()
        self.assertEqual(backend.url, self.url)
        self.assertIn("GPU computer", backend.connection_lost)
        self.assertTrue(backend.health().ok)
        self.comfy.stop()
        self.server.comfy._checked = 0.0
        with self.assertRaises(BackendUnavailable) as caught:
            self.app.backend()
        self.assertIn("its ComfyUI does not", str(caught.exception))
        self.assertEqual(self.spawned, [])
        self.comfy = FakeComfy(self.nodes)


class ConnectionLostTests(unittest.TestCase):
    def test_a_job_whose_connection_drops_says_so(self):
        from openfill.backends.comfy import ComfyBackend

        client = mock.Mock()
        client.queue_prompt.return_value = "p1"
        client.wait_for.side_effect = ComfyApiError("GET /history/p1 failed: timed out")
        client.is_alive.return_value = False
        client.last_model_files.return_value = set()
        backend = ComfyBackend("http://x", client=client, connection_lost="lost the GPU computer")
        with self.assertRaises(BackendUnavailable) as caught:
            backend._run({"1": {"class_type": "KSampler", "inputs": {"steps": 2}}}, 2, 1, 4096, None)
        self.assertEqual(str(caught.exception), "lost the GPU computer")


class WebSocketHeaderTests(unittest.TestCase):
    def test_extra_handshake_headers_are_sent(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        got = {}

        def serve():
            conn, _ = listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(1024)
            got["request"] = data
            conn.close()

        thread = threading.Thread(target=serve)
        thread.start()
        ws = WebSocket(f"ws://127.0.0.1:{listener.getsockname()[1]}/ws", 5, {"Authorization": "Bearer abc"})
        with self.assertRaises(ConnectionError):
            ws.connect()
        thread.join()
        listener.close()
        self.assertIn(b"\r\nAuthorization: Bearer abc\r\n", got["request"])
        self.assertTrue(got["request"].endswith(b"\r\n\r\n"))


if __name__ == "__main__":
    unittest.main()
