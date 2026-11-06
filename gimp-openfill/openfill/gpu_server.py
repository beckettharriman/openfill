# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""OpenFill GPU server: lets OpenFill on another computer use this computer's graphics card.

Run it on the computer with the graphics card (see docs/remote-gpu.md)::

    python openfill/gpu_server.py serve --comfy-dir "C:\\AI\\ComfyUI\\ComfyUI_windows_portable"

It starts ComfyUI on 127.0.0.1 only (or uses one that runs there, ``--comfy-url``) and puts a
small proxy in front of it that

- listens on one address only: the Tailscale address when there is one, else the one you pick
  (never every network at once, never a public address);
- lets in only requests that carry the pairing code, compared in constant time, with a pause
  after repeated wrong codes; the code is shown once and only its SHA-256 is stored;
- passes only the ComfyUI endpoints OpenFill uses (and the progress websocket), with size
  limits on uploads, so the rest of ComfyUI (and any node pack's web routes) stays private;
- tells OpenFill which node packs are installed, and installs the ones a catalog model needs.

Standard library only. ComfyUI itself never listens beyond this computer.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # run as a file: make "openfill" importable
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from openfill.gpu_server import main as _main

    raise SystemExit(_main())

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__

DEFAULT_PORT = 8390
DEFAULT_COMFY_PORT = 8188
STATE_FILENAME = "gpu-server.json"
LOG_FILENAME = "gpu-server.log"
COMFY_LOG_FILENAME = "comfyui.log"
TOKEN_BYTES = 24  # 32 characters of base64url
MAX_UPLOAD_MB = 256  # an 8K RGBA PNG is well below this
MAX_BODY = 32 * 1024 * 1024  # workflow graphs and the other JSON requests
MAX_OWN_BODY = 64 * 1024  # the GPU server's own requests
FAILURE_WINDOW = 60.0
MAX_FAILURES = 8  # wrong codes from one address within FAILURE_WINDOW before it is locked out
MAX_CONNECTIONS = 64
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_DNS = "100.100.100.100"

# The ComfyUI endpoints OpenFill uses, by method; everything else answers 404.
_NAME = r"[A-Za-z0-9_.+-]+"  # no "/" or "%": an encoded slash could reach another route
ALLOWED = {
    "GET": [
        re.compile(p)
        for p in (
            r"^/system_stats$",
            rf"^/object_info(/{_NAME})?$",
            rf"^/models(/{_NAME})?$",
            r"^/history(/[A-Za-z0-9-]+)?$",
            r"^/queue$",
            r"^/prompt$",
            r"^/view$",
        )
    ],
    "POST": [
        re.compile(p)
        for p in (
            r"^/upload/image$",
            r"^/prompt$",
            r"^/queue$",
            r"^/interrupt$",
            r"^/free$",
        )
    ],
}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
# Never passed on to ComfyUI: the pairing code, and what would make ComfyUI's own origin check
# see a cross-site request.
DROP_UPSTREAM = {"host", "authorization", "x-openfill-token", "origin", "referer", "cookie"}


# ---------------------------------------------------------------- pairing ----


def default_home() -> Path:
    """Where the GPU server keeps its pairing state and logs."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "OpenFill" / "gpu-server"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "openfill" / "gpu-server"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_state(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((home / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(home: Path, state: dict[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = home / STATE_FILENAME
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, path)


def new_pairing(home: Path) -> str:
    """Make a new pairing code (the old one stops working); returns it. Only its hash is kept."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    state = read_state(home)
    state.update({"token_sha256": token_digest(token), "paired": time.strftime("%Y-%m-%d %H:%M:%S")})
    write_state(home, state)
    return token


def pairing_line(token: str, host: str, port: int) -> str:
    """What the user pastes into OpenFill: ``ofgpu://CODE@HOST:PORT``."""
    if ":" in host:
        host = f"[{host}]"
    return f"ofgpu://{token}@{host}:{port}"


class Gate:
    """Checks the pairing code in constant time and slows down guessing."""

    def __init__(self, digest: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.digest = bytes.fromhex(digest)
        self.clock = clock
        self._failures: dict[str, deque] = {}
        self._lock = threading.Lock()

    def locked(self, client: str) -> bool:
        with self._lock:
            times = self._failures.get(client)
            if not times:
                return False
            now = self.clock()
            while times and now - times[0] > FAILURE_WINDOW:
                times.popleft()
            return len(times) >= MAX_FAILURES

    def check(self, client: str, headers: Any) -> bool:
        auth = str(headers.get("Authorization") or "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else str(headers.get("X-OpenFill-Token") or "")
        given = hashlib.sha256(token.encode("utf-8", "replace")).digest()
        ok = bool(token) and hmac.compare_digest(given, self.digest)
        if not ok:
            with self._lock:
                self._failures.setdefault(client, deque()).append(self.clock())
        return ok


# --------------------------------------------------------------- addresses ----


def tailscale_ip(run: Callable[..., Any] = subprocess.run) -> str | None:
    """This computer's Tailscale IPv4 address, or None (the tailscale command, else the route
    to Tailscale's DNS address)."""
    exe = shutil.which("tailscale")
    if not exe and sys.platform == "win32":
        candidate = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        exe = str(candidate) if candidate.is_file() else None
    if exe:
        try:
            kw: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 5}
            if sys.platform == "win32":
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            out = run([exe, "ip", "-4"], **kw).stdout or ""
            for line in out.split():
                if _in_tailscale(line):
                    return line.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:  # no packet is sent: connect() on UDP only picks the route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((TAILSCALE_DNS, 53))
            ip = s.getsockname()[0]
        return ip if _in_tailscale(ip) else None
    except OSError:
        return None


def _in_tailscale(text: str) -> bool:
    try:
        return ipaddress.ip_address(text.strip()) in TAILSCALE_NET
    except ValueError:
        return False


def classify(host: str) -> str:
    """ "loopback", "tailscale", "lan", "any" (every network) or "public" for a bind address."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "public"
    if ip.is_unspecified:
        return "any"
    if ip.is_loopback:
        return "loopback"
    if ip.version == 4 and ip in TAILSCALE_NET:
        return "tailscale"
    if ip.is_private or ip.is_link_local:
        return "lan"
    return "public"


def local_addresses() -> list[str]:
    """IPv4 addresses of this computer (for the "which one?" question)."""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass
    for probe in (TAILSCALE_DNS, "192.168.0.1", "10.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 53))
                found.append(s.getsockname()[0])
        except OSError:
            pass
    out = []
    for ip in found:
        if ip not in out and classify(ip) in ("tailscale", "lan"):
            out.append(ip)
    return out


def choose_bind(requested: str | None, ask: Callable[[str], str] | None = None) -> tuple[str, str]:
    """``(address, kind)`` to listen on. Raises SystemExit with plain words when there is no
    safe choice: every network at once and public addresses are refused."""
    if requested:
        kind = classify(requested)
        if kind == "any":
            raise SystemExit(
                f"Refusing to listen on {requested} (every network at once). Pick one address: your Tailscale "
                "address, or 127.0.0.1 when OpenFill connects over SSH."
            )
        if kind == "public":
            raise SystemExit(
                f"Refusing to listen on {requested}: it is not a Tailscale, home network or loopback address. "
                "Never put ComfyUI on the internet; use Tailscale or SSH (see docs/remote-gpu.md)."
            )
        return requested.strip("[]"), kind
    ip = tailscale_ip()
    if ip:
        return ip, "tailscale"
    choices = local_addresses()
    text = (
        "Tailscale was not found on this computer. Pick the address to listen on:\n"
        + "".join(f"  {c}   (home network, not encrypted)\n" for c in choices)
        + "  127.0.0.1   (only through SSH from the other computer)\n"
    )
    if ask is None:
        raise SystemExit(text + "Start the server again with --bind <address>.")
    answer = ask(text + "Address: ").strip()
    if not answer:
        raise SystemExit("No address chosen.")
    return choose_bind(answer, None)


# ----------------------------------------------------------------- ComfyUI ----


class Comfy:
    """The ComfyUI behind the proxy: started and stopped here (``cmd``), or one that runs already."""

    def __init__(
        self,
        url: str,
        cmd: list[str] | None = None,
        cwd: str | None = None,
        log_path: Path | None = None,
        startup_timeout: float = 600.0,
        log: Callable[[str], None] = print,
    ) -> None:
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit(
                f"ComfyUI must listen on this computer only (127.0.0.1), not on {parts.hostname}: the GPU "
                "server is what the other computer talks to."
            )
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or DEFAULT_COMFY_PORT
        self.url = f"http://{self.host}:{self.port}"
        self.cmd = cmd
        self.cwd = cwd
        self.log_path = log_path
        self.startup_timeout = startup_timeout
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.state = "down"  # "down", "starting" or "up"
        self._checked = 0.0
        self._lock = threading.Lock()

    @property
    def managed(self) -> bool:
        return self.cmd is not None

    def answers(self, timeout: float = 2.0) -> bool:
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            conn.request("GET", "/system_stats")
            ok = conn.getresponse().status == 200
            conn.close()
            return ok
        except (OSError, http.client.HTTPException):
            return False

    def status(self) -> str:
        """ "up", "starting" or "down" (asked at most every 2 s)."""
        with self._lock:
            if self.state == "starting":
                return "starting"
            if self.managed and (self.proc is None or self.proc.poll() is not None):
                self.state = "down"
                return "down"
            if time.monotonic() - self._checked > 2.0:
                self._checked = time.monotonic()
                self.state = "up" if self.answers() else "down"
            return self.state

    def start(self, wait: bool = False) -> None:
        """Start ComfyUI (managed only) and, in a thread unless ``wait``, wait until it answers."""
        if not self.managed:
            return
        with self._lock:
            if self.state == "starting" or (self.proc is not None and self.proc.poll() is None):
                return
            if self.answers(timeout=1.0):
                raise SystemExit(
                    f"Another ComfyUI already runs on port {self.port}. Stop it, or start the GPU server "
                    f"with --comfy-url {self.url} to use that one."
                )
            if not port_free(self.host, self.port):
                # Windows keeps a killed ComfyUI's listening socket for a minute or two: use the
                # next free port (the proxy follows; the other computer never sees this port).
                self._use_port(next(p for p in range(self.port + 1, self.port + 50) if port_free(self.host, p)))
            self.state = "starting"
            kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "cwd": self.cwd}
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                kw["stdout"] = open(self.log_path, "ab")  # noqa: SIM115 - the child keeps it
                kw["stderr"] = subprocess.STDOUT
            if sys.platform == "win32":
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.log(f"starting ComfyUI: {' '.join(self.cmd or [])}")
            try:
                self.proc = subprocess.Popen(self.cmd or [], **kw)
            finally:
                if "stdout" in kw:
                    kw["stdout"].close()
        if wait:
            self._wait_ready()
        else:
            threading.Thread(target=self._wait_ready, name="comfy-start", daemon=True).start()

    def _use_port(self, port: int) -> None:
        if self.cmd and "--port" in self.cmd:
            self.cmd[self.cmd.index("--port") + 1] = str(port)
        else:
            raise SystemExit(f"port {self.port} on this computer is taken")
        self.log(f"port {self.port} is still taken; ComfyUI uses port {port}")
        self.port = port
        self.url = f"http://{self.host}:{port}"

    def _wait_ready(self) -> None:
        started = time.monotonic()
        while time.monotonic() - started < self.startup_timeout:
            if self.proc is None or self.proc.poll() is not None:
                break
            if self.answers():
                with self._lock:
                    self.state, self._checked = "up", time.monotonic()
                self.log(f"ComfyUI is ready after {time.monotonic() - started:.0f} s")
                return
            time.sleep(1.0)
        with self._lock:
            self.state = "down"
        self.log(f"ComfyUI did not start (see {self.log_path})")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        self.log(f"stopping ComfyUI (pid {proc.pid})")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        with self._lock:
            self.state = "down"

    def restart(self) -> None:
        self.stop()
        time.sleep(1.0)
        self.start()


def port_free(host: str, port: int) -> bool:
    """Nothing listens on ``host:port`` (a bind test: a socket Windows still holds counts as taken)."""
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def comfy_command(comfy_dir: str, port: int, extra: list[str]) -> tuple[list[str], str, str]:
    """``(command, working folder, ComfyUI's python)`` for the ComfyUI in ``comfy_dir``."""
    from .config import comfy_layout, comfy_problem

    layout = comfy_layout(comfy_dir)
    if layout is None:
        raise SystemExit(comfy_problem(comfy_dir))
    python = str(layout["python"])
    cmd = [python]
    if (Path(comfy_dir) / "python_embeded").is_dir():
        cmd.append("-s")  # as the portable's own start files do
    cmd += [str(layout["main"]), "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch", *extra]
    return cmd, str(layout["cwd"]), python


# ------------------------------------------------------------------- tasks ----


class Tasks:
    """Node pack installs for a model, one at a time, in the shape of OpenFill's task status."""

    def __init__(self, custom_nodes: Path | None, python: str | None, comfy: Comfy, log: Callable[[str], None]):
        self.custom_nodes = custom_nodes
        self.python = python
        self.comfy = comfy
        self.log = log
        self.status: dict[str, Any] = {"current": None, "history": []}
        self._lock = threading.Lock()
        self._catalog: list | None = None

    def catalog(self) -> list:
        if self._catalog is None:
            from . import catalog as cat

            self._catalog = cat.load_catalog()
        return self._catalog

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.status))

    def nodes(self) -> dict[str, str]:
        """Node pack folders in custom_nodes and their OpenFill VERSION ("" for none)."""
        out: dict[str, str] = {}
        if self.custom_nodes is None or not self.custom_nodes.is_dir():
            return out
        for child in sorted(self.custom_nodes.iterdir()):
            name = child.name
            if not child.is_dir() or name.startswith((".", "__")) or name.endswith((".disabled", ".openfill-new")):
                continue
            try:
                out[name] = (child / "VERSION").read_text(encoding="utf-8").strip()
            except OSError:
                out[name] = ""
        return out

    def install_for(self, model_id: str) -> dict[str, Any]:
        """Start installing what ``model_id`` (an id in this computer's catalog) needs; the
        node packs come from the catalog only, never from the request."""
        from . import catalog as cat

        if self.custom_nodes is None:
            raise ValueError("this GPU server does not know ComfyUI's custom_nodes folder (start it with --comfy-dir)")
        try:
            entry = cat.by_id(self.catalog(), model_id)
        except cat.CatalogError:
            raise ValueError(f"'{model_id}' is not in this computer's OpenFill catalog; update OpenFill here") from None
        missing = [n for n in entry.requires_nodes if cat.node_missing(self.custom_nodes, n)]
        if not missing:
            return {"task_id": "", "message": "everything this model needs is installed"}
        if any(not n.bundled for n in missing) and not self.python:
            raise ValueError("this GPU server does not know ComfyUI's Python (start it with --comfy-dir)")
        with self._lock:
            if self.status["current"]:
                raise RuntimeError("another install is running on the GPU computer; try again when it is done")
            task_id = f"{int(time.time() * 1000)}-install_nodes"
            self.status["current"] = {"task_id": task_id, "type": "install_nodes", "model_id": model_id}
        threading.Thread(target=self._install, args=(task_id, model_id, missing), daemon=True).start()
        return {"task_id": task_id, "message": "installing " + ", ".join(n.folder for n in missing)}

    def _install(self, task_id: str, model_id: str, nodes: list) -> None:
        from . import downloads

        state, message = "done", ""
        try:
            for n in nodes:
                self.log(f"installing node pack {n.folder} for {model_id}")
                downloads.install_custom_node(
                    n.repo, n.folder, n.commit, self.custom_nodes, Path(self.python or sys.executable), self.log
                )
            if self.comfy.managed:
                self.log("restarting ComfyUI so it loads the new node packs")
                self.comfy.restart()
                message = "installed; ComfyUI is restarting"
            else:
                message = "installed; restart ComfyUI on the GPU computer so it loads them"
        except Exception as err:  # noqa: BLE001 - reported to OpenFill
            state, message = "error", str(err)
            self.log(f"node install failed: {err}")
        with self._lock:
            entry = {
                "task_id": task_id,
                "type": "install_nodes",
                "model_id": model_id,
                "state": state,
                "message": message,
                "finished": time.time(),
            }
            self.status["history"] = [entry, *self.status["history"]][:20]
            self.status["current"] = None


# ------------------------------------------------------------------- proxy ----


class GpuServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(
        self,
        address: tuple[str, int],
        gate: Gate,
        comfy: Comfy,
        tasks: Tasks,
        max_upload: int = MAX_UPLOAD_MB * 1024 * 1024,
        log: Callable[[str], None] = print,
    ) -> None:
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        self.gate = gate
        self.comfy = comfy
        self.tasks = tasks
        self.max_upload = max_upload
        self.log = log
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: GpuServer
    server_version = "OpenFillGPU/" + __version__
    timeout = 120  # seconds a client may take to send its request

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's name
        pass  # no access log: paths are enough noise; problems are logged on their own

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    # ------------------------------------------------------------------------

    def _handle(self) -> None:
        if not self.server.slots.acquire(blocking=False):
            self._send_json(503, {"error": "too many connections"})
            return
        try:
            self._dispatch()
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        finally:
            self.server.slots.release()

    def _dispatch(self) -> None:
        client = self.client_address[0]
        gate = self.server.gate
        if gate.locked(client):
            self._send_json(429, {"error": "too many wrong pairing codes; wait a minute"})
            return
        if not gate.check(client, self.headers):
            time.sleep(0.25)  # each wrong code costs the guesser time
            self.server.log(f"refused a request from {client}: wrong or missing pairing code")
            self._send_json(401, {"error": "wrong or missing pairing code"})
            return
        path = self.path.partition("?")[0]
        if path.startswith("/openfill/"):
            self._own(path)
            return
        if path == "/ws" and self.command == "GET" and "websocket" in str(self.headers.get("Upgrade", "")).lower():
            if self._comfy_ready():
                self._websocket()
            return
        if not any(p.match(path) for p in ALLOWED.get(self.command, ())):
            self._send_json(404, {"error": "not available through the OpenFill GPU server"})
            return
        body = self._read_body(self.server.max_upload if path == "/upload/image" else MAX_BODY)
        if body is None:
            return
        if self._comfy_ready():
            self._forward(body)

    def _comfy_ready(self) -> bool:
        comfy = self.server.comfy
        state = comfy.status()
        if state == "down" and comfy.managed:
            self.server.log("ComfyUI is not running; starting it again")
            try:
                comfy.start()
            except SystemExit as err:
                self._send_json(502, {"error": str(err), "comfy": "down"})
                return False
            state = "starting"
        if state == "up":
            return True
        code = 503 if state == "starting" else 502
        self._send_json(code, {"error": f"ComfyUI on the GPU computer is {state}", "comfy": state})
        return False

    def _read_body(self, limit: int) -> bytes | None:
        if self.command not in ("POST", "PUT"):
            return None if self._has_body_anyway() else b""
        if "chunked" in str(self.headers.get("Transfer-Encoding", "")).lower():
            self._send_json(411, {"error": "send a Content-Length"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "bad Content-Length"})
            return None
        if length < 0 or length > limit:
            self._send_json(413, {"error": f"request too large (limit {limit // (1024 * 1024)} MB)"})
            self.close_connection = True
            return None
        chunks, left = [], length
        while left:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                raise ConnectionError("client went away during the upload")
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def _has_body_anyway(self) -> bool:
        if self.headers.get("Content-Length") not in (None, "", "0") or self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "unexpected request body"})
            return True
        return False

    def _upstream_headers(self) -> dict[str, str]:
        comfy = self.server.comfy
        out = {k: v for k, v in self.headers.items() if k.lower() not in DROP_UPSTREAM | HOP_BY_HOP}
        out["Host"] = f"{comfy.host}:{comfy.port}"
        return out

    def _forward(self, body: bytes) -> None:
        comfy = self.server.comfy
        headers = self._upstream_headers()
        if body or self.command in ("POST", "PUT"):
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(comfy.host, comfy.port, timeout=600)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as err:
            conn.close()
            self._send_json(502, {"error": f"ComfyUI on the GPU computer did not answer ({err})", "comfy": "down"})
            return
        try:
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP and key.lower() not in ("server", "date", "set-cookie"):
                    self.send_header(key, value)
            length = resp.getheader("Content-Length")
            if length is not None and length.isdigit():
                self.send_header("Content-Length", length)
                self.send_header("Connection", "close")
                self.end_headers()
                left = int(length)
                while left > 0:
                    chunk = resp.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
        finally:
            conn.close()
            self.close_connection = True

    def _websocket(self) -> None:
        """Pass ComfyUI's /ws (progress and previews) through, both ways, as raw bytes."""
        comfy = self.server.comfy
        try:
            up = socket.create_connection((comfy.host, comfy.port), timeout=10)
        except OSError as err:
            self._send_json(502, {"error": f"ComfyUI's websocket did not answer ({err})", "comfy": "down"})
            return
        self.close_connection = True
        try:
            lines = [f"GET {self.path} HTTP/1.1", f"Host: {comfy.host}:{comfy.port}"]
            for key, value in self.headers.items():
                if key.lower() not in DROP_UPSTREAM:
                    lines.append(f"{key}: {value}")
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            head = bytearray()
            while not head.endswith(b"\r\n\r\n"):
                byte = up.recv(1)
                if not byte or len(head) > 65536:
                    raise ConnectionError("ComfyUI closed the websocket handshake")
                head += byte
            self.wfile.write(bytes(head))
            self.wfile.flush()
            if b" 101 " not in bytes(head).split(b"\r\n", 1)[0]:
                return
            up.settimeout(None)
            self.connection.settimeout(None)
            done = threading.Event()

            def client_to_comfy() -> None:
                try:
                    while not done.is_set():
                        data = self.rfile.read1(65536)
                        if not data:
                            break
                        up.sendall(data)
                except OSError:
                    pass
                finally:
                    done.set()
                    _shutdown(up)

            pump = threading.Thread(target=client_to_comfy, name="ws-up", daemon=True)
            pump.start()
            try:
                while not done.is_set():
                    data = up.recv(65536)
                    if not data:
                        break
                    self.connection.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                _shutdown(self.connection)
                pump.join(timeout=5)
        finally:
            up.close()

    # the GPU server's own requests ---------------------------------------------------

    def _own(self, path: str) -> None:
        srv = self.server
        if path == "/openfill/ping" and self.command == "GET":
            self._send_json(
                200,
                {
                    "server": "openfill-gpu-server",
                    "version": __version__,
                    "comfy": srv.comfy.status(),
                    "managed": srv.comfy.managed,
                    "can_install": srv.tasks.custom_nodes is not None,
                },
            )
        elif path == "/openfill/inventory" and self.command == "GET":
            self._send_json(200, {"nodes": srv.tasks.nodes()})
        elif path == "/openfill/status" and self.command == "GET":
            self._send_json(200, srv.tasks.snapshot())
        elif path == "/openfill/install-nodes" and self.command == "POST":
            body = self._read_body(MAX_OWN_BODY)
            if body is None:
                return
            try:
                model_id = str(json.loads(body.decode("utf-8") or "{}").get("model_id") or "")
                self._send_json(200, srv.tasks.install_for(model_id))
            except (ValueError, AttributeError) as err:
                self._send_json(400, {"error": str(err)})
            except RuntimeError as err:
                self._send_json(409, {"error": str(err)})
        else:
            self._send_json(404, {"error": "not available through the OpenFill GPU server"})

    def _send_json(self, code: int, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass
        self.close_connection = True


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


# --------------------------------------------------------------------- CLI ----


class Log:
    """Prints and appends to gpu-server.log (never a pairing code)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def __call__(self, text: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
        with self._lock:
            print(line, flush=True)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpu_server.py",
        description="OpenFill GPU server: lets OpenFill on another computer use this computer's graphics card.",
    )
    p.add_argument("--home", help="folder for the pairing state and logs (default: your user settings folder)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="start ComfyUI and the GPU server")
    where = s.add_mutually_exclusive_group(required=True)
    where.add_argument("--comfy-dir", help="the ComfyUI folder (portable build or a checkout with a venv)")
    where.add_argument("--comfy-url", help="use a ComfyUI that runs already on 127.0.0.1, e.g. http://127.0.0.1:8188")
    s.add_argument("--comfy-port", type=int, default=DEFAULT_COMFY_PORT, help="port for ComfyUI on 127.0.0.1")
    s.add_argument("--comfy-arg", action="append", default=[], help="extra ComfyUI option (repeat for more)")
    s.add_argument("--custom-nodes", help="ComfyUI's custom_nodes folder (with --comfy-url)")
    s.add_argument("--bind", help="the address to listen on (default: this computer's Tailscale address)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"the port to listen on (default {DEFAULT_PORT})")
    s.add_argument("--max-upload-mb", type=int, default=MAX_UPLOAD_MB, help="largest image upload")
    sub.add_parser("pair", help="make a new pairing code (the old one stops working)").add_argument(
        "--bind", help="the address OpenFill connects to (shown in the code)"
    )
    d = sub.add_parser("download", help="download a model of OpenFill's catalog into this computer's ComfyUI")
    d.add_argument("model_id")
    d.add_argument("--comfy-dir", required=True)
    d.add_argument("--hf-token-env", default="HF_TOKEN", help="environment variable holding a Hugging Face key")
    sub.add_parser("addresses", help="list the addresses the server could listen on")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.home) if args.home else default_home()
    if args.command == "addresses":
        ts = tailscale_ip()
        print(f"Tailscale: {ts or 'not found'}")
        for ip in local_addresses():
            print(f"{ip} ({classify(ip)})")
        return 0
    if args.command == "pair":
        host = args.bind or tailscale_ip() or "THIS-COMPUTER"
        token = new_pairing(home)
        print(_pairing_banner(pairing_line(token, host, DEFAULT_PORT)))
        return 0
    if args.command == "download":
        return download(args.model_id, args.comfy_dir, os.environ.get(args.hf_token_env, ""))
    return serve(args, home)


def _pairing_banner(line: str) -> str:
    return (
        "\nPairing code (shown only now; it works until you run 'pair' again):\n\n"
        f"    {line}\n\n"
        "Paste it into OpenFill on the other computer: Generative Fill Settings > Connection > Remote GPU "
        "pairing code (GIMP), or the Engine settings (PhotoCraft). Treat it like a password.\n"
    )


def serve(args: argparse.Namespace, home: Path, ask: Callable[[str], str] | None = None) -> int:
    log = Log(home / LOG_FILENAME)
    interactive = ask or (input if sys.stdin is not None and sys.stdin.isatty() else None)
    host, kind = choose_bind(args.bind, interactive)
    if args.comfy_dir:
        cmd, cwd, python = comfy_command(args.comfy_dir, args.comfy_port, list(args.comfy_arg))
        comfy = Comfy(f"http://127.0.0.1:{args.comfy_port}", cmd, cwd, home / COMFY_LOG_FILENAME, log=log)
        from .catalog import default_custom_nodes_dir

        nodes: Path | None = default_custom_nodes_dir(args.comfy_dir)
    else:
        comfy = Comfy(args.comfy_url, log=log)
        python = None
        nodes = Path(args.custom_nodes) if args.custom_nodes else None
    return run(host, kind, args.port, home, comfy, nodes, python, args.max_upload_mb, log)


def run(
    host: str,
    kind: str,
    port: int,
    home: Path,
    comfy: Comfy,
    custom_nodes: Path | None,
    python: str | None,
    max_upload_mb: int = MAX_UPLOAD_MB,
    log: Callable[[str], None] | None = None,
    ready: Callable[[GpuServer], None] | None = None,
) -> int:
    """Serve until Ctrl+C (or ``server.shutdown()`` from ``ready``); stops the ComfyUI it started."""
    log = log or Log(home / LOG_FILENAME)
    state = read_state(home)
    fresh = None
    if not state.get("token_sha256"):
        fresh = new_pairing(home)
        state = read_state(home)
    gate = Gate(state["token_sha256"])
    tasks = Tasks(custom_nodes, python, comfy, log)
    try:
        server = GpuServer((host, port), gate, comfy, tasks, max_upload_mb * 1024 * 1024, log)
    except OSError as err:
        raise SystemExit(f"Could not listen on {host}:{port} ({err.strerror or err}). Try another --port.") from err
    log(f"OpenFill GPU server {__version__} listening on {host}:{port} ({kind})")
    if kind == "lan":
        log(
            "WARNING: this is a home network address. Your images and the pairing code travel unencrypted. "
            "Prefer Tailscale (encrypted), or SSH from the other computer (bind 127.0.0.1)."
        )
    log(f"ComfyUI: {comfy.url} ({'started here' if comfy.managed else 'already running'}, this computer only)")
    if fresh:
        print(_pairing_banner(pairing_line(fresh, host, port)), flush=True)
    else:
        log("Pairing: use the code you got before (run 'pair' for a new one).")
    if comfy.managed:
        comfy.start()
    if ready is not None:
        ready(server)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()
        comfy.stop()
    return 0


def download(model_id: str, comfy_dir: str, hf_token: str = "") -> int:
    """Download a catalog model (and the companion models it needs) into ``comfy_dir``'s models."""
    from . import catalog as cat
    from . import downloads

    entries = cat.load_catalog()
    root = cat.default_models_root(comfy_dir)
    todo = [cat.by_id(entries, model_id)]
    for dep in todo[0].requires_models:
        if cat.installed_stand_in(todo[0], dep, entries, root) is None:
            todo.insert(0, cat.by_id(entries, dep))
    for entry in todo:
        if entry.gated and not hf_token:
            print(f"{entry.name} needs a Hugging Face key: accept the license at {entry.page}, then set HF_TOKEN.")
            return 2
        for f in entry.files:
            print(f"{entry.id}: {f.dest}")
            last = [0.0]

            def report(done: int, total: int | None, _speed: float = 0.0, _last: list = last) -> None:
                if time.monotonic() - _last[0] > 2:
                    _last[0] = time.monotonic()
                    print(f"  {done / 1e6:.0f} of {(total or 0) / 1e6:.0f} MB", flush=True)

            try:
                downloads.download_file(
                    f.url, root / f.dest, f.size, f.sha256, hf_token if f.gated else None, progress=report
                )
            except downloads.DownloadError as err:
                if f.optional:
                    print(f"  skipped: {err}")
                    continue
                print(f"Download failed: {err}")
                return 1
    print("Done. OpenFill on the other computer sees the model after Check backend.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
