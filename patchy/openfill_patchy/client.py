# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Client for Patchy's connector: ``patchy-mcp --attach`` as a child process, speaking
newline-delimited JSON-RPC (MCP) on its stdin and stdout.

See Patchy's docs/companions.md and docs/ai-control.md. The connector is a proxy into the
running Patchy: every edit carries the state token of the latest ``get_state``
(``expectedState``), and a script run is one undo step. With ``patchy/events`` declared at
``initialize``, Patchy pushes ``notifications/patchy/event`` (selection, documents, the
Plugins > Companions menu); a reader thread hands them to ``on_event`` in order.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

COMPANION_ID = "io.github.beckettharriman.openfill"
PROTOCOL_VERSION = "2025-11-25"
# Patchy's companion protocol versions this OpenFill speaks (Patchy's docs/companions.md,
# "Protocol"). Declared at initialize, written into the menu entry, and compared with
# get_info's companionProtocol at start.
COMPANION_PROTOCOL_MIN = 1
COMPANION_PROTOCOL_MAX = 1
# The first Patchy release with Plugins > Companions, named when a Patchy has none.
FIRST_PATCHY_WITH_COMPANIONS = "1.25"
FLATPAK_ID = "com.rtsoft.patchy"  # Patchy's Flatpak (Linux)
# Patchy answers "busy" while the artist is mid-gesture, in a modal dialog, or another
# client's script runs. Nothing ran, so the request is sent again, for this long at most.
BUSY_PATIENCE_S = 30 * 60.0
# A drag of a selection's handles keeps Patchy busy for a moment; ``on_wait`` hears of it only
# once Patchy has stayed busy this long in a row, so a gesture never flashes a warning.
BUSY_NOTICE_S = 3.0
STALE_ATTEMPTS = 4  # an edit refused as stale is sent again with the newer token this often


def companion_info() -> dict[str, Any]:
    """What OpenFill tells Patchy about itself at ``initialize``."""
    from . import __version__

    return {
        "id": COMPANION_ID,
        "name": "OpenFill",
        "version": __version__,
        "minProtocol": COMPANION_PROTOCOL_MIN,
        "maxProtocol": COMPANION_PROTOCOL_MAX,
    }


def protocol_problem(info: dict[str, Any]) -> str | None:
    """Why this OpenFill cannot work with the Patchy that answered ``get_info``, in words
    for the artist, or None when they share a protocol version. Patchy's own version is
    never compared: development builds report the last release's."""
    version = str(info.get("version") or "").strip()
    patchy = f"Patchy {version}" if version else "this Patchy"
    served = info.get("companionProtocol")
    if not isinstance(served, dict):
        return (
            f"OpenFill needs Patchy {FIRST_PATCHY_WITH_COMPANIONS} or newer, the first release with "
            f"Plugins > Companions. Update Patchy: {patchy} has no companion apps."
        )
    low, high = served.get("min"), served.get("max")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in (low, high)) or low > high:
        return f"OpenFill does not understand the companion protocol {patchy} reports ({served!r})."
    if high < COMPANION_PROTOCOL_MIN:
        return f"OpenFill needs a newer version of Patchy than {patchy}. Update Patchy."
    if low > COMPANION_PROTOCOL_MAX:
        return f"This OpenFill is too old for {patchy}. Update OpenFill."
    return None


class PatchyError(RuntimeError):
    """Patchy refused a request or could not be reached. ``code`` is Patchy's error id
    (``busy``, ``stale_state``, ``operation_failed``, ...) when it gave one."""

    def __init__(self, message: str, code: str = "", data: dict | None = None) -> None:
        super().__init__(message)
        self.code, self.data = code, data or {}


class WorkspaceLost(PatchyError):
    """Patchy is gone, or went away during a request (``workspace_disconnected``: the
    request may have run, so it is never sent again)."""


def flatpak_patchy_installed() -> bool:
    """Patchy's Flatpak is installed for this user or for the whole system (Linux)."""
    if not sys.platform.startswith("linux"):
        return False
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return any((root / "app" / FLATPAK_ID).is_dir() for root in (data / "flatpak", Path("/var/lib/flatpak")))


