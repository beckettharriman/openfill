# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Remote GPU, the editor's side: ComfyUI runs on another computer.

Two ways to reach it (see docs/remote-gpu.md):

- direct: the OpenFill GPU server (``gpu_server.py``) on the other computer, over Tailscale or
  the home network, with the pairing code it showed once;
- SSH: OpenFill opens ``ssh -N -L`` itself (the system's ssh, key login) to that computer and
  talks to its ComfyUI (or its GPU server) through the tunnel.

OpenFill never starts, stops or installs anything on this computer in Remote GPU mode, and never
falls back to the local ComfyUI or a cloud service when the remote one does not answer: it says
what is wrong instead.
"""

from __future__ import annotations

import atexit
import errno
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import catalog as cat
from .backend import BackendUnavailable, GenerationCancelled
from .comfy_api import ComfyApiError, ComfyClient, auth_headers, set_token

PAIRING_SCHEME = "ofgpu"
DEFAULT_PORT = 8390  # the GPU server's port when an address names none
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._%+-]*(@[A-Za-z0-9_][A-Za-z0-9._%-]*)?(:[0-9]{1,5})?$")
PROBE_TIMEOUT = 8.0  # a computer across the network: more than the 3 s of a local check
STATUS_TIMEOUT = 1.5  # the Models tab polls the GPU server's task status from its UI thread
TREE_TTL = 15.0  # seconds the model and node list of the remote ComfyUI is reused
RETRY_AFTER_FAILURE = 20.0  # an unreachable computer is not asked again before this

# ComfyUI model folder of a catalog path -> the /models/<key> lists that hold its files. ComfyUI
# keeps "unet" and "clip" as legacy names of diffusion_models and text_encoders; ComfyUI-GGUF
# lists .gguf files under its own keys.
FOLDER_KEYS = {
    "unet": ("diffusion_models", "unet", "unet_gguf"),
    "diffusion_models": ("diffusion_models", "unet", "unet_gguf"),
    "clip": ("text_encoders", "clip", "clip_gguf"),
    "text_encoders": ("text_encoders", "clip", "clip_gguf"),
}
# A node class each catalog node pack adds: loaded in the remote ComfyUI = the pack works there.
NODE_MARKERS = {
    "comfyui-inpaint-nodes": "INPAINT_LoadInpaintModel",
    "ComfyUI-GGUF": "UnetLoaderGGUF",
    "openfill-nodes": "OpenFillLamaNative",
}

SETUP_HINT = (
    "Open Generative Fill Settings > Connection and paste the pairing code the OpenFill GPU server "
    "showed on the GPU computer (see docs/remote-gpu.md)."
)


class RemoteSetupError(ValueError):
    """The Remote GPU settings cannot be used as typed (plain words)."""


# ---------------------------------------------------------------- settings ----


def parse_address(text: str) -> tuple[str, str]:
    """``(base_url, token)`` from what the user pasted: a pairing code
    ``ofgpu://TOKEN@HOST:PORT``, or an address ``http://HOST:PORT`` / ``HOST:PORT`` / ``HOST``
    (token "" then). A missing port means the GPU server's default, 8390."""
    text = (text or "").strip()
    if not text:
        return "", ""
    token = ""
    if text.lower().startswith(PAIRING_SCHEME + "://"):
        parts = urlsplit(text)
        token = parts.username or ""
        if not TOKEN_RE.match(token):
            raise RemoteSetupError("The pairing code is not complete. Copy the whole line the GPU server showed.")
        text = "http://" + parts.netloc.rpartition("@")[2]
    elif "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteSetupError(f"'{text}' is not an address OpenFill can use. {SETUP_HINT}")
    if parts.username or parts.password:
        raise RemoteSetupError("Put the pairing code in the ofgpu:// form the GPU server shows, not in a web address.")
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as err:
        raise RemoteSetupError(f"The port in '{text}' is not a number.") from err
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"  # IPv6
    return f"{parts.scheme}://{host}:{port}", token


def same_address(a: str, b: str) -> bool:
    """``a`` and ``b`` name the same GPU server (scheme, host and port compared)."""
    try:
        return bool(a) and bool(b) and parse_address(a)[0].lower() == parse_address(b)[0].lower()
    except RemoteSetupError:
        return False


def parse_ssh_target(text: str) -> tuple[str, int | None]:
    """``("user@host", port)`` from ``user@host``, ``user@host:2222`` or a ~/.ssh/config name.
    Refuses anything ssh could read as an option."""
    text = (text or "").strip()
    if text.lower().startswith("ssh://"):
        text = text[6:].rstrip("/")
    if not text or not _SSH_TARGET_RE.match(text):
        raise RemoteSetupError(
            f"'{text}' is not an SSH login OpenFill can use. Type it as user@computer, for example "
            "becke@gaming-pc, or user@computer:2222 for another SSH port."
        )
    host, _, port = text.partition(":")
    return host, (int(port) if port else None)


def is_loopback_host(host: str) -> bool:
    return host.strip("[]") in ("127.0.0.1", "localhost", "::1")


# -------------------------------------------------------------- the tunnel ----


def ssh_executable(which: Callable[[str], str | None] = shutil.which) -> str:
    """The system ssh: on PATH, else Windows' own OpenSSH client."""
    found = which("ssh")
    if found:
        return found
    if sys.platform == "win32":
        builtin = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if builtin.is_file():
            return str(builtin)
    raise BackendUnavailable(
        "Remote GPU over SSH needs the ssh program, and it was not found. On Windows 10 and 11 turn on "
        "Settings > System > Optional features > OpenSSH Client; on Linux install openssh-client."
    )


def explain_ssh(stderr: str, target: str) -> str:
    """What ssh's error output means, in plain words, with the next step."""
    text = stderr.lower()
    host = target.rpartition("@")[2]
    if "permission denied" in text:
        return (
            f"SSH refused the login to {target}. OpenFill logs in with an SSH key, never a password: "
            f"set one up (ssh-keygen, then copy the public key to {host}) and check that "
            f"'ssh {target}' works in a terminal without asking for a password."
        )
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return (
            f"SSH does not know {host} yet (or its key changed). Run 'ssh {target}' once in a terminal and "
            "answer yes after checking the fingerprint, then try again."
        )
    if "could not resolve hostname" in text or "name or service not known" in text:
        return f"The computer name '{host}' was not found. Check the spelling, or use its Tailscale or network address."
    if "connection refused" in text and "channel" not in text:
        return f"{host} answers, but no SSH server runs there. Turn on its OpenSSH Server, then try again."
    if "timed out" in text or "no route to host" in text or "network is unreachable" in text:
        return asleep_message(host)
    if "address already in use" in text or "cannot listen" in text:
        return "SSH could not open its local port. Try again; if it keeps happening, restart the editor."
    last = stderr.strip().splitlines()[-1] if stderr.strip() else "no output"
    return f"The SSH connection to {target} failed: {last}"


def asleep_message(host: str) -> str:
    return (
        f"The GPU computer {host} does not answer. It may be asleep, switched off, or not connected to "
        "Tailscale or your network, or the OpenFill GPU server is not running there. Check it, then press "
        "Check backend again."
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SshTunnel:
    """``ssh -N -L 127.0.0.1:<free port>:<dest_host>:<dest_port> target``, opened on demand
    and closed when this process ends."""

    def __init__(
        self,
        target: str,
        dest_host: str,
        dest_port: int,
        log: Any = None,
        popen: Callable[..., Any] = subprocess.Popen,
        ssh: str | None = None,
        records: Path | None = None,
    ) -> None:
        self.target, self.port = parse_ssh_target(target)
        self.dest_host = dest_host.strip("[]")
        self.dest_port = int(dest_port)
        self.log = log
        self._popen = popen
        self._ssh = ssh
        self.proc: Any = None
        self.local_port = 0
        self._stderr: list[str] = []
        self.records = records  # folder of <ssh pid>.json files, see reap_tunnels
        self._job: Any = None  # Windows: ssh ends with this process

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}" if self.local_port else ""

    def command(self, local_port: int) -> list[str]:
        cmd = [
            self._ssh or ssh_executable(),
            "-N",
            "-T",
            "-o",
            "BatchMode=yes",  # key login only: never a password prompt nobody can see
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{local_port}:{self._dest()}:{self.dest_port}",
        ]
        if self.port:
            cmd += ["-p", str(self.port)]
        return [*cmd, "--", self.target]

    def _dest(self) -> str:
        return f"[{self.dest_host}]" if ":" in self.dest_host else self.dest_host  # IPv6 in brackets for -L

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open(self, timeout: float = 25.0, cancel: Callable[[], bool] | None = None) -> str:
        """Start ssh and wait until its local port accepts; raises BackendUnavailable with ssh's reason."""
        if self.alive():
            return self.url
        self.close()
        port = _free_local_port()
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        cmd = self.command(port)
        if self.log:
            self.log.info("remote GPU: opening ssh tunnel %s", " ".join(cmd[1:]))
        try:
            self.proc = self._popen(cmd, **kwargs)
        except OSError as err:
            raise BackendUnavailable(f"ssh could not be started ({err}).") from err
        self._stderr = []
        self._remember()
        if getattr(self.proc, "stderr", None) is not None:
            threading.Thread(target=self._drain, name="openfill-ssh-stderr", daemon=True).start()
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if cancel and cancel():
                self.close()
                raise GenerationCancelled("cancelled while connecting to the GPU computer")
            if self.proc.poll() is not None:
                time.sleep(0.1)  # let the stderr reader catch the last line
                message = explain_ssh("".join(self._stderr), self.target)
                self.proc = None
                raise BackendUnavailable(message)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    self.local_port = port
                    return self.url
            except OSError:
                time.sleep(0.2)
        self.close()
        raise BackendUnavailable(asleep_message(self.target.rpartition("@")[2]))

    def stderr_text(self) -> str:
        return "".join(self._stderr)

    def _drain(self) -> None:
        stream = self.proc.stderr
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                self._stderr = [*self._stderr, line][-40:]
        except (OSError, ValueError):
            pass

    def _remember(self) -> None:
        """Note the ssh process, so a later start ends it if this process dies without closing it
        (GIMP ends a plug-in that hangs); on Windows a job object ends it with this process."""
        pid = int(getattr(self.proc, "pid", 0) or 0)
        if not pid:
            return
        if sys.platform == "win32":
            try:
                from .supervisor import _job_object_for_child

                self._job = _job_object_for_child(pid)
            except (OSError, AttributeError, ImportError):
                self._job = None
        if self.records is not None:
            try:
                self.records.mkdir(parents=True, exist_ok=True)
                (self.records / f"{pid}.json").write_text(json.dumps({"owner": os.getpid()}), encoding="utf-8")
            except OSError:
                pass

    def close(self) -> None:
        proc, self.proc = self.proc, None
        self.local_port = 0
        if proc is None:
            return
        if self.records is not None:
            try:
                (self.records / f"{getattr(proc, 'pid', 0)}.json").unlink()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - kill it then
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def reap_tunnels(records: Path, kill: Callable[[int], None] | None = None) -> list[int]:
    """End the ssh tunnels whose OpenFill process is gone (it crashed or was killed); returns
    their pids. Only processes named ssh are touched, so a reused pid is left alone."""
    from .supervisor import pid_alive, process_name

    ended: list[int] = []
    if not records.is_dir():
        return ended
    for record in records.glob("*.json"):
        try:
            pid = int(record.stem)
            owner = int(json.loads(record.read_text(encoding="utf-8")).get("owner") or 0)
        except (OSError, ValueError, AttributeError):
            pid, owner = 0, 0
        if owner and pid_alive(owner):
            continue
        if pid and pid_alive(pid) and (process_name(pid) or "").startswith("ssh"):
            try:
                (kill or _terminate)(pid)
                ended.append(pid)
            except OSError:
                pass
        try:
            record.unlink()
        except OSError:
            pass
    return ended


def _terminate(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        import signal

        os.kill(pid, signal.SIGTERM)


# ---------------------------------------------------------------- the link ----


def probe(url: str, timeout: float = PROBE_TIMEOUT, opener: Callable[..., Any] = urlopen) -> tuple[str, dict]:
    """Ask ``url`` who it is: ``(kind, info)``.

    kind: "gpu-server" (the OpenFill GPU server, ComfyUI up), "comfy-starting", "comfy-down"
    (the GPU server answers, its ComfyUI does not), "comfyui" (a ComfyUI without the GPU
    server), "bad-token", "locked" (too many wrong codes), "refused", "asleep", "no-name",
    "not-comfy" or "error"; ``info`` holds the server's answer or {"error": text}."""
    headers = {"Accept": "application/json", **auth_headers(url)}
    try:
        with opener(Request(url + "/openfill/ping", headers=headers), timeout=timeout) as resp:
            info = _json_or_empty(resp.read())
        state = str(info.get("comfy") or "")
        if state == "starting":
            return "comfy-starting", info
        return ("gpu-server" if state == "up" else "comfy-down"), info
    except HTTPError as err:
        info = _json_or_empty(_read_quietly(err))
        if err.code == 401:
            return "bad-token", info
        if err.code == 429:
            return "locked", info
        if err.code != 404:
            return "error", {"error": f"HTTP {err.code}", **info}
    except (URLError, OSError) as err:
        return _network_kind(err), {"error": str(err)}
    # 404: not the GPU server; a plain ComfyUI answers /system_stats
    try:
        with opener(Request(url + "/system_stats", headers=headers), timeout=timeout) as resp:
            info = _json_or_empty(resp.read())
        return ("comfyui" if "system" in info else "not-comfy"), info
    except HTTPError as err:
        return ("bad-token" if err.code == 401 else "not-comfy"), _json_or_empty(_read_quietly(err))
    except (URLError, OSError) as err:
        return _network_kind(err), {"error": str(err)}


def _read_quietly(err: HTTPError) -> bytes:
    try:
        return err.read()
    except OSError:
        return b""
    finally:
        err.close()


def _json_or_empty(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def _network_kind(err: BaseException) -> str:
    reason = getattr(err, "reason", err)
    if isinstance(reason, socket.gaierror):
        return "no-name"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, TimeoutError):
        return "asleep"
    code = getattr(reason, "errno", None)
    if code in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT, getattr(errno, "WSAETIMEDOUT", -1)):
        return "asleep"
    if code in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE) or isinstance(reason, ConnectionError):
        return "reset"
    return "error"


