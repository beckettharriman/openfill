#!/usr/bin/env python3
# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Backend supervisor: a tiny detached process that owns the ComfyUI server
and runs long tasks (model downloads, custom node installs) for the plug-in.

GIMP plug-in processes are short-lived, so something else has to keep the
server warm between calls, shut it down when GIMP exits, enforce the idle
timeout and carry on downloading after the dialog closed. This script is that
something. It is started by ``server.py`` with ``pythonw`` (no console).

Modes:
  --mode backend (default): start ComfyUI hidden (CREATE_NO_WINDOW, output to
      a log file, Job Object with KILL_ON_JOB_CLOSE), write server-state.json,
      exit when the watched GIMP pid disappears / idle timeout / stop request;
      also process tasks.
  --mode tasks: no backend; process queued tasks and exit when the queue has
      been empty for a while. Used when a download is requested while the
      backend is not running.

Tasks are JSON files in ``<base>/tasks/``; progress goes to
``<base>/tasks-status.json`` which the plug-in polls. A ``cancel`` file in the
tasks folder cancels the running task.

Depends only on the standard library plus the GIMP-free modules next to it
(``downloads``, ``catalog``, ``config``, ``log``).

Trust: task files are re-validated here against the shipped catalog and the
ComfyUI folder named in ``config.json`` (download URL, destination, size and
sha256; node repo, folder and commit; the interpreter that runs pip), so a
tampered task file cannot make the worker fetch or run anything the catalog
does not list. Live (hub) entries the user picked are read from
``hub-selected.json`` and pass ``catalog.parse_live_entries`` first: one
safetensors/GGUF file from a Hugging Face or Civitai download URL into
``checkpoints``/``unet``/``diffusion_models``, everything else (companion
files, custom nodes) only as listed in the shipped catalog. Pids read from state files are checked against the process
image name before they are trusted, which guards against pid reuse.
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from openfill import catalog, comfy_setup, downloads, local_models  # noqa: E402
from openfill.config import Settings, comfy_layout  # noqa: E402
from openfill.log import redact  # noqa: E402

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------- windows ----


def _job_object_for_child(pid: int) -> Any:
    """Create a Job Object that kills ``pid`` when the handle closes."""
    if not IS_WINDOWS:
        return None
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_uint64)
            for n in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    PROCESS_TERMINATE, PROCESS_SET_QUOTA = 0x0001, 0x0100
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    handle = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
    if handle and k32.AssignProcessToJobObject(job, handle):
        k32.CloseHandle(handle)
        return job  # keep the job handle alive for the life of the supervisor
    return None


def pid_alive(pid: int) -> bool:
    """True while a process with ``pid`` exists (says nothing about which program it is)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        SYNCHRONIZE = 0x00100000
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT -> still running
        finally:
            k32.CloseHandle(handle)
    # A child of ours that has exited stays a zombie (and answers kill(pid, 0)) until it is
    # reaped, e.g. a task worker that died while the Models tab that started it is still open.
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child: only kill() can tell
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_name(pid: int) -> str | None:
    """Lower-case executable base name of ``pid`` (``"pythonw.exe"``), or None if unknown."""
    if pid <= 0:
        return None
    if IS_WINDOWS:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = ctypes.c_uint32(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return Path(buf.value).name.lower()
        finally:
            k32.CloseHandle(handle)
    if sys.platform == "darwin":
        # macOS has no /proc; libproc gives the executable path.
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            buf = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
            if libproc.proc_pidpath(pid, buf, ctypes.sizeof(buf)) <= 0:
                return None
            return Path(os.fsdecode(buf.value)).name.lower() or None
        except (OSError, AttributeError):
            return None
    try:
        return Path(os.readlink(f"/proc/{pid}/exe")).name.lower()
    except OSError:
        pass
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip().lower() or None
    except OSError:
        return None


def pid_is_python(pid: int) -> bool:
    """``pid`` is alive and (when the name is readable) a Python interpreter.

    Used for pids read from state files: after a crash the pid may have been
    reused by an unrelated program, which must not be mistaken for a supervisor.
    """
    if not pid_alive(pid):
        return False
    name = process_name(pid)
    return name is None or "python" in name


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback addresses."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def same_path(a: str | Path, b: str | Path) -> bool:
    """Path equality after resolving links and normalising case on Windows."""
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


# ------------------------------------------------------------------ state ----


def read_json(path: Path) -> dict[str, Any]:
    """Decode a JSON object file; ``{}`` when missing or broken."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write atomically (temporary file, then rename)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def server_busy(url: str) -> bool | None:
    """True/False from ``/queue``; None if the server did not answer."""
    try:
        with urlopen(url.rstrip("/") + "/queue", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("queue_running")) or bool(data.get("queue_pending"))
    except Exception:
        return None


