# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Client for PhotoCraft's control channel: authenticated JSON lines on 127.0.0.1.

See PhotoCraft's docs/control-protocol.md. The app closes a connection that is
idle for 30 s, so a call after a pause reconnects first.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

IDLE_RECONNECT_S = 20.0  # below the app's 30 s idle limit
# PhotoCraft runs requests from its frame loop. On Wayland a window that is fully covered or
# on a sleeping display draws no frames, so a request waits and is dropped after 60 s with
# "timeout: not run", which is safe to send again. Keep trying this long.
NOT_RUN_PATIENCE_S = 30 * 60.0


class ControlError(RuntimeError):
    """PhotoCraft refused a request or could not be reached."""


class _NotRun(Exception):
    """PhotoCraft dropped the request before running it (safe to send again)."""


class ControlClient:
    """One authenticated connection, shared by threads (calls are serialised)."""

    def __init__(self, port: int, token_file: Path, host: str = "127.0.0.1", timeout: float = 180.0) -> None:
        self.host, self.port, self.token_file, self.timeout = host, port, Path(token_file), timeout
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._last = 0.0
        self.on_wait: Callable[[bool], None] | None = None  # told when PhotoCraft stops and resumes answering

    # connection -----------------------------------------------------------

    def _connect(self) -> None:
        self.close()
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as err:
            raise ControlError(f"no control token at {self.token_file}: {err}") from err
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as err:
            raise ControlError(
                f"PhotoCraft is not answering on {self.host}:{self.port}. Start it with the OpenFill launcher."
            ) from err
        self._file = self._sock.makefile("rwb")
        reply = self._exchange({"id": "auth", "method": "auth", "params": {"token": token}})
        if not reply.get("ok"):
            raise ControlError(f"PhotoCraft refused the control token: {reply.get('error')}")

    def close(self) -> None:
        for thing in (self._file, self._sock):
            try:
                if thing is not None:
                    thing.close()
            except OSError:
                pass
        self._file = self._sock = None

    def _exchange(self, message: dict[str, Any]) -> dict[str, Any]:
        assert self._file is not None
        self._file.write((json.dumps(message) + "\n").encode("utf-8"))
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("PhotoCraft closed the control connection")
        self._last = time.monotonic()
        return json.loads(line)

    # calls ------------------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        """Run one control method and return its ``result``; raises ``ControlError``."""
        waited_since = None
        try:
            while True:
                try:
                    return self._call_once(method, params)
                except _NotRun:
                    now = time.monotonic()
                    if waited_since is None:
                        waited_since = now
                        if self.on_wait:
                            self.on_wait(True)
                    if now - waited_since > NOT_RUN_PATIENCE_S:
                        raise ControlError(
                            f"{method}: PhotoCraft did not answer for {NOT_RUN_PATIENCE_S / 60:.0f} minutes"
                        ) from None
        finally:
            if waited_since is not None and self.on_wait:
                self.on_wait(False)

    def _call_once(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            for attempt in (1, 2):
                if self._sock is None or time.monotonic() - self._last > IDLE_RECONNECT_S:
                    self._connect()
                self._next_id += 1
                try:
                    reply = self._exchange({"id": self._next_id, "method": method, "params": params})
                except (OSError, ConnectionError, ValueError) as err:
                    self.close()
                    if attempt == 2:
                        raise ControlError(f"lost the connection to PhotoCraft: {err}") from err
                    continue
                if not reply.get("ok"):
                    error = str(reply.get("error"))
                    if error.startswith("timeout: not run"):
                        raise _NotRun(error)
                    raise ControlError(f"{method}: {error}")
                return reply.get("result")
        raise ControlError(f"{method}: no reply")

    def run(self, command: str, **params: Any) -> Any:
        """Run an engine command by id (``engine.execute``)."""
        return self.call("engine.execute", command=command, params=params)

    def alive(self) -> bool:
        try:
            self.call("ui.inspect")
            return True
        except ControlError:
            return False
