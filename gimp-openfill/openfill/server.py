# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""ServerManager: find a healthy backend or start one, hidden.

The plug-in process is short-lived, so the long-running work is delegated to
``supervisor.py`` (a detached process). This module only knows how to read
the supervisor's state file, health-check the URL and launch the supervisor.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .backend import BackendUnavailable, GenerationCancelled
from .comfy_api import ComfyApiError, ComfyClient
from .config import Paths, Settings, comfy_layout, comfy_problem, in_app_bundle, windows_path_problem
from .supervisor import WATCHERS_DIRNAME, pid_alive, pid_is_python

ProgressCallback = Callable[[float, str], None]
IS_WINDOWS = sys.platform == "win32"
SUPERVISOR_STDERR_FILENAME = "supervisor-stderr.log"
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    """``text`` without ANSI escape sequences (colors, cursor moves) as ComfyUI writes them to its log."""
    return _ANSI_RE.sub("", text)


def port_in_use_message(host: str, port: int) -> str:
    """Plain-words error for a port that another program already holds."""
    free = 8190 if port != 8190 else 8191
    return (
        f"Port {port} is used by another program (it is not ComfyUI). Close that program, or set a free "
        f"port in the ComfyUI URL in Filters > OpenFill > Generative Fill Settings (for example http://{host}:{free})."
    )