class Logger:
    """Append-only log writer with token redaction (no ``logging`` setup needed)."""

    def __init__(self, path: Path, tag: str) -> None:
        self.path = path
        self.tag = tag
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str, level: str = "INFO") -> None:
        """Append one line; errors are ignored (logging must never stop the supervisor)."""
        msg = redact(msg)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:5s} [{os.getpid()} {self.tag}] {msg}\n")
        except OSError:
            pass


# ---------------------------------------------------------------- backend ----


class Backend:
    """The ComfyUI child process."""

    def __init__(self, cmd: list[str], cwd: str, log_path: str, log: Logger) -> None:
        self.cmd = cmd
        self.cwd = cwd
        self.log_path = log_path
        self.log = log
        self.child: subprocess.Popen | None = None
        self.job: Any = None
        self.log_file: Any = None

    def start(self) -> None:
        """Launch ComfyUI hidden, output to the log file, tied to a kill-on-close Job Object."""
        popen_kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # GIMP's interpreter paths must not leak into ComfyUI
        env.pop("PYTHONHOME", None)
        env["PYTHONIOENCODING"] = "utf-8"
        self.log_file = open(self.log_path, "ab", buffering=0)  # noqa: SIM115 - kept open for the child's lifetime
        self.log(f"starting backend: {' '.join(self.cmd)}")
        self.child = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            env=env,
            **popen_kwargs,
        )
        self.job = _job_object_for_child(self.child.pid)
        self.log(f"backend pid {self.child.pid}, job object {'ok' if self.job else 'unavailable'}")

    @property
    def pid(self) -> int:
        """Pid of the running child, 0 when none."""
        return self.child.pid if self.child else 0

    def alive(self) -> bool:
        """True while the child runs."""
        return self.child is not None and self.child.poll() is None

    def stop(self) -> None:
        """Terminate (then kill) our own child process; never touches other pids."""
        child, self.child = self.child, None
        if child is None:
            return
        if child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=15)
                self.log("backend terminated")
            except Exception:
                try:
                    child.kill()
                    child.wait(timeout=5)
                    self.log("backend killed")
                except Exception as err:
                    self.log(f"could not stop backend: {err}", "ERROR")
        if self.log_file:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
        self.job = None


# ------------------------------------------------------------------ tasks ----


