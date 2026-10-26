# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""urllib-only client for the ComfyUI HTTP and WebSocket API."""

from __future__ import annotations

import json
import socket
import time
import uuid
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .websocket import OP_BINARY, OP_TEXT, WebSocket, WebSocketError, ws_url

ProgressCallback = Callable[[float, str], None]

# A server on this machine accepts a TCP connection within milliseconds even while it is busy (the
# kernel completes the handshake). On Windows a refused loopback connection is only reported after
# about 2 s of SYN retries, so the health check first probes the port with this short timeout.
LOOPBACK_PROBE_TIMEOUT = 0.5
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ComfyApiError(RuntimeError):
    """An HTTP or protocol error from ComfyUI (``status`` 0 when the server was not reached)."""

    def __init__(
        self, message: str, status: int = 0, node_errors: dict | None = None, payload: dict | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.node_errors = node_errors or {}
        self.payload = payload or {}


# What PyTorch says when it was built without the graphics card's architecture.
UNSUPPORTED_GPU_SIGNS = (
    "no kernel image is available",
    "cudaerrornokernelimagefordevice",
    "is not compatible with the current pytorch installation",
    "hip error: invalid device function",  # ROCm: PyTorch has no code for this AMD card
    "hiperrornobinaryforgpu",
)


def unsupported_gpu_line(log_text: str) -> str:
    """PyTorch's 'not compatible with the current PyTorch installation' warning from ComfyUI's log, or ""."""
    for line in (log_text or "").splitlines():
        if any(sign in line.lower() for sign in UNSUPPORTED_GPU_SIGNS):
            return " ".join(line.split())[:300]
    return ""


class ComfyExecutionError(ComfyApiError):
    """``execution_error`` event from the server (includes the exception type)."""

    def __init__(self, message: str, exception_type: str = "", node_type: str = "", traceback: str = "") -> None:
        super().__init__(message)
        self.exception_type = exception_type
        self.node_type = node_type
        self.traceback = traceback

    @property
    def is_out_of_memory(self) -> bool:
        """True when the server ran out of GPU (or CUDA) memory. Other CUDA errors are not: a
        PyTorch built without the card's architecture fails with 'CUDA error: no kernel image is
        available for execution on the device', which no smaller selection fixes."""
        text = f"{self.exception_type} {self!s}".lower()
        return "outofmemory" in text or "out of memory" in text

    @property
    def is_unsupported_gpu(self) -> bool:
        """True when ComfyUI's PyTorch has no code for this graphics card (too old or too new)."""
        text = f"{self.exception_type} {self!s} {self.traceback}".lower()
        return any(s in text for s in UNSUPPORTED_GPU_SIGNS)


class ComfyClient:
    """Minimal ComfyUI HTTP and websocket client (standard library only)."""

    def __init__(
        self, base_url: str = "http://127.0.0.1:8188", timeout: float = 30.0, client_id: str | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = client_id or uuid.uuid4().hex

    # low level -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except HTTPError as err:
            try:
                raw = err.read()
            finally:
                err.close()  # the error response holds the socket open otherwise (ResourceWarning)
            payload = {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                pass
            message = (
                payload.get("error", {}).get("message")
                if isinstance(payload.get("error"), dict)
                else payload.get("error") or raw[:300].decode("utf-8", "replace")
            )
            raise ComfyApiError(
                f"{method} {path} -> HTTP {err.code}: {message}", err.code, payload.get("node_errors"), payload
            ) from err
        except (URLError, TimeoutError, ConnectionError, OSError) as err:
            raise ComfyApiError(f"{method} {path} failed: {err}") from err

    def _json(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        raw = self._request(method, path, data, "application/json" if data else None, timeout)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as err:
            raise ComfyApiError(f"{method} {path}: invalid JSON response") from err

    # endpoints -----------------------------------------------------------

    def is_alive(self, timeout: float = 3.0) -> bool:
        """True when /system_stats answers within ``timeout`` seconds."""
        if not self._loopback_port_open():
            return False
        try:
            self._request("GET", "/system_stats", timeout=timeout)
            return True
        except ComfyApiError:
            return False

    def _loopback_port_open(self) -> bool:
        """False only when the server is on this machine and nothing accepts on its port."""
        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        if host not in _LOOPBACK_HOSTS:
            return True
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return True
        try:
            with socket.create_connection((host, port), timeout=LOOPBACK_PROBE_TIMEOUT):
                return True
        except OSError:
            return False

    def system_stats(self) -> dict:
        """GET /system_stats (versions, devices, VRAM)."""
        return self._json("GET", "/system_stats")

    def object_info(self, node_class: str | None = None) -> dict:
        """GET /object_info, or one node class ({} when that class is not installed)."""
        path = "/object_info" if node_class is None else f"/object_info/{node_class}"
        try:
            return self._json("GET", path)
        except ComfyApiError as err:
            if node_class is not None and err.status == 404:
                return {}
            raise

    def combo_options(self, node_class: str, input_name: str) -> list[str]:
        """Enumerated options for ``input_name`` of ``node_class`` (or [])."""
        info = self.object_info(node_class).get(node_class)
        try:
            required = info["input"]["required"]
            options = required[input_name][0]
            return [str(o) for o in options] if isinstance(options, list) else []
        except (KeyError, TypeError, IndexError):
            return []

    def upload_image(self, filename: str, data: bytes, subfolder: str = "openfill", overwrite: bool = True) -> str:
        """POST multipart to ``/upload/image``; returns ``subfolder/filename``."""
        boundary = "----OpenFill" + uuid.uuid4().hex
        filename, subfolder = _header_safe(filename), _header_safe(subfolder)
        fields = {"type": "input", "subfolder": subfolder, "overwrite": "true" if overwrite else "false"}
        body = bytearray()
        for key, value in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'
        ).encode()
        body += data
        body += f"\r\n--{boundary}--\r\n".encode()
        raw = self._request("POST", "/upload/image", bytes(body), f"multipart/form-data; boundary={boundary}")
        info = json.loads(raw.decode("utf-8"))
        name = info.get("name", filename)
        sub = info.get("subfolder", subfolder)
        return f"{sub}/{name}" if sub else name

    def queue_prompt(self, graph: dict) -> str:
        """POST a workflow graph to /prompt; returns its prompt id."""
        payload = {"prompt": graph, "client_id": self.client_id}
        info = self._json("POST", "/prompt", payload)
        if "prompt_id" not in info:
            raise ComfyApiError(
                f"/prompt did not return a prompt_id: {info}", payload=info, node_errors=info.get("node_errors")
            )
        return info["prompt_id"]

    # How long the server may stay unreachable while OpenFill waits for a result before the job
    # fails. A busy ComfyUI on Windows (a model loading, a large file being written) sometimes
    # lets a loopback connection time out once although it is still running the job.
    HISTORY_GRACE_S = 90.0

    def _history_patiently(self, prompt_id: str, unreachable: dict[str, float]) -> dict | None:
        """:meth:`history`, but a server that is not reached (no HTTP answer at all) counts as
        "no entry yet" until it has been unreachable for ``HISTORY_GRACE_S``."""
        try:
            entry = self.history(prompt_id)
        except ComfyApiError as err:
            if err.status:
                raise
            since = unreachable.setdefault("since", time.monotonic())
            if time.monotonic() - since > self.HISTORY_GRACE_S:
                raise
            return None
        unreachable.clear()
        return entry

    def history(self, prompt_id: str) -> dict | None:
        """The /history entry of ``prompt_id``, or None while it has none."""
        info = self._json("GET", f"/history/{prompt_id}")
        return info.get(prompt_id)

    def last_model_files(self) -> set[str]:
        """Model file names used by the most recent prompt in the server's
        history (empty when it has none, e.g. right after a start)."""
        info = self._json("GET", "/history?max_items=1")
        names: set[str] = set()
        for entry in (info or {}).values():
            prompt = entry.get("prompt") if isinstance(entry, dict) else None
            graph = prompt[2] if isinstance(prompt, list) and len(prompt) > 2 else {}
            for node in (graph or {}).values():
                for key, value in ((node or {}).get("inputs") or {}).items():
                    if key in MODEL_INPUTS and isinstance(value, str):
                        names.add(value)
        return names

    def queue(self) -> dict:
        """GET /queue (running and pending prompts)."""
        return self._json("GET", "/queue")

    def interrupt(self) -> None:
        """POST /interrupt: stop the prompt that is running now."""
        self._json("POST", "/interrupt", {})

    def view(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        """Download an output (or input/temp) image through /view."""
        query = urlencode({"filename": filename, "subfolder": subfolder, "type": folder_type})
        return self._request("GET", f"/view?{query}", timeout=max(self.timeout, 120))

    def free(self, unload_models: bool = True, free_memory: bool = True) -> None:
        """Ask the server to release models and VRAM."""
        self._json("POST", "/free", {"unload_models": unload_models, "free_memory": free_memory})

    # job tracking --------------------------------------------------------

    def clear_queue(self) -> None:
        """Remove every pending prompt from the queue."""
        self._json("POST", "/queue", {"clear": True})

    def wait_for(
        self,
        prompt_id: str,
        total_steps: int,
        deadline: float,
        progress: ProgressCallback | None = None,
        poll_interval: float = 1.0,
        preview: Callable[[bytes], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        activity_extension: float = 0.0,
    ) -> dict:
        """Block until ``prompt_id`` finishes; return ``history[prompt_id]``.

        With ``activity_extension`` the deadline moves to at least that many
        seconds after every websocket message (progress, node or preview), so
        a job that keeps reporting progress is never cut off by the estimate.

        Uses the websocket for step-level progress (and binary preview frames,
        handed raw to ``preview``) and falls back to polling ``/history`` when
        it is not available. ``cancel()`` is polled every loop; when it
        returns True the job is interrupted, the queue cleared and
        :class:`ComfyApiError` ("cancelled") raised. Raises
        :class:`ComfyApiError` on timeout and :class:`ComfyExecutionError`
        on failure.
        """
        ws: WebSocket | None = None
        try:
            ws = WebSocket(ws_url(self.base_url, self.client_id), timeout=5.0)
            ws.connect()
            ws.settimeout(poll_interval)
        except (WebSocketError, OSError):
            ws = None
        done_steps = 0
        unreachable: dict[str, float] = {}  # since when /history has not answered
        queued_text = "Waiting for the AI backend..."
        if progress:
            progress(0.0, queued_text)
        last_poll = 0.0
        try:
            while True:
                now = time.monotonic()
                if now > deadline:
                    raise ComfyApiError("timed out waiting for the backend")
                if cancel and cancel():
                    for action in (self.interrupt, self.clear_queue):
                        try:
                            action()
                        except ComfyApiError:
                            pass
                    raise ComfyApiError("cancelled by the user")
                if ws is not None:
                    try:
                        opcode, payload = ws.recv()
                    except TimeoutError:
                        opcode, payload = None, b""
                    except (WebSocketError, OSError):
                        ws = None
                        opcode, payload = None, b""
                    if opcode is not None and activity_extension > 0:
                        deadline = max(deadline, time.monotonic() + activity_extension)
                    if opcode == OP_BINARY and preview is not None:
                        try:
                            preview(payload)
                        except Exception:  # noqa: BLE001 - previews are best effort
                            pass
                    if opcode == OP_TEXT:
                        msg = json.loads(payload.decode("utf-8"))
                        kind, data = msg.get("type"), msg.get("data") or {}
                        if data.get("prompt_id") not in (None, prompt_id) and kind != "status":
                            continue
                        if kind == "progress":
                            value, maximum = data.get("value", 0), max(1, data.get("max", 1))
                            frac = (done_steps + value) / max(total_steps, maximum)
                            if progress:
                                progress(min(0.99, frac), f"Generating... step {value}/{maximum}")
                            if value >= maximum:
                                done_steps += maximum
                        elif kind == "executing":
                            node = data.get("node")
                            if node is None and data.get("prompt_id") == prompt_id:
                                break  # finished
                            if progress and node is not None:
                                progress(min(0.99, done_steps / max(1, total_steps)), f"Running node {node}...")
                        elif kind == "execution_error" and data.get("prompt_id") == prompt_id:
                            raise ComfyExecutionError(
                                data.get("exception_message", "execution error"),
                                data.get("exception_type", ""),
                                data.get("node_type", ""),
                                "\n".join(data.get("traceback", []) or []),
                            )
                        elif kind == "execution_interrupted" and data.get("prompt_id") == prompt_id:
                            raise ComfyApiError("generation was interrupted")
                # Poll the history as a safety net (and as the only signal
                # when the websocket is unavailable).
                if now - last_poll >= poll_interval:
                    last_poll = now
                    if ws is None:
                        time.sleep(poll_interval)
                    entry = self._history_patiently(prompt_id, unreachable)
                    if entry:
                        status = entry.get("status", {})
                        if status.get("status_str") == "error":
                            raise ComfyExecutionError(_status_message(status))
                        break
        finally:
            if ws is not None:
                ws.close()
        entry = None
        for _ in range(30):
            entry = self._history_patiently(prompt_id, unreachable)
            if entry:
                break
            time.sleep(0.5)
        if not entry:
            raise ComfyApiError("the backend finished but produced no history entry")
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise ComfyExecutionError(_status_message(status))
        if progress:
            progress(1.0, "Done")
        return entry

    def output_images(self, history_entry: dict, node_id: str | None = None) -> list[bytes]:
        """Download every image produced by ``node_id`` (or all SaveImage nodes)."""
        outputs = history_entry.get("outputs", {})
        images: list[bytes] = []
        for nid, out in outputs.items():
            if node_id is not None and nid != node_id:
                continue
            for img in out.get("images", []):
                if img.get("type") != "output":
                    continue
                images.append(self.view(img["filename"], img.get("subfolder", ""), "output"))
        return images


# Node inputs that name a model file (used to tell a warm model from a cold one).
MODEL_INPUTS = frozenset(
    ("ckpt_name", "unet_name", "clip_name", "clip_name1", "clip_name2", "vae_name", "model_name", "head", "patch")
)


def _status_message(status: dict) -> str:
    for msg in status.get("messages", []):
        if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
            data = msg[1] or {}
            return f"{data.get('exception_type', '')}: {data.get('exception_message', 'error')}"
    return "execution failed"


def _header_safe(value: str) -> str:
    """Make a value safe inside a quoted multipart header parameter: no quotes,
    backslashes, CR/LF or other control characters, ASCII only."""
    out = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in str(value))
    return out or "_"