def find_connector(explicit: str | None = None) -> list[str] | None:
    """The command that starts Patchy's connector in attached mode, or None.

    ``PATCHY_MCP`` is set when Patchy starts a companion from Plugins > Companions. A Patchy
    Flatpak cannot start one (the sandbox runs no host programs): the artist starts OpenFill,
    and it runs the Flatpak's own connector, which reaches the Patchy inside the sandbox."""
    for candidate in (explicit, os.environ.get("PATCHY_MCP")):
        if candidate:
            return [candidate, "--attach"]
    home = Path.home()
    paths: list[Path] = []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        paths.append(local / "Programs" / "Patchy" / "patchy-mcp.exe")
    elif sys.platform == "darwin":
        for folder in (Path("/Applications"), home / "Applications"):
            paths.append(folder / "Patchy.app" / "Contents" / "MacOS" / "patchy-mcp")
    for path in paths:
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path), "--attach"]
    found = shutil.which("patchy-mcp")  # Linux: the script beside patchy
    if found:
        return [found, "--attach"]
    flatpak = shutil.which("flatpak")
    if flatpak and flatpak_patchy_installed():
        return [flatpak, "run", "--command=patchy-mcp", FLATPAK_ID, "--attach"]
    return None


class PatchyClient:
    """One connector process, shared by threads (tool calls are serialised: the connector
    takes one request at a time)."""

    def __init__(
        self,
        command: list[str],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600.0,
        log: Path | None = None,
    ) -> None:
        self.command, self.env, self.timeout, self.log = list(command), env, timeout, log
        self.on_event = on_event
        self.on_wait: Callable[[bool], None] | None = None  # told when Patchy stays busy, and when it answers
        self.state_token = ""
        self.lost = False  # set once Patchy is gone; cleared by a successful read
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()  # one request at a time
        self._replies: dict[int, queue.Queue] = {}
        self._replies_lock = threading.Lock()
        self._events: queue.Queue = queue.Queue()
        self._alive_at = 0.0
        self._alive = True
        self._misses = 0
        self._looking = False

    # process ------------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Start the connector and initialize the MCP session; the server's reply."""
        stderr = open(self.log, "ab") if self.log else subprocess.DEVNULL  # noqa: SIM115
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": stderr,
            "env": self.env if self.env is not None else dict(os.environ),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = self._spawn(kwargs)
        except OSError as err:
            raise PatchyError(f"could not start Patchy's connector {self.command[0]}: {err}") from err
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        threading.Thread(target=self._read, name="openfill-patchy-read", daemon=True).start()
        threading.Thread(target=self._dispatch, name="openfill-patchy-events", daemon=True).start()
        from . import __version__

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"experimental": {"patchy/events": True, "patchy/companion": companion_info()}},
                "clientInfo": {"name": "OpenFill", "version": __version__},
            },
            timeout=60.0,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def _spawn(self, kwargs: dict[str, Any]) -> subprocess.Popen:
        """The connector process (the tests put a fake in its place)."""
        return subprocess.Popen(self.command, **kwargs)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the connector ends with its stdin; Patchy stays as it is
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise WorkspaceLost("the connection to Patchy is closed")
        try:
            proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError as err:
            raise WorkspaceLost(f"lost the connection to Patchy: {err}") from err

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._replies_lock:
                    slot = self._replies.get(message["id"])
                if slot is not None:
                    slot.put(message)
            elif message.get("method") == "notifications/patchy/event":
                self._events.put(message.get("params") or {})
        # The connector ended: every waiting request learns it.
        self.lost = True
        with self._replies_lock:
            for slot in self._replies.values():
                slot.put(None)
        self._events.put(None)

    def _dispatch(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            if self.on_event:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 - one bad event must not stop the rest
                    import traceback

                    traceback.print_exc()

    def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        request_id = next(self._ids)
        slot: queue.Queue = queue.Queue()
        with self._replies_lock:
            self._replies[request_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                reply = slot.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise PatchyError(f"{method}: Patchy did not answer in time") from None
        finally:
            with self._replies_lock:
                self._replies.pop(request_id, None)
        if reply is None:
            raise WorkspaceLost("Patchy's connector closed")
        if "error" in reply:
            error = reply["error"] or {}
            raise PatchyError(f"{method}: {error.get('message')}", str(error.get("code", "")))
        return reply.get("result") or {}

    # tools ---------------------------------------------------------------------

    def tool(self, tool: str, /, patience: float = BUSY_PATIENCE_S, **arguments: Any) -> dict[str, Any]:
        """Call one tool and return its ``structuredContent``; raises ``PatchyError``.

        ``busy`` is sent again until ``patience`` runs out (nothing ran). A request Patchy
        lost (``workspace_disconnected``) is never sent again."""
        waited_since = None
        told = False  # on_wait(True) was sent
        try:
            while True:
                with self._lock:
                    result = self._request("tools/call", {"name": tool, "arguments": arguments})
                data = result.get("structuredContent") or {}
                if not result.get("isError"):
                    return data
                code = str(data.get("error") or "")
                message = str(data.get("message") or data.get("logs") or "Patchy refused the request")
                if code in ("workspace_disconnected", "workspace_unavailable"):
                    self.lost = True
                    raise WorkspaceLost(f"{tool}: {message}", code, data)
                if code != "busy":
                    raise PatchyError(f"{tool}: {message}", code, data)
                now = time.monotonic()
                if waited_since is None:
                    waited_since = now
                if not told and self.on_wait and now - waited_since >= BUSY_NOTICE_S:
                    self.on_wait(True)
                    told = True
                if now - waited_since > patience:
                    raise PatchyError(f"{tool}: Patchy stayed busy", code, data)
                time.sleep(min(1.0, 0.1 + (now - waited_since) / 10))
        finally:
            if told and self.on_wait:
                self.on_wait(False)

    def get_info(self, patience: float = BUSY_PATIENCE_S) -> dict[str, Any]:
        return self.tool("get_info", patience=patience)

    def get_state(self, patience: float = BUSY_PATIENCE_S) -> dict[str, Any]:
        """Patchy's documents, layers, selection and history; remembers the state token."""
        state = self.tool("get_state", patience=patience)
        self.state_token = str(state.get("stateToken") or self.state_token)
        self.lost = False
        return state

    def _edit(self, tool: str, retry_stale: bool, /, **arguments: Any) -> dict[str, Any]:
        for attempt in range(STALE_ATTEMPTS):
            if not self.state_token:
                self.get_state()
            try:
                data = self.tool(tool, expectedState=self.state_token, **arguments)
            except WorkspaceLost:
                raise
            except PatchyError as err:
                newer = (err.data.get("state") or {}).get("stateToken")
                if newer:
                    self.state_token = str(newer)
                if err.code == "stale_state" and retry_stale and attempt < STALE_ATTEMPTS - 1:
                    continue
                raise
            newer = (data.get("state") or {}).get("stateToken")
            if newer:
                self.state_token = str(newer)
            return data
        raise PatchyError(f"{tool}: the state kept changing", "stale_state")

    def run_script(self, code: str, name: str = "OpenFill", retry_stale: bool = True) -> Any:
        """Run JavaScript in Patchy (one undo step per document it changes); its
        ``patchy.setResult`` value. ``retry_stale``: a stale token is refreshed and the run sent
        again (right for edits that do not depend on what changed, such as an insert at a
        saved mask)."""
        data = self._edit("execute_script", retry_stale, code=code, name=name)
        return data.get("result")

    def undo(self, document_id: str) -> bool:
        """One Undo in ``document_id``, only if nothing changed since the last ``get_state``."""
        return bool(self._edit("undo", False, documentId=str(document_id)).get("changed"))

    def redo(self, document_id: str) -> bool:
        """One Redo in ``document_id``, only if nothing changed since the last ``get_state``."""
        return bool(self._edit("redo", False, documentId=str(document_id)).get("changed"))

    def alive(self, every: float = 3.0) -> bool:
        """True while Patchy answers. Never waits: a look (``get_info``) runs in its own
        thread at most every ``every`` seconds, and two failures in a row end it, so a slow
        moment does not end the companion."""
        if self._proc is None or self._proc.poll() is not None:
            return False
        now = time.monotonic()
        if now - self._alive_at >= every and not self._looking:
            self._alive_at, self._looking = now, True
            threading.Thread(target=self._look, name="openfill-patchy-alive", daemon=True).start()
        return self._alive

    def _look(self) -> None:
        try:
            self.get_info(patience=0)
            self._alive, self._misses = True, 0
        except WorkspaceLost:
            self._misses += 1
            self._alive = self._misses < 2
        except PatchyError:
            self._alive = True  # busy or slow, but there
        finally:
            self._looking = False