class TaskWorker:
    """Processes ``<base>/tasks/*.json`` one at a time in a thread."""

    def __init__(
        self, base: Path, config_path: Path, log: Logger, on_restart_backend: Callable[[], None] | None = None
    ) -> None:
        self.base = base
        self.tasks_dir = base / "tasks"
        self.status_path = base / "tasks-status.json"
        self.config_path = config_path
        self.log = log
        self.on_restart_backend = on_restart_backend
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.last_activity = time.time()
        self.status: dict[str, Any] = {"worker_pid": os.getpid(), "current": None, "queue": [], "history": []}
        self._thread = threading.Thread(target=self._run, name="tasks", daemon=True)

    # queue helpers -----------------------------------------------------

    def pending(self) -> list[Path]:
        """Queued task files, oldest first."""
        return sorted(p for p in self.tasks_dir.glob("*.json") if not p.name.endswith(".running.json"))

    def cancel_requested(self) -> bool:
        """True when the plug-in dropped a ``cancel`` file."""
        return (self.tasks_dir / "cancel").exists()

    def _clear_cancel(self) -> None:
        try:
            (self.tasks_dir / "cancel").unlink()
        except OSError:
            pass

    def _write_status(self) -> None:
        with self._lock:
            self.status["updated"] = time.time()
            self.status["queue"] = [p.stem for p in self.pending()]
            try:
                write_json(self.status_path, self.status)
            except OSError:
                pass

    def _set_current(self, **fields: Any) -> None:
        with self._lock:
            if self.status["current"] is None:
                self.status["current"] = {}
            self.status["current"].update(fields)
        self._write_status()

    def _finish(self, task: dict[str, Any], state: str, message: str = "", **extra: Any) -> None:
        with self._lock:
            entry = {
                "task_id": task.get("id"),
                "type": task.get("type"),
                "model_id": task.get("model_id"),
                "state": state,
                "message": message,
                "finished": time.time(),
                **extra,
            }
            self.status["history"] = ([entry] + self.status["history"])[:30]
            self.status["current"] = None
        self.last_activity = time.time()
        self._write_status()
        self.log(f"task {task.get('id')} {state}: {message}")

    # lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread, after re-queueing the tasks a stopped worker left half done."""
        self.requeue_orphans()
        self._write_status()
        self._thread.start()

    def requeue_orphans(self) -> list[str]:
        """Put ``*.running.json`` tasks back in the queue and return their names.

        Only one worker runs per profile, so a running task found at start belongs to a
        worker that was killed or shut down with GIMP mid-download. Downloads resume from
        their ``.part`` file.
        """
        names = []
        for running in sorted(self.tasks_dir.glob("*.running.json")):
            queued = running.with_name(running.name[: -len(".running.json")] + ".json")
            try:
                os.replace(running, queued)
            except OSError:
                continue
            names.append(queued.name)
            self.log(f"re-queued {queued.name}: its worker stopped before it finished")
        return names

    def stop(self) -> None:
        """Ask the worker thread to finish after the current task."""
        self._stop.set()

    def idle_for(self) -> float:
        """Seconds since the last task finished or started."""
        return time.time() - self.last_activity

    def busy(self) -> bool:
        """A task is running or queued."""
        with self._lock:
            return self.status["current"] is not None or bool(self.pending())

    def _run(self) -> None:
        while not self._stop.is_set():
            tasks = self.pending()
            if not tasks:
                self._stop.wait(1.0)
                continue
            path = tasks[0]
            running = path.with_name(path.stem + ".running.json")
            try:
                os.replace(path, running)  # atomic claim
            except OSError:
                continue
            task = read_json(running)
            self._clear_cancel()
            self.last_activity = time.time()
            try:
                if task.get("type") == "download":
                    self._download(task)
                elif task.get("type") == "install_nodes":
                    self._install_nodes(task)
                elif task.get("type") == "install_pip":
                    self._install_pip(task)
                elif task.get("type") == "setup_comfy":
                    self._setup_comfy(task)
                elif task.get("type") == "remove":
                    self._remove(task)
                else:
                    self._finish(task, "error", f"unknown task type {task.get('type')}")
            except comfy_setup.SetupError as err:
                self._finish(task, "error", str(err), code="setup")
            except downloads.DownloadError as err:
                self._finish(task, "cancelled" if err.code == "cancelled" else "error", str(err), code=err.code)
            except (PermissionError, http.client.HTTPException) as err:
                self._finish(task, "error", downloads.plain_os_message(err), code="network")
            except Exception as err:  # noqa: BLE001 - must never kill the worker
                self._finish(task, "error", f"{type(err).__name__}: {err}")
            finally:
                try:
                    running.unlink()
                except OSError:
                    pass
                self._clear_cancel()

    # task types --------------------------------------------------------

    def _token(self) -> str:
        return str(read_json(self.config_path).get("hf_token", "") or "")

    # task validation ---------------------------------------------------

    def _comfy_dir(self) -> str:
        return Settings.load(self.config_path, backup=False).comfy_dir

    def _catalog(self) -> list[catalog.ModelEntry]:
        """The shipped catalog plus the live (hub) entries the user chose in the
        Models tab (``hub-selected.json``), each re-validated by the strict live rules,
        plus the user's own model files, recognised again here from their headers."""
        entries = catalog.load_merged(base=self.base)
        comfy_dir = self._comfy_dir()
        if not comfy_dir:
            return entries
        return local_models.with_local(entries, catalog.default_models_root(comfy_dir), self.base)

    def _catalog_files(self, task: dict[str, Any]) -> dict[str, catalog.FileSpec]:
        """Files of the task's catalog entry, keyed by normalised ``dest``."""
        entry = catalog.by_id(self._catalog(), str(task.get("model_id") or ""))
        return {downloads.safe_relative_path(f.dest): f for f in entry.files}

    def _checked_models_root(self, task: dict[str, Any]) -> Path:
        root = Path(task["models_root"])
        expected = catalog.default_models_root(self._comfy_dir())
        if not same_path(root, expected):
            raise ValueError(f"task models folder {root} is not the configured {expected}")
        return root

    def checked_download_files(self, task: dict[str, Any]) -> list[tuple[catalog.FileSpec, Path]]:
        """The task's files as catalog entries with their destination paths.

        Raises ``ValueError`` when the models folder is not the configured one
        or a file (URL plus destination) is not one of the catalog entry named by
        ``model_id``. Size, sha256, the optional flag
        and the gated flag always come from the catalog, never from the task.
        """
        root = self._checked_models_root(task)
        known = self._catalog_files(task)
        out = []
        for f in task["files"]:
            spec = known.get(downloads.safe_relative_path(f["dest"]))
            if spec is None or spec.url != f.get("url"):
                raise ValueError(f"{f.get('dest')!r} from {f.get('url')!r} is not a catalog download")
            out.append((spec, downloads.safe_join(root, spec.dest)))
        return out

    def checked_node_install(self, task: dict[str, Any]) -> tuple[Path, Path, list[catalog.NodeSpec]]:
        """``(custom_nodes_dir, python, nodes)`` of an install task, validated.

        The folder and the interpreter must be the ones of the ComfyUI install
        named in config.json, and every node must match a catalog entry exactly
        (repo, folder and pinned commit). Raises ``ValueError`` otherwise.
        """
        comfy_dir = self._comfy_dir()
        nodes_dir = Path(task["custom_nodes_dir"])
        expected_dir = catalog.default_custom_nodes_dir(comfy_dir)
        if not same_path(nodes_dir, expected_dir):
            raise ValueError(f"task custom_nodes folder {nodes_dir} is not the configured {expected_dir}")
        layout = comfy_layout(comfy_dir)
        python = Path(task["python"])
        if layout is None or not same_path(python, layout["python"]):
            raise ValueError(f"task interpreter {python} is not the ComfyUI interpreter of {comfy_dir}")
        known = {(n.repo, n.folder, n.commit): n for e in self._catalog() for n in e.requires_nodes}
        nodes = []
        for n in task["nodes"]:
            spec = known.get((n.get("repo"), n.get("folder"), n.get("commit")))
            if spec is None:
                raise ValueError(f"custom node {n.get('folder')!r} at {n.get('commit')!r} is not in the catalog")
            nodes.append(spec)
        return nodes_dir, python, nodes

    # task types --------------------------------------------------------

    def _download(self, task: dict[str, Any]) -> None:
        files = self.checked_download_files(task)
        live = catalog.by_id(self._catalog(), str(task.get("model_id") or "")).source != "bundled"
        total_all = sum(int(spec.size or 0) for spec, _ in files)
        done_before = 0
        results = []
        for index, (spec, dest) in enumerate(files):
            if spec.optional and dest.is_file():
                continue
            self._set_current(
                task_id=task.get("id"),
                type="download",
                model_id=task.get("model_id"),
                file=spec.dest,
                file_index=index,
                file_count=len(files),
                done=done_before,
                total=total_all,
                speed=0.0,
                state="downloading",
                started=time.time(),
            )

            def progress(done: int, total: int | None, speed: float, _base: int = done_before) -> None:
                self._set_current(
                    done=_base + done, total=total_all or (total or 0), speed=speed, file_done=done, file_total=total
                )

            # The token is only attached for gated files; download_file further restricts it to
            # https Hugging Face hosts and never forwards it on a redirect.
            token = self._token() if spec.gated or task.get("use_token") else None
            self.log(f"download {spec.url} -> {dest}")
            result = downloads.download_file(
                spec.url,
                dest,
                spec.size,
                spec.sha256,
                token or None,
                progress,
                self.cancel_requested,
                keep_existing=live,
            )
            recorded = False
            if not spec.sha256 and result.sha256:
                # a live file without a published checksum: pin what was downloaded
                try:
                    recorded = catalog.record_live_checksum(
                        self.base, str(task.get("model_id") or ""), spec.dest, result.sha256
                    )
                except OSError as err:
                    self.log(f"could not record the checksum of {spec.dest}: {err}", "WARNING")
                self.log(f"{spec.dest}: no published checksum; downloaded sha256 {result.sha256}")
            results.append(
                {
                    "dest": spec.dest,
                    "sha256": result.sha256,
                    "verified": bool(spec.sha256),
                    "sha256_recorded": recorded,
                    "skipped": result.skipped,
                    "resumed_from": result.resumed_from,
                }
            )
            done_before += int(spec.size or result.bytes_written or 0)
        self._finish(task, "done", f"{len(results)} file(s) ready", files=results)

    def _install_nodes(self, task: dict[str, Any]) -> None:
        nodes_dir, python, nodes = self.checked_node_install(task)
        for n in nodes:
            self._set_current(
                task_id=task.get("id"),
                type="install_nodes",
                model_id=task.get("model_id"),
                file=n.folder,
                state="installing",
                done=0,
                total=0,
                started=time.time(),
            )
            downloads.install_custom_node(n.repo, n.folder, n.commit, nodes_dir, python, self.log)
        self._ensure_opencv(python, task)
        if self.on_restart_backend and task.get("restart_backend", True):
            self._set_current(state="restarting backend")
            self.on_restart_backend()
        self._finish(
            task, "done", "custom nodes installed" + (" and backend restarted" if self.on_restart_backend else "")
        )

    def _ensure_opencv(self, python: Path, task: dict[str, Any]) -> None:
        """Install the pinned OpenCV next to the inpaint nodes when ComfyUI cannot import it.

        The nodes' border pre-fill (navier-stokes / telea) needs it; without it fills and
        Generative Expand start from a flat colour. A ComfyUI set up by hand on macOS or
        Linux usually lacks it. Best effort: the nodes work without it.
        """
        packages = comfy_setup.load_manifest().pip
        if not packages or downloads.has_module(python, "cv2"):
            return
        self._set_current(task_id=task.get("id"), type=task.get("type"), file="OpenCV", state="installing")
        try:
            downloads.install_pip_packages(python, packages, self.log)
        except RuntimeError as err:
            self.log(f"could not install OpenCV: {err}", "WARNING")

    def checked_pip_install(self, task: dict[str, Any]) -> Path:
        """The ComfyUI interpreter of a pip task, validated: only the configured ComfyUI's
        interpreter and only the pinned packages of comfy_setup.json. Raises ``ValueError``."""
        comfy_dir = self._comfy_dir()
        layout = comfy_layout(comfy_dir)
        python = Path(str(task.get("python") or ""))
        if layout is None or not same_path(python, layout["python"]):
            raise ValueError(f"task interpreter {python} is not the ComfyUI interpreter of {comfy_dir}")
        if list(task.get("packages") or []) != list(comfy_setup.load_manifest().pip):
            raise ValueError("only the pinned packages of comfy_setup.json can be installed")
        return python

    def _install_pip(self, task: dict[str, Any]) -> None:
        python = self.checked_pip_install(task)
        self._set_current(
            task_id=task.get("id"), type="install_pip", file="OpenCV", state="installing", started=time.time()
        )
        if downloads.has_module(python, "cv2"):
            self._finish(task, "done", "OpenCV is already installed")
            return
        downloads.install_pip_packages(python, task["packages"], self.log)
        self._finish(task, "done", "OpenCV installed")

    def _setup_comfy(self, task: dict[str, Any]) -> None:
        """Download and install the pinned ComfyUI (see comfy_setup.py), then save it as the ComfyUI folder."""
        manifest = comfy_setup.load_manifest()
        if task.get("older_gpu"):
            manifest = manifest.for_older_gpus()  # CUDA 12.6 PyTorch: runs GTX 10 and older NVIDIA cards
            self.log(f"older NVIDIA card: using {manifest.archive_name}")
        install_dir = Path(str(task.get("install_dir") or ""))
        if not install_dir.is_absolute():
            raise comfy_setup.SetupError(f"the install folder {str(install_dir)!r} is not an absolute path")
        started = time.time()

        def progress(state: str, done: int, total: int) -> None:
            self._set_current(
                task_id=task.get("id"),
                type="setup_comfy",
                state=state,
                done=done,
                total=total,
                started=started,
                step=comfy_setup.STATES.index(state) + 1,
                steps=len(comfy_setup.STATES),
                fraction=comfy_setup.overall_fraction(state, done, total),
            )

        comfy = comfy_setup.run_setup(
            manifest, install_dir, progress, self.cancel_requested, self.log, popen=subprocess.Popen
        )
        settings = Settings.load(self.config_path, backup=False)
        settings.comfy_dir = str(comfy)
        if task.get("cpu") and "--cpu" not in settings.extra_server_args.split():
            settings.extra_server_args = (settings.extra_server_args + " --cpu").strip()
        if task.get("cpu"):
            settings.cpu_comfy_dir = str(comfy)
        settings.save(self.config_path)
        self._finish(task, "done", f"ComfyUI is ready in {comfy}", comfy_dir=str(comfy))

    def _remove(self, task: dict[str, Any]) -> None:
        root = self._checked_models_root(task)
        known = self._catalog_files(task)
        removed = []
        for rel in task["files"]:
            try:
                rel = downloads.safe_relative_path(rel)
                path = downloads.safe_join(root, rel)
            except ValueError:
                continue
            if rel not in known:
                continue  # only files the catalog put there are ever deleted
            for p in (path, downloads.part_path(path)):
                if p.is_file():
                    p.unlink()
                    removed.append(str(p))
        self._finish(task, "done", f"removed {len(removed)} file(s)", removed=removed)


