# OpenFill - generative fill for GIMP 3
# Copyright (C) 2022 Torben Giesselmann, (C) 2026 Beckett Harriman
# SPDX-License-Identifier: GPL-3.0-or-later
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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .backend import BackendUnavailable, GenerationCancelled
from .comfy_api import ComfyClient
from .config import Paths, Settings, comfy_layout, comfy_problem, in_app_bundle
from .supervisor import WATCHERS_DIRNAME, pid_is_python

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
        """URL of the running managed backend, else the configured one."""
        state = self.read_state()
        return state.get("url") or self.settings.active_url

    def is_healthy(self, url: str | None = None) -> bool:
        """True when the selected backend answers at ``url``."""
        if self.settings.backend == "a1111":
            from .backends.a1111 import A1111Backend

            return A1111Backend(url or self.settings.a1111_url).health().ok
        return ComfyClient(url or self.url()).is_alive()

    # start ---------------------------------------------------------------

    def ensure_running(self, progress: ProgressCallback | None = None, cancel: Callable[[], bool] | None = None) -> str:
        """Return a healthy base URL, starting the backend if allowed."""
        url = self.url()
        if self.is_healthy(url):
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
        layout = comfy_layout(self.settings.comfy_dir)
        if layout is None:
            raise BackendUnavailable(
                f"{comfy_problem(self.settings.comfy_dir)} Install ComfyUI there or set the ComfyUI folder in "
                "Filters > OpenFill > Generative Fill Settings."
            )
        self.paths.ensure()
        self._drop_stale_cpu_flag()
        url = self.settings.url
        host, port = _split_host_port(url)
        # Something already listening here is not a healthy ComfyUI (ensure_running checked), so launching
        # would only make ComfyUI load for half a minute and then fail to bind the port.
        if port_answers(host, port, timeout=1.0):
            if ComfyClient(url).is_alive(timeout=10.0):  # a busy ComfyUI that was slow to answer the first check
                self.touch()
                return url
            if self.log:
                self.log.warning("port %s on %s is taken by a program that is not ComfyUI", port, host)
            raise BackendUnavailable(port_in_use_message(host, port))
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
        return self._wait_ready(url, progress, cancel)

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
        if self.task_worker_alive():
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
        is dropped, so the Models tab does not show a download that nobody is doing."""
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
        client = ComfyClient(url)
        timeout = max(30, int(self.settings.startup_timeout))
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            elapsed = time.monotonic() - started
            if cancel and cancel():
                # stop waiting at once; the backend keeps starting and serves the next run
                if self.log:
                    self.log.info("cancelled while the backend was starting (%.0f s)", elapsed)
                raise GenerationCancelled("cancelled while the AI backend was starting")
            if progress:
                progress(min(0.95, elapsed / timeout), f"Starting the AI backend... {int(elapsed)} s")
            if client.is_alive(timeout=2.0):
                self.touch()
                if self.log:
                    self.log.info("backend ready at %s after %.0f s", url, elapsed)
                return url
            state = self.read_state()
            if state.get("status") == "failed" or (elapsed > 15 and not state):
                break
            time.sleep(1.5)
        elapsed = time.monotonic() - started
        tail = self.server_log_tail()
        if "already in use" in tail.lower():
            host, port = _split_host_port(url)
            raise BackendUnavailable(port_in_use_message(host, port))
        if self.log:
            self.log.warning("backend did not come up at %s after %.0f s; log tail:\n%s", url, elapsed, tail)
        raise BackendUnavailable(
            f"ComfyUI did not start at {url} (gave up after {elapsed:.0f} s). Check the ComfyUI folder and "
            "the extra server arguments in Filters > OpenFill > Generative Fill Settings, then try again. "
            f"Last lines of the ComfyUI log ({self.paths.server_log}):\n{tail}"
        )

    def server_log_tail(self, lines: int = 8) -> str:
        """Last ``lines`` lines of the ComfyUI log, for error messages."""
        try:
            text = self.paths.server_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no backend log yet)"
        return "\n".join(strip_ansi(text).strip().splitlines()[-lines:])


def _split_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 8188