def port_answers(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts a TCP connection on ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_bindable(host: str, port: int) -> bool:
    """True when a server could listen on ``host:port`` now (nothing holds it)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def free_port(
    host: str,
    after: int,
    avoid: tuple[int, ...] = (),
    tries: int = 20,
    answers: Callable[..., bool] = port_answers,
    bindable: Callable[[str, int], bool] = port_bindable,
) -> int | None:
    """The first port after ``after`` (not in ``avoid``) that nothing answers on and that can
    be listened on, for OpenFill's own ComfyUI when its usual port is taken; None when the
    next ``tries`` ports are all in use."""
    for port in range(after + 1, min(after + 1 + tries, 65536)):
        if port in avoid:
            continue
        if not answers(host, port, timeout=0.3) and bindable(host, port):
            return port
    return None


def with_port(url: str, port: int) -> str:
    """``url`` with its port replaced (scheme, host and path kept)."""
    parts = urlparse(url)
    host = parts.hostname or "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return parts._replace(netloc=f"{host}:{port}").geturl()


def comfy_argv(stats: dict[str, Any]) -> list[str] | None:
    """The command line ComfyUI was started with (``system.argv`` of /system_stats), or None
    when this ComfyUI does not say."""
    argv = (stats.get("system") or {}).get("argv") if isinstance(stats, dict) else None
    if not isinstance(argv, list) or not argv:
        return None
    return [str(a) for a in argv]


def _base_name(path: str) -> str:
    return re.split(r"[\\/]", path)[-1].lower()


def same_comfy_command(argv: list[str], command: list[str]) -> bool:
    """True when ``argv`` (ComfyUI's ``sys.argv``: main.py and its options) is the tail of
    ``command`` (the supervisor's ``[python, -s, main.py, options...]``)."""
    if not argv or len(command) <= len(argv):
        return False
    tail = [str(a) for a in command[len(command) - len(argv) :]]
    return _base_name(tail[0]) == _base_name(argv[0]) and tail[1:] == argv[1:]


# A first start of ComfyUI takes 20 s to about 2 minutes; past this the progress text and
# the companion's status line add a hint.
SLOW_START_S = 90


def unresponsive_folders(folders: list[str], timeout: float = 2.0) -> list[str]:
    """The folders of ``folders`` that do not answer within ``timeout`` seconds (a sleeping,
    unplugged or network drive behind an automount). Each is looked at in its own daemon
    thread, so a folder that hangs never holds the caller up."""
    done: dict[str, bool] = {}

    def look(folder: str) -> None:
        try:
            os.stat(folder)
        except OSError:
            pass
        done[folder] = True

    threads = []
    for folder in folders:
        t = threading.Thread(target=look, args=(folder,), name="openfill-folder-check", daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [f for f in folders if not done.get(f)]


def slow_start_hint(slow_folders: list[str] | None = None) -> str:
    """Plain words for a ComfyUI that takes longer to start than usual (see :data:`SLOW_START_S`)."""
    if slow_folders:
        return (
            f"Slower than usual: the model folder {slow_folders[0]} does not answer. A sleeping, unplugged "
            "or network drive holds ComfyUI up. Plug it in, or take the folder out of Models from another ComfyUI."
        )
    return (
        "Slower than usual. A model folder on a slow, sleeping or unplugged drive can hold ComfyUI up; "
        "the ComfyUI log shows where it is."
    )


class PortTaken(BackendUnavailable):
    """The port OpenFill started its own ComfyUI on is held by another program
    (``comfy``: by another ComfyUI). ServerManager then moves to a free port."""

    def __init__(self, host: str, port: int, comfy: bool = False) -> None:
        self.host, self.port, self.comfy = host, port, comfy
        if comfy:
            text = (
                f"Port {port} is used by another ComfyUI (not the one OpenFill started), and no free port "
                f"was found next to it. Close that ComfyUI, or set a free port in the ComfyUI URL in "
                "Filters > OpenFill > Generative Fill Settings."
            )
        else:
            text = port_in_use_message(host, port)
        super().__init__(text)


def moved_port_note(old: int, new: int) -> str:
    """The status line for OpenFill's ComfyUI on another port than the one in the settings."""
    return (
        f"Port {old} is used by another program, so OpenFill started its own ComfyUI on port {new}. "
        "Your settings are unchanged."
    )


def _hidden_python() -> str:
    """Interpreter for the supervisor: the console-less twin of ours if any."""
    exe = Path(sys.executable)
    if IS_WINDOWS:
        for name in ("pythonw.exe", "pythonw3.exe"):
            candidate = exe.with_name(name)
            if candidate.exists():
                return str(candidate)
    return str(exe)


def helper_python(
    executable: str,
    platform: str,
    comfy_python: str | Path | None,
    which: Callable[..., str | None] = shutil.which,
    path_env: str | None = None,
) -> str:
    """Interpreter for the supervisor and task worker.

    On macOS, GIMP signs the Python in GIMP.app with a launch constraint: only
    GIMP may start it, and the kernel kills it when a plug-in does
    ("Launch Constraint Violation"). There the helpers run on ComfyUI's own
    Python (the supervisor needs only the standard library), else on the first
    python3 on PATH outside an app bundle. Everywhere else: ``executable``.
    """
    if platform != "darwin" or not in_app_bundle(executable):
        return executable
    if comfy_python and Path(comfy_python).exists() and not in_app_bundle(comfy_python):
        return str(comfy_python)
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    outside = os.pathsep.join(d for d in path_env.split(os.pathsep) if d and not in_app_bundle(d))
    return which("python3", path=outside) or executable


class ServerManager:
    """Reads the supervisor's state, health-checks the backend and starts it hidden when needed."""

    def __init__(self, settings: Settings, paths: Paths, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.paths = paths
        self.log = logger
        # The process the backend serves; it stops when this one ends. GIMP plug-ins are
        # children of GIMP (the default, the parent pid); the PhotoCraft companion sets its own.
        self.watch_pid: int | None = None
        self.watch_name = ""  # the watched process's executable name, set with watch_pid
        self._remote: Any = None  # remote.RemoteLink, made when the Remote GPU is the backend
        self._remote_status: tuple[float, dict[str, Any]] = (0.0, {})
        self._foreign = ""  # the URL of a ComfyUI in use that OpenFill did not start
        self._told: set[str] = set()  # notes already written to the log

    # Remote GPU ------------------------------------------------------------

    @property
    def remote(self) -> Any:
        """The Remote GPU link (``remote.RemoteLink``), or None when ComfyUI runs on this computer."""
        if not self.settings.uses_remote:
            return None
        if self._remote is None:
            from .remote import RemoteLink

            self._remote = RemoteLink(self.settings, self.log, records=self.paths.base / "ssh-tunnels")
        return self._remote

    # state ---------------------------------------------------------------

    def read_state(self) -> dict[str, Any]:
        """``server-state.json`` as written by the supervisor; ``{}`` when absent or broken."""
        try:
            return json.loads(self.paths.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.paths.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.paths.state)

    def touch(self) -> None:
        """Record activity so the idle timeout restarts."""
        if self.settings.uses_remote:
            return  # the GPU computer's ComfyUI is not ours to stop
        state = self.read_state()
        if state:
            self.register_watcher()
            state["last_used"] = time.time()
            try:
                self._write_state(state)
            except OSError:
                pass

    def register_watcher(self, gimp_pid: int | None = None) -> None:
        """Tell the supervisor that this GIMP uses the managed backend too, so it is not shut down
        when the GIMP that started it exits while this one still needs it."""
        try:
            folder = self.paths.base / WATCHERS_DIRNAME
            folder.mkdir(parents=True, exist_ok=True)
            pid = gimp_pid or self.watch_pid or os.getppid()
            # A companion (not a GIMP) records its own process name, so the supervisor can tell
            # it apart from an unrelated program that later gets the same pid.
            (folder / str(pid)).write_text(self.watch_name if self.watch_pid else "", encoding="utf-8")
        except OSError:
            pass

    def request_stop(self) -> bool:
        """Ask the supervisor to shut the backend down. Returns True if asked."""
        if self.settings.uses_remote:
            return False
        state = self.read_state()
        if not state:
            return False
        state["stop"] = True
        try:
            self._write_state(state)
        except OSError:
            return False
        if self.log:
            self.log.info("stop requested for backend pid %s", state.get("server_pid"))
        return True

    # health --------------------------------------------------------------

    def url(self) -> str:
        """URL of the running managed backend, else the configured one (Remote GPU: the GPU
        computer's address, or the local end of the SSH tunnel to it)."""
        if self.remote is not None:
            return self.remote.url()
        state = self.read_state()
        return state.get("url") or self.settings.active_url

    def death_watch(self) -> Callable[[], bool] | None:
        """For the ComfyUI the supervisor runs: a check that turns True once that ComfyUI is
        gone, so a job waiting on it can stop at once. None when no live managed ComfyUI is
        recorded: a ComfyUI the user started (even behind a state file a killed supervisor
        left) keeps the usual grace period.

        Gone means: the state file was removed (the supervisor saw ComfyUI exit), the
        supervisor died, it now runs another ComfyUI (a restart), or the recorded ComfyUI
        process exited. The state is read again on every call, so a pid reused by another
        program after the supervisor cleaned up still counts as gone."""
        state = self.read_state()
        pid, supervisor = state.get("server_pid"), state.get("supervisor_pid")
        if not (_valid_pid(pid) and _valid_pid(supervisor)):
            return None
        if not pid_alive(pid) or not pid_is_python(supervisor):
            return None  # a stale state: the ComfyUI that answered is not this one

        def gone() -> bool:
            if not self.paths.state.exists():
                return True
            now = self.read_state()
            if not now:
                return False  # being rewritten: ask again on the next poll
            if now.get("supervisor_pid") != supervisor or now.get("server_pid") != pid:
                return True
            return not pid_is_python(supervisor) or not pid_alive(pid)

        return gone

    def live_state(self) -> dict[str, Any]:
        """The state file while the supervisor that wrote it runs; ``{}`` otherwise."""
        state = self.read_state()
        sup = state.get("supervisor_pid")
        return state if _valid_pid(sup) and pid_is_python(sup) else {}

    def answering_is_ours(self, url: str, state: dict[str, Any] | None = None) -> bool | None:
        """Is the ComfyUI that answers at ``url`` the one OpenFill's supervisor started? Its
        command line (``/system_stats``) is compared with the one the supervisor recorded.
        True or False; None when that cannot be told (no running supervisor, a state from
        another address, a ComfyUI that does not show its command line, no answer)."""
        state = self.live_state() if state is None else state
        command = state.get("command")
        if not isinstance(command, list) or not command:
            return None
        if state.get("url") and _split_host_port(str(state["url"]))[1] != _split_host_port(url)[1]:
            return None
        try:
            argv = comfy_argv(ComfyClient(url, timeout=3).system_stats())
        except (ComfyApiError, OSError, ValueError):
            return None
        if argv is None:
            return None
        return same_comfy_command(argv, [str(c) for c in command])

    def note(self) -> str:
        """A plain line for the status and Check backend: OpenFill's ComfyUI runs on another
        port than the settings say (the usual one was taken), or the ComfyUI in use is one
        OpenFill did not start. "" when neither."""
        if self.settings.uses_remote or self.settings.backend != "comfyui":
            return ""
        state = self.live_state()
        configured = _split_host_port(self.settings.url)[1]
        port = state.get("port")
        if isinstance(port, int) and port != configured:
            return moved_port_note(configured, port)
        if self._foreign and not state:
            text = f"Using the ComfyUI that was already running at {self._foreign} (OpenFill did not start it)."
            if self.settings.shared_models.strip():
                text += " It does not read your folders in Models from another ComfyUI unless it was set up with them."
            return text
        return ""

    def short_note(self) -> str:
        """:meth:`note` in a few words for the companion's status line ("" when none)."""
        if self.settings.uses_remote or self.settings.backend != "comfyui":
            return ""
        state = self.live_state()
        configured = _split_host_port(self.settings.url)[1]
        port = state.get("port")
        if isinstance(port, int) and port != configured:
            return f"Ready on port {port} ({configured} is taken)"
        if self._foreign and not state and self.settings.shared_models.strip():
            return "Ready (a ComfyUI OpenFill did not start)"
        return ""

    def _tell(self, text: str) -> None:
        """Log ``text`` once per manager."""
        if text and text not in self._told and self.log:
            self._told.add(text)
            self.log.info("%s", text)

    def is_healthy(self, url: str | None = None) -> bool:
        """True when the selected backend answers at ``url``."""
        if self.settings.backend == "a1111":
            from .backends.a1111 import A1111Backend

            return A1111Backend(url or self.settings.a1111_url).health().ok
        return ComfyClient(url or self.url()).is_alive()

    # start ---------------------------------------------------------------

    def ensure_running(self, progress: ProgressCallback | None = None, cancel: Callable[[], bool] | None = None) -> str:
        """Return a healthy base URL, starting the backend if allowed. A Remote GPU is only
        checked (never started here, never replaced by this computer's ComfyUI)."""
        if self.remote is not None:
            url = self.remote.ensure(progress, cancel, wait_s=max(30, int(self.settings.startup_timeout)))
            if self.log:
                self.log.info("remote GPU ready at %s (%s)", url, self.remote.kind)
            return url
        url = self.url()
        if self.is_healthy(url):
            if self.settings.backend == "comfyui":
                state = self.live_state()
                if state and self.settings.autostart and self.answering_is_ours(url, state) is False:
                    # OpenFill's own ComfyUI is still loading and another one took its port: it
                    # would fail to listen there. Wait for it, which moves it to a free port.
                    if self.log:
                        self.log.warning("another ComfyUI answers at %s, not the one OpenFill is starting", url)
                    return self._await_own(url, progress, cancel)
                self._foreign = "" if state else url
                self._tell(self.note())
            self.touch()
            return url
        if self.settings.backend == "a1111":
            raise BackendUnavailable(
                f"AUTOMATIC1111 WebUI is not answering at {self.settings.a1111_url}. "
                "Start it with --api, or switch the backend to ComfyUI in the settings."
            )
        if not self.settings.autostart:
            raise BackendUnavailable(
                f"ComfyUI is not running at {url}. Start ComfyUI yourself, or turn on 'Start backend "
                "automatically' in Filters > OpenFill > Generative Fill Settings, then press Check backend."
            )
        return self.start(progress, cancel)

    def start(self, progress: ProgressCallback | None = None, cancel: Callable[[], bool] | None = None) -> str:
        """Launch the backend supervisor detached and wait until ComfyUI answers."""
        if self.settings.uses_remote:
            raise BackendUnavailable("Remote GPU is chosen: ComfyUI runs on the GPU computer, not here.")
        layout = comfy_layout(self.settings.comfy_dir)
        if layout is None:
            raise BackendUnavailable(
                f"{comfy_problem(self.settings.comfy_dir)} Install ComfyUI there or set the ComfyUI folder in "
                "Filters > OpenFill > Generative Fill Settings."
            )
        problem = windows_path_problem(layout["python"])
        if problem:
            raise BackendUnavailable(problem)
        self.paths.ensure()
        self._drop_stale_cpu_flag()
        url = self.settings.url
        host, port = _split_host_port(url)
        self._foreign = ""
        if port_answers(host, port, timeout=1.0):
            # Not a healthy ComfyUI (ensure_running checked): maybe a busy one that was slow to answer.
            if ComfyClient(url).is_alive(timeout=10.0):
                self._foreign = "" if self.live_state() else url
                self._tell(self.note())
                self.touch()
                return url
            if self.log:
                self.log.warning("port %s on %s is taken by a program that is not ComfyUI", port, host)
            new = free_port(host, port)
            if new is None:
                raise BackendUnavailable(port_in_use_message(host, port))
            self._tell(moved_port_note(port, new))
            if progress:
                progress(
                    0.0, f"Port {port} is used by another program. Starting OpenFill's own ComfyUI on port {new}..."
                )
            url, port = with_port(url, new), new
        self._spawn_supervisor(layout, host, port)
        return self._await_own(url, progress, cancel)

    def _await_own(self, url: str, progress: ProgressCallback | None, cancel: Callable[[], bool] | None) -> str:
        """Wait for OpenFill's ComfyUI at ``url``. When another program takes its port while
        it loads (it would then fail to listen), start it again once on a free port."""
        try:
            return self._wait_ready(url, progress, cancel)
        except PortTaken as err:
            configured = _split_host_port(self.settings.url)[1]
            new = free_port(err.host, err.port, avoid=(configured,))
            layout = comfy_layout(self.settings.comfy_dir)
            if new is None or layout is None:
                raise
            what = "another ComfyUI" if err.comfy else "another program"
            if self.log:
                self.log.warning("port %s is used by %s; starting OpenFill's ComfyUI on port %s", err.port, what, new)
            self._tell(moved_port_note(err.port, new))
            if progress:
                progress(0.0, f"Port {err.port} is used by {what}. Starting OpenFill's own ComfyUI on port {new}...")
            self._spawn_supervisor(layout, err.host, new)
            return self._wait_ready(with_port(url, new), progress, cancel)

    def _spawn_supervisor(self, layout: dict[str, Path], host: str, port: int) -> None:
        """Launch the backend supervisor detached, for ComfyUI on ``host:port``."""
        gimp_pid = self.watch_pid or os.getppid()
        cmd = [
            self._helper_python(),
            str(Path(__file__).with_name("supervisor.py")),
            "--mode",
            "backend",
            "--python",
            str(layout["python"]),
            "--main",
            str(layout["main"]),
            "--cwd",
            str(layout["cwd"]),
            "--host",
            host,
            "--port",
            str(port),
            "--base",
            str(self.paths.base),
            "--config",
            str(self.paths.config),
            "--watch-pid",
            str(gimp_pid),
            "--idle-minutes",
            str(self.settings.idle_timeout_minutes),
        ]
        if self.settings.extra_server_args.strip():
            # one argv item: a separate value starting with "-" (--cpu) would be read as an option
            cmd.append(f"--extra-args={self.settings.extra_server_args.strip()}")
        if self.log:
            self.log.info("launching supervisor (gimp pid %s): %s", gimp_pid, " ".join(cmd))
        self._spawn_detached(cmd)

    # tasks (downloads, node installs) -------------------------------------

    @property
    def tasks_dir(self) -> Path:
        """Queue folder the supervisor's task worker polls."""
        return self.paths.base / "tasks"

    def task_worker_alive(self) -> bool:
        """A worker is a tasks-mode supervisor, or a backend supervisor that
        announced itself in tasks-status.json (older supervisors did not).
        Pids are checked against the process name, so a reused pid does not count."""
        for name, key in (("tasks-worker.json", "pid"), ("tasks-status.json", "worker_pid")):
            try:
                info = json.loads((self.paths.base / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if pid_is_python(int(info.get(key) or 0)):
                return True
        return False

    def ensure_task_worker(self) -> None:
        """Make sure something will process queued tasks."""
        if self.settings.uses_remote or self.task_worker_alive():
            return
        cmd = [
            self._helper_python(),
            str(Path(__file__).with_name("supervisor.py")),
            "--mode",
            "tasks",
            "--base",
            str(self.paths.base),
            "--config",
            str(self.paths.config),
        ]
        if self.log:
            self.log.info("launching task worker: %s", " ".join(cmd))
        self._spawn_detached(cmd)

    def queue_task(self, task: dict[str, Any]) -> str:
        """Write ``task`` to the queue (atomically) and make sure a worker runs; returns its id."""
        if self.settings.uses_remote:
            raise BackendUnavailable(REMOTE_NO_LOCAL_TASKS)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        task_id = task.get("id") or f"{int(time.time() * 1000)}-{task.get('type', 'task')}"
        task["id"] = task_id
        task.setdefault("queued", time.time())
        tmp = self.tasks_dir / f"{task_id}.json.tmp"
        tmp.write_text(json.dumps(task, indent=2), encoding="utf-8")
        os.replace(tmp, self.tasks_dir / f"{task_id}.json")
        self.ensure_task_worker()
        if self.log:
            self.log.info("queued task %s (%s)", task_id, task.get("type"))
        return task_id

    def task_status(self) -> dict[str, Any]:
        """The worker's ``tasks-status.json`` (current task, queue, history).

        A "current" task whose worker is gone (killed, or shut down with GIMP mid-download)
        is dropped, so the Models tab does not show a download that nobody is doing.
        Remote GPU: the GPU server's status (node installs there), asked at most every 2 s."""
        if self.remote is not None:
            return self._remote_task_status()
        try:
            status = json.loads((self.paths.base / "tasks-status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if status.get("current") and not self.task_worker_alive():
            status["current"] = None
        return status

    def request_cancel(self) -> None:
        """Cancel the running task and drop everything still queued."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        (self.tasks_dir / "cancel").write_text(str(time.time()), encoding="utf-8")
        # also drop anything still waiting in the queue
        for p in self.tasks_dir.glob("*.json"):
            if not p.name.endswith(".running.json"):
                try:
                    p.unlink()
                except OSError:
                    pass

    def pending_tasks(self) -> list[dict[str, Any]]:
        """Decoded queued tasks, oldest first."""
        if self.settings.uses_remote:
            return []
        out = []
        worker = None
        for p in sorted(self.tasks_dir.glob("*.json")) if self.tasks_dir.is_dir() else []:
            if p.name.endswith(".running.json"):
                worker = self.task_worker_alive() if worker is None else worker
                if not worker:
                    continue  # left by a worker that is gone; the next worker re-queues it
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return out

    @property
    def supervisor_stderr_log(self) -> Path:
        """Where the supervisor's stderr goes (argument errors, crashes before its own log is open)."""
        return self.paths.base / SUPERVISOR_STDERR_FILENAME

    def _remote_task_status(self) -> dict[str, Any]:
        from .remote import RETRY_AFTER_FAILURE, task_status

        checked, status = self._remote_status
        now = time.monotonic()
        if now - checked < (2.0 if status.get("ok", True) else RETRY_AFTER_FAILURE):
            return status
        url = self.remote.url()
        status = task_status(ComfyClient(url, timeout=2)) if url else {}
        self._remote_status = (now, status or {"ok": False})
        return status

    def _drop_stale_cpu_flag(self) -> None:
        """Download ComfyUI adds --cpu for its own folder on a PC without an NVIDIA card; once the
        user points OpenFill at another ComfyUI, that flag would keep it on the processor."""
        from .comfy_setup import default_install_dir  # imported here: comfy_setup is not needed otherwise

        if not self.settings.drop_stale_cpu_flag(default_install_dir() / "ComfyUI_windows_portable"):
            return
        try:
            self.settings.save(self.paths.config)
        except OSError as err:
            if self.log:
                self.log.warning("could not save the settings without --cpu: %s", err)
        if self.log:
            self.log.info("removed --cpu: it was added for another ComfyUI folder than %s", self.settings.comfy_dir)

    def _helper_python(self) -> str:
        """See :func:`helper_python`; ComfyUI's interpreter comes from the configured folder."""
        layout = comfy_layout(self.settings.comfy_dir) if self.settings.comfy_dir else None
        return helper_python(_hidden_python(), sys.platform, layout["python"] if layout else None)

    def _spawn_detached(self, cmd: list[str]) -> None:
        """Start ``cmd`` detached and hidden; its stderr is appended to :attr:`supervisor_stderr_log`."""
        try:
            self.paths.base.mkdir(parents=True, exist_ok=True)
            stderr: Any = open(self.supervisor_stderr_log, "ab")  # noqa: SIM115 - the child gets its own handle
        except OSError:
            stderr = subprocess.DEVNULL
        try:
            self._popen_detached(cmd, stderr)
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()

    def _popen_detached(self, cmd: list[str], stderr: Any) -> None:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": stderr,
            "close_fds": True,
        }
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        if cmd and cmd[0] != _hidden_python():
            env.pop("PYTHONHOME", None)  # GIMP's Python home breaks any other interpreter
        kwargs["env"] = env
        if IS_WINDOWS:
            # No DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW next to it, and a
            # child without a (hidden) console opens a window for every console program it starts.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
                return
            except OSError as err:
                if self.log:
                    self.log.info("breakaway spawn refused (%s); retrying without it", err)
                subprocess.Popen(cmd, creationflags=flags, **kwargs)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kwargs)

    def _wait_ready(self, url: str, progress: ProgressCallback | None, cancel: Callable[[], bool] | None = None) -> str:
        """Wait until OpenFill's ComfyUI answers at ``url``. A ComfyUI that answers there but is
        not the one the supervisor started (the user's own, started while ours loaded) does not
        count: ours is stopped and :class:`PortTaken` raised, so the caller moves to a free port."""
        client = ComfyClient(url)
        host, port = _split_host_port(url)
        timeout = max(30, int(self.settings.startup_timeout))
        started = time.monotonic()
        hint = ""
        while time.monotonic() - started < timeout:
            elapsed = time.monotonic() - started
            if cancel and cancel():
                # stop waiting at once; the backend keeps starting and serves the next run
                if self.log:
                    self.log.info("cancelled while the backend was starting (%.0f s)", elapsed)
                raise GenerationCancelled("cancelled while the AI backend was starting")
            if elapsed >= SLOW_START_S and not hint:
                hint = slow_start_hint(unresponsive_folders(self._model_folders()))
                if self.log:
                    self.log.warning("ComfyUI is still starting after %.0f s. %s", elapsed, hint)
            if progress:
                text = f"Starting the AI backend... {int(elapsed)} s"
                progress(min(0.95, elapsed / timeout), f"{text}. {hint}" if hint else text)
            state = self.read_state()
            sup = state.get("supervisor_pid")
            live = state if _valid_pid(sup) and pid_is_python(sup) else {}
            if client.is_alive(timeout=2.0):
                mine = self.answering_is_ours(url, live) if live else None
                if mine is False:
                    if self.log:
                        self.log.warning(
                            "another ComfyUI answers at %s, not the one OpenFill started (pid %s)",
                            url,
                            state.get("server_pid"),
                        )
                    self._stop_own(live, cancel)
                    raise PortTaken(host, port, comfy=True)
                if live:  # ours, or one that does not show its command line (as before)
                    self.touch()
                    if self.log:
                        self.log.info("backend ready at %s after %.0f s", url, elapsed)
                    return url
                # no running supervisor has written its state yet: who answers is not known yet
            if state.get("status") == "failed" or (elapsed > 15 and not state):
                break
            time.sleep(1.5)
        elapsed = time.monotonic() - started
        tail = self.server_log_tail()
        if "already in use" in tail.lower():
            raise PortTaken(host, port, comfy=ComfyClient(url).is_alive(timeout=2.0))
        if self.log:
            self.log.warning("backend did not come up at %s after %.0f s; log tail:\n%s", url, elapsed, tail)
        raise BackendUnavailable(
            f"ComfyUI did not start at {url} (gave up after {elapsed:.0f} s). Check the ComfyUI folder and "
            "the extra server arguments in Filters > OpenFill > Generative Fill Settings, then try again. "
            f"Last lines of the ComfyUI log ({self.paths.server_log}):\n{tail}"
        )

    def _stop_own(self, state: dict[str, Any], cancel: Callable[[], bool] | None = None) -> None:
        """Ask the supervisor of ``state`` to stop its ComfyUI and wait (up to 40 s) until it is
        gone, so a new one can start on another port."""
        supervisor = state.get("supervisor_pid")
        if not self.request_stop():
            return
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not (cancel and cancel()):
            now = self.read_state()
            if not now or now.get("supervisor_pid") != supervisor or not pid_is_python(supervisor):
                return
            time.sleep(0.5)

    def _model_folders(self) -> list[str]:
        """The folders the starting ComfyUI reads models from besides its own: the shared
        folders it was started with, else the ones in the settings."""
        roots = self.read_state().get("shared_roots")
        if isinstance(roots, list) and roots:
            return [str(r) for r in roots]
        # as stored (one per line), without looking at them: that could hang as well
        return [line.strip() for line in self.settings.shared_models.splitlines() if line.strip()]

    def server_log_tail(self, lines: int = 8) -> str:
        """Last ``lines`` lines of the ComfyUI log, for error messages."""
        if self.settings.uses_remote:
            return "(ComfyUI's log is on the GPU computer, next to the OpenFill GPU server)"
        try:
            text = self.paths.server_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no backend log yet)"
        return "\n".join(strip_ansi(text).strip().splitlines()[-lines:])


def _valid_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


REMOTE_NO_LOCAL_TASKS = (
    "Remote GPU is chosen: models are downloaded on the GPU computer. Run the OpenFill GPU server's "
    "download command there (see docs/remote-gpu.md), then press Check backend."
)


def _split_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 8188