# ------------------------------------------------------------------- main ----


def build_parser() -> argparse.ArgumentParser:
    """The supervisor's command line. ``server.py`` passes ``--extra-args=<value>`` as one
    item so a value that starts with ``-`` (``--cpu``) is not read as an option."""
    parser = argparse.ArgumentParser(description="OpenFill backend supervisor")
    parser.add_argument("--mode", choices=("backend", "tasks"), default="backend")
    parser.add_argument("--python", default="", help="interpreter that runs ComfyUI")
    parser.add_argument("--main", default="", help="path to ComfyUI main.py")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--base", required=True, help="the plug-in's folder in the GIMP profile")
    parser.add_argument("--config", default="", help="config.json (for the Hugging Face token)")
    parser.add_argument("--watch-pid", type=int, default=0, help="exit when this pid ends")
    parser.add_argument("--idle-minutes", type=float, default=20.0, help="0 disables")
    parser.add_argument("--extra-args", default="", help="extra ComfyUI arguments (pass as --extra-args=VALUE)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point (see the module docstring for the modes)."""
    args = build_parser().parse_args(argv)

    base = Path(args.base)
    base.mkdir(parents=True, exist_ok=True)
    state_path = base / "server-state.json"
    worker_path = base / "tasks-worker.json"
    config_path = Path(args.config) if args.config else base / "config.json"
    log = Logger(base / "openfill.log", "supervisor")

    if args.mode == "tasks":
        return run_tasks_only(base, worker_path, state_path, config_path, log)
    return run_backend(args, base, state_path, worker_path, config_path, log)


def run_tasks_only(base: Path, worker_path: Path, state_path: Path, config_path: Path, log: Logger) -> int:
    """Process queued tasks without a backend; exit after 30 s with an empty queue."""
    existing = read_json(worker_path)
    if existing.get("pid") and pid_is_python(int(existing["pid"])) and existing["pid"] != os.getpid():
        log(f"another task worker ({existing['pid']}) is alive; exiting")
        return 0
    status = read_json(base / "tasks-status.json")
    if status.get("worker_pid") and pid_is_python(int(status["worker_pid"])) and status["worker_pid"] != os.getpid():
        log("backend supervisor is alive and handles tasks; exiting")
        return 0
    write_json(worker_path, {"pid": os.getpid(), "started": time.time(), "mode": "tasks"})

    def ask_backend_restart() -> None:
        """No backend of our own: flag the backend supervisor (if any) to restart."""
        state = read_json(state_path)
        if state.get("supervisor_pid") and pid_is_python(int(state["supervisor_pid"])):
            state["restart_backend"] = True
            write_json(state_path, state)
            log("asked the backend supervisor to restart ComfyUI")

    worker = TaskWorker(base, config_path, log, on_restart_backend=ask_backend_restart)
    worker.start()
    log("task worker started (no backend)")
    try:
        while True:
            time.sleep(2.0)
            if not worker.busy() and worker.idle_for() > 30:
                break
            if read_json(state_path).get("supervisor_pid") not in (None, os.getpid()) and not worker.busy():
                break  # a backend supervisor appeared; let it take over
    finally:
        worker.stop()
        try:
            worker_path.unlink()
        except OSError:
            pass
        log("task worker exiting")
    return 0


START_LOCK_FILENAME = "supervisor.lock"
WATCHERS_DIRNAME = "watchers"


def acquire_start_lock(path: Path) -> bool:
    """One backend supervisor per profile: create ``path`` exclusively with our pid inside.

    Two plug-ins that start at the same moment both find no state file; without this lock both
    launch ComfyUI. A lock whose pid is dead or not a Python process is stale and taken over.
    """
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                holder = 0
            if holder and holder != os.getpid() and pid_is_python(holder):
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def release_start_lock(path: Path) -> None:
    """Remove ``path`` if it is ours."""
    try:
        if int(path.read_text(encoding="utf-8").strip() or 0) == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def live_watchers(base: Path, names: tuple[str, ...] = ("gimp",)) -> list[int]:
    """Pids registered in ``<base>/watchers`` (one file per pid, written by the plug-in of every
    GIMP, or by a companion app, that uses the backend). A file may hold the process name its
    writer expects (the companion runs as Python); otherwise the process must be a GIMP. Entries
    whose process is gone or is another program (a reused pid) are removed."""
    folder = base / WATCHERS_DIRNAME
    alive = []
    for entry in folder.glob("*") if folder.is_dir() else []:
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        name = process_name(pid) if pid_alive(pid) else None
        try:
            expected = entry.read_text(encoding="utf-8").strip().lower()
        except OSError:
            expected = ""
        wanted = (expected,) if expected else names
        if pid_alive(pid) and (name is None or any(n in name for n in wanted)):
            alive.append(pid)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    return sorted(alive)


def run_backend(
    args: argparse.Namespace, base: Path, state_path: Path, worker_path: Path, config_path: Path, log: Logger
) -> int:
    """Hold the per-profile start lock for the supervisor's lifetime, then run the backend."""
    lock = base / START_LOCK_FILENAME
    if not acquire_start_lock(lock):
        log("another supervisor is starting or running (start lock held); exiting")
        return 0
    try:
        return _run_backend(args, base, state_path, worker_path, config_path, log)
    finally:
        release_start_lock(lock)


def _run_backend(
    args: argparse.Namespace, base: Path, state_path: Path, worker_path: Path, config_path: Path, log: Logger
) -> int:
    """Own the ComfyUI process until every GIMP using it exits, the idle timeout passes or a stop is requested."""
    existing = read_json(state_path)
    if (
        existing.get("supervisor_pid")
        and pid_is_python(int(existing["supervisor_pid"]))
        and existing.get("supervisor_pid") != os.getpid()
    ):
        log(f"another supervisor ({existing['supervisor_pid']}) is alive; exiting")
        return 0

    cmd = [
        args.python,
        "-s",
        args.main,
        "--listen",
        args.host,
        "--port",
        str(args.port),
        "--disable-auto-launch",
        "--dont-print-server",
        "--preview-method",
        "taesd",
    ]
    if args.extra_args.strip():
        cmd += args.extra_args.split()
    url = f"http://{args.host}:{args.port}"
    if not is_loopback(args.host):
        log(
            f"ComfyUI will listen on {args.host}, reachable from other machines; "
            "use 127.0.0.1 in the settings unless that is intended",
            "WARN",
        )

    backend = Backend(cmd, args.cwd, str(base / "comfyui.log"), log)
    backend.start()
    watch_name = process_name(args.watch_pid) if args.watch_pid else None
    log(f"watching gimp pid {args.watch_pid} ({watch_name or 'name unknown'})")

    handed_over: list[int] = []

    def watched_alive() -> bool:
        # A reused pid (another program after GIMP exited) counts as gone.
        if pid_alive(args.watch_pid) and (watch_name is None or process_name(args.watch_pid) in (None, watch_name)):
            return True
        # Other GIMPs that use this backend (same profile) keep it alive.
        others = [p for p in live_watchers(base) if p != args.watch_pid]
        if others and others != handed_over:
            log(f"GIMP (pid {args.watch_pid}) is gone; still serving GIMP pid(s) {others}")
            handed_over[:] = others
        return bool(others)

    now = time.time()
    state = {
        "supervisor_pid": os.getpid(),
        "server_pid": backend.pid,
        "url": url,
        "port": args.port,
        "host": args.host,
        "started": now,
        "last_used": now,
        "watch_pid": args.watch_pid,
        "idle_minutes": args.idle_minutes,
        "command": cmd,
        "status": "starting",
    }
    write_json(state_path, state)

    restart_lock = threading.Lock()

    def restart_backend() -> None:
        with restart_lock:
            log("restarting backend after node install")
            backend.stop()
            backend.start()
            current = read_json(state_path)
            current.update({"server_pid": backend.pid, "status": "starting", "started": time.time()})
            write_json(state_path, current)

    worker = None
    other_worker = read_json(worker_path)
    if not (other_worker.get("pid") and pid_is_python(int(other_worker["pid"]))):
        worker = TaskWorker(base, config_path, log, on_restart_backend=restart_backend)
        worker.start()

    reason = "unknown"
    idle_limit = args.idle_minutes * 60.0 if args.idle_minutes > 0 else 0.0
    last_busy = now
    try:
        while True:
            time.sleep(2.0)
            if not backend.alive() and not restart_lock.locked():
                reason = f"backend exited with code {backend.child.returncode if backend.child else '?'}"
                break
            if args.watch_pid and not watched_alive():
                reason = f"GIMP (pid {args.watch_pid}) is gone" + (f" and so are {handed_over}" if handed_over else "")
                break
            current = read_json(state_path)
            if current.get("stop"):
                reason = "stop requested"
                break
            if current.get("supervisor_pid") not in (None, os.getpid()):
                reason = "state file taken over by another supervisor"
                backend.child = None  # do not kill the other supervisor's server
                break
            if current.get("restart_backend"):
                current.pop("restart_backend", None)
                write_json(state_path, current)
                restart_backend()
                continue
            if worker is None:
                other = read_json(worker_path)
                if not (other.get("pid") and pid_is_python(int(other["pid"]))):
                    worker = TaskWorker(base, config_path, log, on_restart_backend=restart_backend)
                    worker.start()
                    log("adopted task processing (standalone worker is gone)")
            last_used = float(current.get("last_used", state["last_used"]))
            busy = server_busy(url)
            if busy or (worker and worker.busy()):
                last_busy = time.time()
            if busy is not None and current.get("status") != "ready":
                current["status"] = "ready"
                write_json(state_path, current)
            if idle_limit and busy is False and not (worker and worker.busy()):
                idle_for = time.time() - max(last_used, last_busy)
                if idle_for > idle_limit:
                    reason = f"idle for {idle_for / 60:.1f} min (limit {args.idle_minutes:g})"
                    break
    finally:
        log(f"shutting down: {reason}")
        if worker:
            worker.stop()
        backend.stop()
        if backend.child is None and reason != "state file taken over by another supervisor":
            try:
                state_path.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
