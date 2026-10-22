# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""OpenFill's entries in PhotoCraft's Filter > Plug-ins menu, and the watch that notices them.

PhotoCraft's plug-ins are sandboxed WebAssembly filters that cannot reach anything
outside the pixels they are given. So each OpenFill entry is a filter that changes
nothing: picking it leaves a history step named after it, which ``Watcher`` sees on
its next look at the document. The watcher deletes that step (so Undo and Redo never
meet it) and tells the companion which entry was picked.

The modules are written here, byte by byte (ABI v1 in PhotoCraft's docs/plugins.md):
no imports, a manifest in a data segment, a bump allocator and a ``pc_filter`` that
returns 0.
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .control import ControlClient, ControlError

# (action, menu label). The label is also the name of the history step the entry leaves.
# One entry: everything else is in the OpenFill bar it opens.
ENTRIES = (("panel", "OpenFill"),)
# Entries earlier versions added (removed from a PhotoCraft that still has them).
OLD_ACTIONS = ("fill", "remove", "expand", "txt2img")
PLUGIN_PREFIX = "app.openfill."
VERSION = "1.0.0"


# WebAssembly --------------------------------------------------------------------------


def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        done = (n == 0 and not byte & 0x40) or (n == -1 and byte & 0x40)
        out.append(byte if done else byte | 0x80)
        if done:
            return bytes(out)


def _vec(items: list[bytes]) -> bytes:
    return _uleb(len(items)) + b"".join(items)


def _section(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + _uleb(len(body)) + body


def _name(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _uleb(len(raw)) + raw


def _func(local_i32: int, code: bytes) -> bytes:
    body = _vec([_uleb(local_i32) + b"\x7f"] if local_i32 else []) + code + b"\x0b"
    return _uleb(len(body)) + body


I32, I64 = 0x7F, 0x7E


def plugin_module(manifest: dict[str, Any]) -> bytes:
    """A filter plug-in that leaves the pixels as they are."""
    data = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    base = 1024
    types = [
        b"\x60" + _vec([]) + _vec([bytes([I32])]),  # () -> i32
        b"\x60" + _vec([]) + _vec([bytes([I64])]),  # () -> i64
        b"\x60" + _vec([bytes([I32])]) + _vec([bytes([I32])]),  # (i32) -> i32
        b"\x60" + _vec([bytes([I32])] * 8) + _vec([bytes([I32])]),  # pc_filter
    ]
    abi = _func(0, b"\x41" + _sleb(1))
    manifest_ptr = _func(0, b"\x42" + _sleb((len(data) << 32) | base))
    # pc_alloc(size): grow by ceil(size / 64 KiB) pages and return the old end, or 0
    alloc = _func(
        1,
        b"\x20\x00\x41"
        + _sleb(0xFFFF)
        + b"\x6a\x41"
        + _sleb(16)
        + b"\x76"  # (size + 65535) >> 16
        + b"\x40\x00\x22\x01"  # memory.grow, local.tee 1
        + b"\x41"
        + _sleb(-1)
        + b"\x46"  # == -1
        + b"\x04\x7f\x41\x00\x05\x20\x01\x41"
        + _sleb(16)
        + b"\x74\x0b",  # if 0 else old << 16
    )
    no_op = _func(0, b"\x41\x00")
    exports = [
        _name("memory") + b"\x02\x00",
        _name("pc_abi_version") + b"\x00\x00",
        _name("pc_manifest") + b"\x00\x01",
        _name("pc_alloc") + b"\x00\x02",
        _name("pc_filter") + b"\x00\x03",
    ]
    return (
        b"\x00asm"
        + struct.pack("<I", 1)
        + _section(1, _vec(types))
        + _section(3, _vec([_uleb(i) for i in range(4)]))
        + _section(5, _vec([b"\x00" + _uleb(1)]))
        + _section(7, _vec(exports))
        + _section(10, _vec([abi, manifest_ptr, alloc, no_op]))
        + _section(11, _vec([b"\x00\x41" + _sleb(base) + b"\x0b" + _uleb(len(data)) + data]))
    )


def manifest(action: str, label: str) -> dict[str, Any]:
    return {
        "id": PLUGIN_PREFIX + action,
        "name": label,
        "version": VERSION,
        "kind": "filter",
        "author": "OpenFill",
        "description": "Opens OpenFill (runs next to PhotoCraft). Changes nothing by itself.",
        "params": {},
        "overlap": 0,
        "area": "content",
    }


def install(client: ControlClient) -> list[str]:
    """Add OpenFill's entries to Filter > Plug-ins (plug-ins last as long as the PhotoCraft
    process). The labels installed; empty when this PhotoCraft has no plug-ins."""
    for action in OLD_ACTIONS:
        try:
            client.run("plugin.remove", id=PLUGIN_PREFIX + action)
        except ControlError:
            pass  # not installed
    done = []
    for action, label in ENTRIES:
        data = base64.b64encode(plugin_module(manifest(action, label))).decode("ascii")
        try:
            client.run("plugin.install", data=data, replace=True)
            done.append(label)
        except ControlError:
            break
    return done


# Watching -----------------------------------------------------------------------------

LABEL_ACTION = {label: action for action, label in ENTRIES}
SCRATCH_PREFIX = "OpenFill "  # the names of the documents the bridge opens for a moment


@dataclass
class DocInfo:
    """What the companion shows about PhotoCraft (refreshed by ``Watcher``)."""

    connected: bool = False
    error: str = ""
    name: str = ""
    index: int | None = None
    width: int = 0
    height: int = 0
    mode: str = ""
    selection: list[int] | None = None
    active_layer_kind: str = ""
    documents: int = 0
    updated: float = 0.0
    waiting: bool = False  # PhotoCraft has stopped answering (window hidden on Wayland)

    def snapshot(self) -> dict[str, Any]:
        if not self.connected:
            return {"connected": False, "error": self.error}
        if not self.name:
            return {"connected": True, "document": None, "waiting": self.waiting}
        return {
            "connected": True,
            "waiting": self.waiting,
            "document": {"name": self.name, "width": self.width, "height": self.height, "mode": self.mode},
            "selection": self.selection,
        }


@dataclass
class Watcher:
    """Polls PhotoCraft: keeps ``info`` current and reports picks of OpenFill's entries."""

    client: ControlClient
    on_pick: Callable[[str], None]
    interval: float = 0.4
    info: DocInfo = field(default_factory=DocInfo)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _seen: dict[tuple[int, str], int] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        threading.Thread(target=self._loop, name="openfill-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.info.snapshot()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as err:  # noqa: BLE001 - keep watching
                with self.lock:
                    self.info.connected, self.info.error = False, str(err)
            self._stop.wait(self.interval)

    def poll(self) -> str | None:
        """One look at PhotoCraft; the action picked from the menu since the last look, if any."""
        session = self.client.call("ui.inspect").get("session") or {}
        docs = session.get("documents") or []
        active = next((d for d in docs if d.get("index") == session.get("active")), None)
        if active is None:
            with self.lock:
                self.info = DocInfo(connected=True, documents=len(docs), updated=time.time(), waiting=self.info.waiting)
            return None
        if str(active.get("name") or "").startswith(SCRATCH_PREFIX):
            return None  # OpenFill's own short-lived document: keep showing the user's
        key = (int(active["index"]), str(active.get("name")))
        revision = int(active.get("revision") or 0)
        if self._seen.get(key) == revision and self.info.name == key[1]:
            return None
        d = self.client.run("document.inspect") or {}
        history = list(d.get("history") or [])
        picked = None
        if history and history[-1] in LABEL_ACTION:
            picked = LABEL_ACTION[history[-1]]
            try:  # the entry changed nothing: take its step out of the history (and Redo)
                self.client.run("history.deleteState", state=len(history) - 1)
                d = self.client.run("document.inspect") or d
            except ControlError:
                pass
        bounds = d.get("selectionBounds") if d.get("hasSelection") else None
        layer_kind = ""
        for row in d.get("layers") or []:
            if row.get("id") == d.get("activeLayer"):
                layer_kind = str(row.get("kind") or "")
        with self.lock:
            self.info = DocInfo(
                connected=True,
                name=key[1],
                index=key[0],
                width=int(d.get("width") or 0),
                height=int(d.get("height") or 0),
                mode=str(d.get("mode") or ""),
                selection=[int(v) for v in bounds] if bounds else None,
                active_layer_kind=layer_kind,
                documents=len(docs),
                updated=time.time(),
                waiting=self.info.waiting,
            )
        self._seen[key] = int(d.get("revision") or revision) if picked else revision
        if picked:
            self.on_pick(picked)
        return picked

    def set_waiting(self, waiting: bool) -> None:
        with self.lock:
            self.info.waiting = waiting