class RemoteLink:
    """The connection to the Remote GPU as the settings describe it (one per App)."""

    def __init__(
        self,
        settings: Any,
        log: Any = None,
        tunnel_factory: Callable[..., SshTunnel] = SshTunnel,
        records: Path | None = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self._tunnel_factory = tunnel_factory
        self.records = records  # where the ssh tunnels are noted (see reap_tunnels)
        if records is not None:
            try:
                ended = reap_tunnels(records)
            except OSError:
                ended = []
            if ended and log:
                log.info("remote GPU: ended ssh tunnels left by an OpenFill that is gone: %s", ended)
        self.tunnel: SshTunnel | None = None
        self._tunnel_key: tuple = ()
        self._failed_at = 0.0
        self._failure = ""
        self._lock = threading.Lock()
        self.kind = ""  # the last probe's answer (see probe)
        self.info: dict = {}
        atexit.register(self.close)

    # what the settings say ---------------------------------------------------------

    @property
    def via_ssh(self) -> bool:
        return bool((self.settings.remote_ssh or "").strip())

    def address(self) -> tuple[str, str]:
        """``(url as seen from the GPU computer or Tailscale, pairing code)``. A stored code is
        used only for the address it was paired with: after the address is edited it needs a new
        code, so the code is never sent to another computer."""
        url, token = parse_address(self.settings.remote_url)
        if not url and self.via_ssh:
            url = "http://127.0.0.1:8188"  # SSH to a plain ComfyUI on its default port
        if not token and same_address(getattr(self.settings, "remote_paired", ""), url):
            token = (self.settings.remote_token or "").strip()
        return url, token

    @property
    def paired_elsewhere(self) -> bool:
        """A code is stored, but for another address than the one set now."""
        try:
            _url, token = self.address()
        except RemoteSetupError:
            return False
        return not token and bool((self.settings.remote_token or "").strip())

    @property
    def host(self) -> str:
        if self.via_ssh:
            return parse_ssh_target(self.settings.remote_ssh)[0].rpartition("@")[2]
        try:
            return urlsplit(self.address()[0]).hostname or "the GPU computer"
        except RemoteSetupError:
            return "the GPU computer"

    def url(self) -> str:
        """The base URL OpenFill talks to: the GPU computer's address, or the tunnel's local end.
        A tunnel that is not open yet is opened here, but not on the main (UI) thread, where a
        computer that is asleep would freeze the window for the ssh timeout; :meth:`ensure`
        always opens it. An unreachable computer is retried after a pause."""
        try:
            remote, token = self.address()
        except RemoteSetupError:
            return ""
        if not remote:
            return ""
        if not self.via_ssh:
            set_token(remote, token)
            return remote
        tunnel = self._tunnel(remote)
        ui_thread = threading.current_thread() is threading.main_thread()
        if not tunnel.alive() and not ui_thread and time.monotonic() - self._failed_at > RETRY_AFTER_FAILURE:
            with self._lock:
                try:
                    tunnel.open()
                except BackendUnavailable as err:
                    self._failed_at, self._failure = time.monotonic(), str(err)
        if tunnel.url:
            set_token(tunnel.url, token)
        return tunnel.url or "http://127.0.0.1:9"  # nothing listens on the discard port: "not running"

    def _tunnel(self, remote: str) -> SshTunnel:
        parts = urlsplit(remote)
        key = (self.settings.remote_ssh.strip(), parts.hostname, parts.port)
        if self.tunnel is None or key != self._tunnel_key:
            if self.tunnel is not None:
                self.tunnel.close()
            dest = (key[0], parts.hostname or "127.0.0.1", parts.port or 8188, self.log)
            self.tunnel = (
                self._tunnel_factory(*dest, records=self.records) if self.records else self._tunnel_factory(*dest)
            )
            self._tunnel_key = key
        return self.tunnel

    # checks ------------------------------------------------------------------------

    def ensure(
        self,
        progress: Callable[[float, str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        wait_s: float = 240.0,
    ) -> str:
        """A base URL whose ComfyUI answers, or BackendUnavailable saying exactly what is wrong."""
        try:
            remote, token = self.address()
        except RemoteSetupError as err:
            raise BackendUnavailable(str(err)) from None
        if not remote:
            raise BackendUnavailable(f"Remote GPU is chosen, but no GPU computer is set. {SETUP_HINT}")
        if self.via_ssh:
            if progress:
                progress(0.0, f"Connecting to {self.host} over SSH...")
            tunnel = self._tunnel(remote)
            with self._lock:
                url = tunnel.open(cancel=cancel)
            self._failed_at = 0.0
        else:
            url = remote
        set_token(url, token)
        started = time.monotonic()
        while True:
            kind, info = probe(url)
            self.kind, self.info = kind, info
            if kind in ("gpu-server", "comfyui"):
                break
            if kind != "comfy-starting" or time.monotonic() - started > wait_s:
                raise BackendUnavailable(self.explain(kind, info))
            if cancel and cancel():
                raise GenerationCancelled("cancelled while the GPU computer was starting ComfyUI")
            if progress:
                progress(
                    min(0.95, (time.monotonic() - started) / wait_s),
                    f"The GPU computer is starting ComfyUI... {int(time.monotonic() - started)} s",
                )
            time.sleep(2.0)
        if kind == "comfyui" and not self.via_ssh and not is_loopback_host(urlsplit(url).hostname or ""):
            # A bare ComfyUI on the network: anyone there could use it. OpenFill does not take part.
            raise BackendUnavailable(
                f"{self.host} runs ComfyUI open to the network, without the OpenFill GPU server. OpenFill "
                "does not use an open ComfyUI over the network. Start the OpenFill GPU server on that computer "
                "and pair with it, or connect over SSH (see docs/remote-gpu.md)."
            )
        if kind == "gpu-server" and not token:
            if self.paired_elsewhere:
                raise BackendUnavailable(
                    f"The address of the GPU computer changed since you paired, so OpenFill does not send "
                    f"the old pairing code to {self.host}. {SETUP_HINT}"
                )
            raise BackendUnavailable(f"The GPU server on {self.host} needs its pairing code. {SETUP_HINT}")
        return url

    def explain(self, kind: str, info: dict | None = None) -> str:
        host = self.host
        detail = str((info or {}).get("error") or "")
        if kind in ("asleep", "reset", "error") and self.via_ssh and self.tunnel is not None:
            time.sleep(0.5)  # OpenSSH prints why the forwarded connection failed a moment after closing it
            stderr = self.tunnel.stderr_text().lower()
            # The SSH login worked (ssh still runs) and the far end closed the forwarded connection at
            # once: nothing listens there. (Windows' SSH server closes it without saying why.) A
            # dropped network makes the connection hang instead.
            if "connect failed" in stderr or "open failed" in stderr or (kind == "reset" and self.tunnel.alive()):
                kind = "nothing-behind-tunnel"
        if kind == "bad-token" and self.paired_elsewhere:
            kind = "paired-elsewhere"
        messages = {
            "paired-elsewhere": (
                f"The address of the GPU computer changed since you paired, so OpenFill does not send "
                f"the old pairing code to {host}. {SETUP_HINT}"
            ),
            "bad-token": (
                f"The GPU computer {host} did not accept the pairing code. On that computer run the OpenFill "
                "GPU server's pair command to get a new code, then paste it in Settings > Connection."
            ),
            "locked": (
                f"The GPU computer {host} refused several wrong pairing codes and waits a minute before "
                "it accepts one again. Check the code, then try again in a minute."
            ),
            "comfy-down": (
                f"The OpenFill GPU server on {host} runs, but its ComfyUI does not (it stopped or failed to "
                "start). Look at the GPU server's window or log on that computer, or restart the GPU server."
            ),
            "comfy-starting": f"The GPU computer {host} took too long to start ComfyUI. Look at the GPU server's log there.",
            "refused": (
                f"{host} answers, but the OpenFill GPU server is not running there (nothing listens on that "
                "port). Start it on the GPU computer, then try again."
            ),
            "nothing-behind-tunnel": (
                f"The SSH connection to {host} works, but nothing answers behind it on port "
                f"{self.address()[0].rpartition(':')[2]}. Start ComfyUI or the OpenFill GPU server on {host}."
            ),
            "asleep": asleep_message(host),
            "reset": (
                f"The connection to {host} broke off. If it keeps happening, check the network (Tailscale) "
                "on both computers."
            ),
            "no-name": f"The computer name '{host}' was not found. Check the address in Settings > Connection.",
            "not-comfy": f"What answers at {self.address()[0]} is not ComfyUI or the OpenFill GPU server.",
        }
        return messages.get(kind) or f"The GPU computer {host} could not be used: {detail or kind}."

    def close(self) -> None:
        if self.tunnel is not None:
            self.tunnel.close()

    # model and node checks -----------------------------------------------------------


def catalog_folders(entries: Iterable[cat.ModelEntry]) -> set[str]:
    """The ComfyUI model folders the catalog's files go to."""
    return {f.dest.replace("\\", "/").split("/", 1)[0] for e in entries for f in e.files}


def fetch_tree(client: ComfyClient, folders: Iterable[str], models_root: Path, nodes_root: Path) -> cat.RemoteTree:
    """What the remote ComfyUI has, from its own lists (``/models/<folder>``, ``/object_info``)
    and, behind the OpenFill GPU server, its node pack folders (``/openfill/inventory``)."""
    keys = set(client._json("GET", "/models") or [])
    files: dict[str, set[str]] = {}
    lists: dict[str, set[str]] = {}
    for folder in sorted(set(folders)):
        names: set[str] = set()
        for key in FOLDER_KEYS.get(folder, (folder,)):
            if key not in keys:
                continue
            if key not in lists:
                raw = client._json("GET", f"/models/{key}")
                lists[key] = {str(n).replace("\\", "/") for n in raw} if isinstance(raw, list) else set()
            names |= lists[key]
        files[folder] = names
    loaded = {folder: bool(client.object_info(cls)) for folder, cls in NODE_MARKERS.items()}
    nodes: dict[str, str | None] = {}
    try:
        inventory = client._json("GET", "/openfill/inventory")
    except ComfyApiError as err:
        if err.status not in (404, 405):
            raise
        inventory = None
    if isinstance(inventory, dict) and isinstance(inventory.get("nodes"), dict):
        for folder, version in inventory["nodes"].items():
            if loaded.get(folder, True):  # a folder whose nodes did not load does not count
                nodes[str(folder)] = str(version or "")
    else:
        nodes = {folder: None for folder, ok in loaded.items() if ok}
    return cat.RemoteTree(models_root, nodes_root, files, nodes)


def install_nodes(client: ComfyClient, model_id: str) -> dict:
    """Ask the GPU server to install the node packs ``model_id`` needs there."""
    try:
        return client._json("POST", "/openfill/install-nodes", {"model_id": model_id}, timeout=30)
    except ComfyApiError as err:
        if err.status in (404, 405):
            raise BackendUnavailable(
                "This GPU computer runs ComfyUI without the OpenFill GPU server, so OpenFill cannot install "
                "node packs there. Install them on that computer, or start the OpenFill GPU server there."
            ) from None
        message = (err.payload or {}).get("error") or str(err)
        raise BackendUnavailable(f"The GPU computer did not install the node packs: {message}") from None


def task_status(client: ComfyClient) -> dict:
    """The GPU server's task status, in the shape of the local task worker's (current, history)."""
    try:
        data = client._json("GET", "/openfill/status", timeout=STATUS_TIMEOUT)
    except ComfyApiError:
        return {}
    return data if isinstance(data, dict) else {}
