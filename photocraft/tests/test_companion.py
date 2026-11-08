# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PhotoCraft companion: control client, bridge and page API, without PhotoCraft."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "photocraft"), str(ROOT / "gimp-openfill")]

from openfill_photocraft import __main__ as launch  # noqa: E402
from openfill_photocraft import control  # noqa: E402
from openfill_photocraft.bridge import BridgeError, PhotoCraftBridge  # noqa: E402
from PIL import Image  # noqa: E402

from openfill.geometry import Rect  # noqa: E402


def read_psd(data: bytes) -> tuple[tuple[int, int], list[dict]]:
    """The canvas size and the layers (top first; groups with ``children``) of a PSD that
    ``openfill_photocraft.psd`` wrote: what PhotoCraft reads from it."""
    import struct

    assert data[:4] == b"8BPS"
    height, width = struct.unpack(">II", data[14:22])
    pos = 26
    for _ in range(2):  # colour mode data, image resources
        pos += 4 + struct.unpack(">I", data[pos : pos + 4])[0]
    pos += 8  # section length, layer info length
    (count,) = struct.unpack(">h", data[pos : pos + 2])
    pos += 2
    records = []
    for _ in range(abs(count)):
        top, left, bottom, right = struct.unpack(">4i", data[pos : pos + 16])
        (channels,) = struct.unpack(">H", data[pos + 16 : pos + 18])
        pos += 18
        lengths = []
        for _ in range(channels):
            cid, length = struct.unpack(">hI", data[pos : pos + 6])
            lengths.append((cid, length))
            pos += 6
        flags = data[pos + 10]
        (extra,) = struct.unpack(">I", data[pos + 12 : pos + 16])
        body, pos = data[pos + 16 : pos + 16 + extra], pos + 16 + extra
        (mask_len,) = struct.unpack(">I", body[:4])
        mask = struct.unpack(">4i", body[4:20]) if mask_len else None
        at = 4 + mask_len
        at += 4 + struct.unpack(">I", body[at : at + 4])[0]
        name_len = body[at]
        name = body[at + 1 : at + 1 + name_len].decode("latin-1")
        divider = 0
        if b"8BIMluni" in body:
            i = body.index(b"8BIMluni") + 12
            (n,) = struct.unpack(">I", body[i : i + 4])
            name = body[i + 4 : i + 4 + 2 * n].decode("utf-16-be")
        if b"8BIMlsct" in body:
            i = body.index(b"8BIMlsct") + 12
            (divider,) = struct.unpack(">I", body[i : i + 4])
        records.append(
            {
                "rect": (left, top, right - left, bottom - top),
                "visible": not flags & 2,
                "name": name,
                "mask": None if mask is None else (mask[1], mask[0], mask[3] - mask[1], mask[2] - mask[0]),
                "divider": divider,
                "lengths": lengths,
            }
        )
    planes = {}
    for index, record in enumerate(records):
        for cid, length in record["lengths"]:
            planes[(index, cid)] = data[pos + 2 : pos + length]
            pos += length
    stack: list[list[dict]] = [[]]
    for index, record in enumerate(records):  # bottom first
        if record["divider"] == 3:
            stack.append([])
            continue
        row = {"name": record["name"], "visible": record["visible"], "rect": record["rect"]}
        if record["divider"] in (1, 2):
            row["kind"], row["children"] = "Group", list(reversed(stack.pop()))
        else:
            row["alpha"] = planes[(index, -1)]
            if record["mask"] is not None:
                row["mask"], row["mask_rect"], row["mask_pixels"] = True, record["mask"], planes[(index, -2)]
        stack[-1].append(row)
    return (width, height), list(reversed(stack[0]))


class FakeServer:
    """A loopback server speaking the control protocol's JSON lines."""

    def __init__(self, token: str, handler) -> None:
        self.token, self.handler = token, handler
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")
        authed = False
        for line in f:
            msg = json.loads(line)
            if not authed:
                ok = msg.get("method") == "auth" and msg["params"].get("token") == self.token
                reply = {"id": msg["id"], "ok": ok, **({} if ok else {"error": "bad token"})}
                authed = ok
            else:
                try:
                    reply = {"id": msg["id"], "ok": True, "result": self.handler(msg["method"], msg["params"])}
                except Exception as err:  # noqa: BLE001
                    reply = {"id": msg["id"], "ok": False, "error": str(err)}
            f.write((json.dumps(reply) + "\n").encode())
            f.flush()

    def close(self) -> None:
        self.sock.close()


class ControlClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = Path(self.tmp.name) / "token"
        self.token.write_text("a" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def test_auth_call_and_error(self):
        def handler(method, params):
            if method == "engine.execute" and params["command"] == "boom":
                raise RuntimeError("command `boom` is not available")
            return {"method": method, "params": params}

        server = FakeServer("a" * 64, handler)
        client = control.ControlClient(server.port, self.token)
        self.assertEqual(client.call("ui.inspect")["method"], "ui.inspect")
        self.assertEqual(client.run("select.all", x=1)["params"], {"command": "select.all", "params": {"x": 1}})
        with self.assertRaises(control.ControlError) as cm:
            client.run("boom")
        self.assertIn("not available", str(cm.exception))
        server.close()

    def test_wrong_token_and_no_server(self):
        server = FakeServer("b" * 64, lambda m, p: None)
        with self.assertRaises(control.ControlError) as cm:
            control.ControlClient(server.port, self.token).call("ui.inspect")
        self.assertIn("refused", str(cm.exception))
        server.close()
        with self.assertRaises(control.ControlError):
            control.ControlClient(server.port, self.token, timeout=1).call("ui.inspect")

    def test_requests_photocraft_did_not_run_are_sent_again(self):
        tries = []

        def handler(method, params):
            tries.append(method)
            if len(tries) < 3:
                raise RuntimeError("timeout: not run - the request was still queued when its 60s deadline passed")
            return "done"

        server = FakeServer("a" * 64, handler)
        client = control.ControlClient(server.port, self.token)
        waits = []
        client.on_wait = waits.append
        self.assertEqual(client.call("ui.inspect"), "done")
        self.assertEqual((len(tries), waits), (3, [True, False]))
        server.close()

    def test_reconnects_after_idle(self):
        server = FakeServer("a" * 64, lambda m, p: 1)
        client = control.ControlClient(server.port, self.token)
        client.call("ui.inspect")
        client._last -= control.IDLE_RECONNECT_S + 1  # as if idle for longer than PhotoCraft allows
        client.call("ui.inspect")
        self.assertEqual(server.connections, 2)
        server.close()


class FakePhotoCraft:
    """Enough of PhotoCraft for the bridge: documents with layers, a selection, history,
    file round trips through the exchange folder, and copies between documents."""

    EDITS = {
        "layer.copyToDocument": "Duplicate Layer",
        "image.crop": "Crop",
        "layer.setProps": "Layer Properties",
        "layer.delete": "Delete Layer",
        "layer.new.group": "New Group",
        "layer.moveTo": "Move Layer",
        "layer.layerMask.revealSelection": "Add Layer Mask",
    }

    NEEDS_SELECTION = ("select.modify.expand", "select.modify.feather", "layer.layerMask.revealSelection")

    def __init__(self, exchange: Path, size=(64, 48), version: str | None = None, actions: bool = True) -> None:
        self.exchange = exchange
        self.actions = {} if actions else None  # recorded actions; None: a PhotoCraft without them
        self.version = version  # what ui.inspect reports; None as PhotoCraft before 0.16
        self.views: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 10
        self.docs = [self._doc("photo.png", size, [{"id": 2, "name": "Background", "visible": True}])]
        self.docs[0]["selection"] = [8, 6, 20, 10]
        self.active = 0
        self.opened_psds: list[list[dict]] = []  # the layers of every PSD OpenFill opened
        # The user clicking to deselect in PhotoCraft while OpenFill works: before each of the
        # next ``deselects`` selection-using commands, the active document loses its selection.
        self.deselects = 0

    def _doc(self, name, size, layers):
        return {
            "name": name,
            "size": size,
            "layers": layers,
            "history": ["Open"],
            "revision": 1,
            "selection": None,
            "channels": [],
        }

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    @property
    def doc(self):
        return self.docs[self.active]

    def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "ui.set":
            self.views.append({"document": self.active, **params})
            return None
        if method == "ui.inspect":
            return {
                "updates": {"currentVersion": self.version} if self.version else None,
                "views": [{"zoom": 0.5 + i, "center": [10.0 * i, 20.0]} for i in range(len(self.docs))],
                "session": {
                    "active": self.active,
                    "documents": [
                        {"index": i, "name": d["name"], "revision": d["revision"]} for i, d in enumerate(self.docs)
                    ],
                },
            }
        if method == "app.save" and params["path"].endswith(".tif"):
            # a flat TIFF: red, green, blue and the document's alpha channel (as PhotoCraft 0.18 on)
            if not self.doc["channels"]:  # the colours alone
                Image.new("RGB", self.doc["size"], self.doc.get("colour", (200, 200, 200))).save(
                    self.exchange / params["path"]
                )
                return {"path": params["path"]}
            colour = self.doc.get("colour", (200, 200, 200))
            im = Image.new("RGBA", self.doc["size"], (*colour, 0))
            for channel in self.doc["channels"]:
                x, y, w, h = channel["rect"]
                im.paste((*colour, 255), (x, y, x + w, y + h))
            im.save(self.exchange / params["path"], compression="raw")
            return {"path": params["path"]}
        if method == "app.save":
            kind = "L" if self.doc["name"] == "OpenFill mask" else "RGBA"
            im = Image.new(
                kind, self.doc["size"], 0 if kind == "L" else (*self.doc.get("colour", (200, 200, 200)), 255)
            )
            if kind == "L":
                x, y, w, h = self.doc["selection_source"]
                im.paste(255, (x, y, x + w, y + h))
            im.save(self.exchange / params["path"])
            return {"path": params["path"]}
        if method == "app.open" and params["path"].endswith(".psd"):
            path = self.exchange / params["path"]
            size, rows = read_psd((path if path.exists() else self.exchange.parent / params["path"]).read_bytes())
            self.opened_psds.append(rows)

            def ids(rows):  # the document's rows: what layer.list shows, without the pixels
                out = []
                for row in rows:
                    kept = {k: v for k, v in row.items() if not isinstance(v, bytes) and k != "children"}
                    kept["id"] = self._new_id()
                    if "children" in row:
                        kept["children"] = ids(row["children"])
                    out.append(kept)
                return out

            self.docs.append(self._doc(Path(params["path"]).name, size, ids(rows)))
            self.active = len(self.docs) - 1
            return {"name": Path(params["path"]).name}
        if method == "app.open":
            with Image.open(
                self.exchange.parent / params["path"]
                if not (self.exchange / params["path"]).exists()
                else self.exchange / params["path"]
            ) as im:
                size = im.size
            self.docs.append(
                self._doc(Path(params["path"]).name, size, [{"id": self._new_id(), "name": "Layer 0", "visible": True}])
            )
            self.active = len(self.docs) - 1
            return {"name": Path(params["path"]).name}
        if method == "engine.execute":
            return self.run(params["command"], **params["params"])
        raise AssertionError(method)

    def _edit(self, doc, command) -> None:
        if command in self.EDITS:
            doc["redo"] = []
            doc["history"].append(self.EDITS[command])
            doc["revision"] += 1

    def run(self, command: str, **params):
        self.calls.append((command, params))
        doc = self.doc
        if command == "document.inspect" and "document" in params:  # it takes a document; most commands don't
            doc = self.docs[params["document"]]
        if command.startswith("actions."):
            if self.actions is None:
                raise control.ControlError(f"engine.execute: unknown command `{command}`")
            if command == "actions.list":
                return {"actions": [{"name": n} for n in self.actions], "recording": None}
            if command == "actions.record":
                self.actions[params["name"]] = []
                return {"action": params["name"]}
            if command == "actions.stop":  # what was done while recording (``recorded``), as PhotoCraft does
                steps = getattr(self, "recorded", [])
                self.actions[next(reversed(self.actions))] = list(steps)
                self.recorded = []
                return {"steps": len(steps)}
            if command == "actions.insert":
                self.actions[params["action"]].append((params["id"], params.get("params") or {}))
                return {}
            if command == "actions.delete":
                if "step" in params:
                    del self.actions[params["action"]][params["step"]]
                    return {"action": params["action"]}
                del self.actions[params["action"]]
                return {"deleted": params["action"]}
            if command == "actions.play":  # in one go: nothing (no click) comes between its steps
                for step, step_params in self.actions[params["action"]]:
                    try:
                        FakePhotoCraft.run(self, step, **step_params)
                    except control.ControlError as err:
                        return {"ran": 0, "failed": {"id": step, "error": str(err)}}
                return {"ran": len(self.actions[params["action"]])}
        if command in self.NEEDS_SELECTION:
            if self.deselects > 0:
                self.deselects -= 1
                doc["selection"] = None
            if not doc["selection"]:  # as PhotoCraft says it
                raise control.ControlError(f"engine.execute: {command} is not available right now: no selection")
        if command in self.EDITS:  # what each history step started from, for history.deleteState
            edited = self.docs[params["document"]] if command == "layer.copyToDocument" else doc
            edited.setdefault("before", {})[len(edited["history"])] = json.loads(json.dumps(edited["layers"]))
            edited.setdefault("sizes", {})[len(edited["history"])] = edited["size"]
        if command == "history.deleteState":
            doc["layers"] = doc["before"][params["state"]]
            doc["size"] = doc.setdefault("sizes", {}).get(params["state"], doc["size"])
            del doc["history"][params["state"] :]
            doc["revision"] += 1
            return {"states": len(doc["history"])}
        if command == "history.selectState":  # move to that state; the later ones are redo states
            k = int(params["state"])
            full = doc["history"] + doc.get("redo", [])
            saved = doc.setdefault("saved", {})
            saved[len(doc["history"]) - 1] = (json.loads(json.dumps(doc["layers"])), doc["size"])
            if k < len(doc["history"]):
                layers = doc["before"][k + 1]
                size = doc.setdefault("sizes", {}).get(k + 1, doc["size"])
            else:
                layers, size = saved[k]
            doc["layers"], doc["size"] = json.loads(json.dumps(layers)), size
            doc["history"], doc["redo"] = full[: k + 1], full[k + 1 :]
            doc["revision"] += 1
            return {"state": k, "changed": True}
        if command == "history.newSnapshot":
            snaps = doc.setdefault("snapshots", {})
            snaps[len(snaps) + 1] = (params["name"], json.loads(json.dumps(doc["layers"])), doc["size"])
            return {"snapshot": len(snaps), "name": params["name"]}
        if command == "history.selectSnapshot":
            name, saved, size = doc["snapshots"][params["snapshot"]]
            doc.setdefault("before", {})[len(doc["history"])] = json.loads(json.dumps(doc["layers"]))
            doc.setdefault("sizes", {})[len(doc["history"])] = doc["size"]
            doc["layers"], doc["size"] = json.loads(json.dumps(saved)), size
            doc["history"].append(name)
            doc["revision"] += 1
            return {"snapshot": params["snapshot"], "name": name}
        if command == "history.deleteSnapshot":
            del doc["snapshots"][params["snapshot"]]
            return {}
        if command == "document.inspect":
            return {
                "width": doc["size"][0],
                "height": doc["size"][1],
                "depth": doc.get("depth", 8),
                "hasSelection": bool(doc["selection"]),
                "selectionBounds": doc["selection"],
                "history": list(doc["history"]),
                "snapshots": [{"id": k, "name": v[0]} for k, v in doc.get("snapshots", {}).items()],
                "revision": doc["revision"],
                "activeLayer": doc["layers"][0]["id"] if doc["layers"] else None,
                "layers": doc["layers"],
                "channels": {"alpha": doc["channels"]},
            }
        if command == "image.duplicate":
            copy = self._doc(params.get("name", "copy"), doc["size"], [{"id": self._new_id(), "name": "x"}])
            copy["selection"] = doc["selection"]  # a duplicate keeps the selection
            self.docs.append(copy)
            self.active = len(self.docs) - 1
            return {}
        if command == "select.saveSelection":
            if not doc["selection"]:
                raise control.ControlError(f"engine.execute: {command} is not available right now: no selection")
            target = self.docs[params.get("document", self.active)]
            target["channels"].append({"name": params["name"], "rect": doc["selection"]})
            return {"channel": len(target["channels"]) - 1}
        if command == "channel.duplicate":
            rect = next(c["rect"] for c in doc["channels"] if c["name"] == params["channel"])
            new = self._doc("OpenFill mask", doc["size"], [{"id": self._new_id(), "name": "mask"}])
            new["selection_source"] = rect
            self.docs.append(new)
            self.active = len(self.docs) - 1
            return {}
        if command == "file.close":
            self.docs.pop(params.get("document", self.active))
            self.active = 0
            return None
        if command == "document.activate":
            self.active = params["document"]
            return None
        if command == "layer.list":
            target = self.docs[params["document"]] if "document" in params else doc
            rows = []
            for layer in target["layers"]:
                rows.append({**layer, "depth": 0})
                if params.get("flat"):
                    rows += [{**c, "depth": 1, "parent": layer["id"]} for c in layer.get("children", [])]
            return {"activeLayer": rows[0]["id"] if rows else None, "layers": rows}
        if command == "layer.copyToDocument":
            src, dest = self.docs[params.get("source", self.active)], self.docs[params["document"]]
            ids = []
            # As PhotoCraft places a copy: above the selected layer, into a selected (open)
            # group as its top child, on top of the stack with no layer selected.
            siblings, at = dest["layers"], 0
            anchor = dest.get("selected")
            for row in self._all(dest):
                if row["id"] == anchor:
                    if "children" in row and row.get("expanded", True):
                        siblings, at = row["children"], 0
                    else:
                        siblings = next(
                            (g["children"] for g in dest["layers"] if row in g.get("children", [])), dest["layers"]
                        )
                        at = siblings.index(row)
            for layer_id in params["layers"]:
                layer = next(row for row in src["layers"] if row["id"] == layer_id)
                copy = json.loads(json.dumps(layer))
                copy["id"] = self._new_id()
                for child in copy.get("children", []):
                    child["id"] = self._new_id()
                siblings.insert(at, copy)
                ids.append(copy["id"])
            dest["selected"] = ids[-1] if ids else anchor
            self.active = params["document"]
            self._edit(dest, command)
            return {"document": params["document"], "layers": ids}
        if command == "layer.select":
            doc["selected"] = params.get("layer")
        elif command == "layer.setExpanded":  # a view setting, not a history step
            next(r for r in doc["layers"] if r["id"] == params["layer"])["expanded"] = params["expanded"]
        elif command == "select.deselectLayers":
            doc["selected"] = None
        elif command == "select.loadSelection":
            doc["selection"] = [0, 0, *doc["size"]]
        elif command == "layer.layerMask.revealSelection":
            doc["selection"] = None  # adding a mask uses the selection up, as in PhotoCraft
            next(row for row in doc["layers"] if row["id"] == params["layer"])["mask"] = True
        elif command == "layer.setProps":
            row = next(r for r in self._all(doc) if r["id"] == params["layer"])
            row.update({k: v for k, v in params.items() if k in ("name", "visible")})
        elif command == "layer.new.group":
            group = {"id": self._new_id(), "name": params.get("name"), "kind": "Group", "visible": True, "children": []}
            doc["layers"].insert(0, group)
            self._edit(doc, command)
            return {"layer": group["id"]}
        elif command == "layer.moveTo":
            group = next(r for r in doc["layers"] if r["id"] == params["target"])
            row = next(r for r in self._all(doc) if r["id"] == params["layer"])
            doc["layers"] = [r for r in doc["layers"] if r is not row]
            for g in doc["layers"]:
                g["children"] = [c for c in g.get("children", []) if c is not row] if "children" in g else None
                if g["children"] is None:
                    del g["children"]
            group["children"].insert(0, row)
        elif command == "layer.delete":
            doc["layers"] = [r for r in doc["layers"] if r["id"] != params["layer"]]
            for group in doc["layers"]:
                if "children" in group:
                    group["children"] = [c for c in group["children"] if c["id"] != params["layer"]]
        elif command == "image.crop":
            doc["size"] = (params["width"], params["height"])
        self._edit(doc, command)
        return {"selected": True} if command.startswith("select.") else None

    def _all(self, doc):
        for row in doc["layers"]:
            yield row
            yield from row.get("children", [])

    def commands(self) -> list[str]:
        return [m if m != "engine.execute" else p["command"] for m, p in self.calls if m != "engine.execute"]


def png_bytes(color, size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exchange = Path(self.tmp.name) / "exchange"
        self.exchange.mkdir()
        self.pc = FakePhotoCraft(self.exchange)
        self.bridge = PhotoCraftBridge(self.pc, self.exchange, Path(self.tmp.name))
        self.image = self.bridge.track(self.bridge.active_image())
        self.target = self.pc.docs[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_reading_leaves_the_document_unchanged(self):
        self.assertEqual(self.bridge.selection_rect(self.image), Rect(8, 6, 20, 10))
        _png, raw = self.bridge.export_selection_mask(self.image, Rect(0, 0, 32, 24))
        self.assertEqual(len(raw), 32 * 24)
        self.assertEqual(raw[6 * 32 + 8], 255)  # inside the selection
        self.assertEqual(raw[0], 0)
        _png, flat = self.bridge.export_region(self.image, Rect(48, 0, 32, 24), transparent_outside=True)
        self.assertEqual(set(flat[3::4]), {255})  # past the canvas: flattened over the mean colour
        self.assertEqual(self.target["history"], ["Open"])
        self.assertEqual(len(self.pc.docs), 1)  # the duplicates are closed
        self.assertEqual(list(self.exchange.iterdir()), [])
        # one merged duplicate serves both reads
        self.assertEqual(self.pc.commands().count("image.duplicate"), 1)

    def test_variations_reach_the_document_in_one_step(self):
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        layers = self.bridge.insert_results(
            self.image,
            [png_bytes("red"), png_bytes("blue"), png_bytes("green")],
            Rect(0, 0, 64, 48),
            "Generative Fill",
            6.0,
            None,
            grow=4,
        )
        self.assertTrue(all(layer.id is None for layer in layers))  # nothing in PhotoCraft yet
        self.assertEqual(self.target["history"], ["Open"])
        self.bridge.finish(self.image)
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])
        group = self.target["layers"][0]
        self.assertEqual(group["name"], "Generative Fill")
        self.assertEqual(
            [c["name"] for c in group["children"]],
            ["Generative Fill (1)", "Generative Fill (2)", "Generative Fill (3)"],
        )
        self.assertEqual([c["visible"] for c in group["children"]], [True, False, False])
        self.assertTrue(all(c.get("mask") for c in group["children"]))
        self.assertEqual([layer.id for layer in layers], [c["id"] for c in group["children"]])
        self.assertEqual(len(self.pc.docs), 1)  # scratch documents closed
        self.assertEqual(self.target["selection"], [8, 6, 20, 10])  # the user's selection is untouched
        self.assertNotIn("edit.copy", self.pc.commands())  # the clipboard is never used
        # One scratch document holds all three, masks included: nothing is built inside it.
        commands = self.pc.commands()
        self.assertEqual(len(self.pc.opened_psds), 1)
        for command in ("select.loadSelection", "select.modify.expand", "layer.layerMask.revealSelection"):
            self.assertNotIn(command, commands)
        # the document is the active one again right after the open, before anything else
        opened = commands.index("app.open")
        self.assertEqual(self.pc.calls[opened + 1], ("document.activate", {"document": 0}))
        (group, backdrop) = self.pc.opened_psds[0]
        self.assertEqual(backdrop["name"], "OpenFill backdrop")  # looks like the document; never copied
        self.assertNotIn("OpenFill backdrop", [r["name"] for r in self.target["layers"]])
        first = group["children"][0]
        # the selection (8, 6, 20 x 10) grown by 4 and feathered: wider than it, opaque in the middle
        x, y, w, h = first["mask_rect"]
        self.assertTrue(x <= 4 and y <= 2 and x + w >= 32 and y + h >= 20, first["mask_rect"])
        self.assertGreater(first["mask_pixels"][(10 - y) * w + (17 - x)], 200)

    def test_keep_and_discard_leave_one_step_or_none(self):
        from types import SimpleNamespace

        from openfill_photocraft.service import JobState, Service

        def fill():
            base = self.bridge.history(self.image)
            self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
            layers = self.bridge.insert_results(
                self.image,
                [png_bytes("red"), png_bytes("blue"), png_bytes("green")],
                Rect(0, 0, 64, 48),
                "Fill",
                0.0,
                None,
            )
            self.bridge.finish(self.image)
            service = Service.__new__(Service)
            service.bridge, service.client, service.image, service.layers = self.bridge, self.pc, self.image, layers
            service.state = JobState()
            service.app = SimpleNamespace(log=SimpleNamespace(warning=lambda *a: None))
            service._collapsed = False
            service._one_step(self.image, base)
            return service, layers

        service, _layers = fill()
        service.show_variation(2)
        service.keep("selected")
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])  # one Undo takes it back
        top = self.target["layers"][0]
        self.assertEqual((top["name"], top["visible"], top.get("mask")), ("Fill", True, True))
        self.assertNotIn("children", top)  # the kept variation alone, no group
        self.assertEqual(len(self.target["layers"]), 2)
        self.assertEqual(service.layers[0].id, top["id"])

        self.pc.run("history.deleteState", state=1)
        service, _layers = fill()
        service.show_variation(1)
        service.keep("none")
        self.assertEqual(self.target["history"], ["Open"])  # Discard leaves no step
        self.assertEqual([r["name"] for r in self.target["layers"]], ["Background"])

        # something else changed after the result: nothing is taken back, the hidden ones go
        service, _layers = fill()
        self.pc.run("layer.setProps", layer=2, name="Renamed")
        service.keep("selected")
        self.assertEqual(self.target["history"][:3], ["Open", "Duplicate Layer", "Layer Properties"])
        self.assertEqual(len(self.target["layers"][0]["children"]), 1)

    def _fill_service(self, grow=4, feather=6.0):
        """A finished 3-variation fill (grow and feather: the masks use select.modify.*) and a
        Service around it, as after a real job."""
        from types import SimpleNamespace

        from openfill_photocraft.service import JobState, Service

        base = self.bridge.history(self.image)
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        layers = self.bridge.insert_results(
            self.image,
            [png_bytes("red"), png_bytes("blue"), png_bytes("green")],
            Rect(0, 0, 64, 48),
            "Fill",
            feather,
            None,
            grow=grow,
        )
        self.bridge.finish(self.image)
        service = Service.__new__(Service)
        service.bridge, service.client, service.image, service.layers = self.bridge, self.pc, self.image, layers
        service.state = JobState()
        service.state.variations = [layer.name for layer in layers]
        service.app = SimpleNamespace(log=SimpleNamespace(warning=lambda *a: None))
        service._collapsed = False
        service._one_step(self.image, base)
        return service, layers

    def test_a_deselect_click_during_keep_is_harmless(self):
        # Regression: a click to deselect while Keep ran made it fail with
        # "select.modify.feather is not available right now: no selection", and the three
        # variations vanished. Keep works from the job's stored selection, not the live one.
        service, layers = self._fill_service()
        service.show_variation(1)
        self.target["selection"] = None  # the user deselected in the document
        self.pc.deselects = 2  # and clicks land while Keep builds the mask
        service.keep("selected")
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])  # still one step
        top = self.target["layers"][0]
        self.assertEqual((top["name"], top["visible"], top.get("mask")), ("Fill", True, True))
        self.assertNotIn("children", top)
        self.assertEqual(service.layers, [layers[1]])
        self.assertEqual(service.state.variations, [])

    def test_a_keep_that_fails_never_deletes_the_variations(self):
        service, layers = self._fill_service()
        service.show_variation(2)
        before = json.loads(json.dumps(self.target["layers"]))
        history = list(self.target["history"])  # the job's step and the picker's two
        real_call = self.pc.call

        def call(method, **params):  # PhotoCraft can't open OpenFill's layers
            if method == "app.open":
                raise control.ControlError("app.open: no")
            return real_call(method, **params)

        self.pc.call = call
        with self.assertRaises(control.ControlError):
            service.keep("selected")
        self.assertEqual(self.target["history"], history)
        self.assertEqual(self.target["layers"], before)  # the variations are all still there
        self.assertEqual(len(self.pc.docs), 1)  # scratch documents closed
        self.assertEqual(service.layers, layers)  # and still in the picker
        self.assertEqual(len(service.state.variations), 3)
        self.pc.call = real_call
        service.keep("selected")  # Keep again works
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])
        self.assertEqual(self.target["layers"][0]["name"], "Fill")
        self.assertNotIn("children", self.target["layers"][0])

    def test_keep_puts_the_variations_back_when_the_copy_fails(self):
        service, layers = self._fill_service(grow=0, feather=0.0)
        service.show_variation(0)
        real_run, copies = self.pc.run, []

        def run(command, **params):
            if command == "layer.copyToDocument" and params.get("document") == 0:
                copies.append(params)
                if len(copies) == 1:
                    raise control.ControlError("engine.execute: layer.copyToDocument: lost")
            return real_run(command, **params)

        self.pc.run = run
        with self.assertRaises(control.ControlError):
            service.keep("selected")
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])  # put back, one step
        group = self.target["layers"][0]
        self.assertEqual([c["name"] for c in group["children"]], ["Fill (1)", "Fill (2)", "Fill (3)"])
        self.assertEqual([layer.id for layer in layers], [c["id"] for c in group["children"]])
        self.assertEqual(len(service.state.variations), 3)
        service.keep("selected")  # and Keep can be tried again
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])
        self.assertNotIn("children", self.target["layers"][0])

    def test_a_deselect_click_while_the_result_is_added(self):
        self.pc.deselects = 1
        self._fill_service()
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])
        self.assertEqual(len(self.target["layers"][0]["children"]), 3)
        self.assertTrue(all(c.get("mask") for c in self.target["layers"][0]["children"]))

    def test_a_job_result_is_not_lost_when_adding_it_fails_once(self):
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        self.bridge.insert_results(self.image, [png_bytes("red")], Rect(0, 0, 64, 48), "Fill", 6.0, None, grow=4)
        self.pc.deselects = 4  # a whole mask sequence lost: the first commit fails
        self.bridge.finish(self.image)  # the second one works
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])
        self.assertEqual(len(self.pc.docs), 1)

    def test_discard_and_keep_all_need_no_selection(self):
        service, _layers = self._fill_service()
        self.pc.deselects = 1000
        service.keep("all")
        self.assertEqual(len(self.target["layers"][0]["children"]), 3)
        self.pc.run("history.deleteState", state=1)
        self.pc.deselects = 0
        service, _layers = self._fill_service()
        self.pc.deselects = 1000
        service.keep("none")
        self.assertEqual(self.target["history"], ["Open"])

    def test_an_expand_is_one_step_and_keep_leaves_it_one(self):
        from types import SimpleNamespace

        from openfill_photocraft.service import JobState, Service

        service = Service.__new__(Service)
        service.bridge, service.client, service.image = self.bridge, self.pc, self.image
        service.state = JobState(title="Generative Expand")
        service.app = SimpleNamespace(log=SimpleNamespace(warning=lambda *a: None))
        service._collapsed = False
        base = self.bridge.history(self.image)
        self.bridge.resize_canvas(self.image, 84, 48, 10, 0)  # round 1: crop, then its layer
        self.bridge.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        self.bridge.resize_canvas(self.image, 104, 48, 10, 0)  # round 2: crop, then two variations
        layers = self.bridge.insert_results(
            self.image,
            [png_bytes("red", (104, 48)), png_bytes("blue", (104, 48))],
            Rect(0, 0, 104, 48),
            "E",
            0.0,
            None,
            False,
        )
        self.bridge.finish(self.image)
        self.assertGreater(len(self.target["history"]), 2)
        service._one_step(self.image, base)
        self.assertEqual(self.target["history"], ["Open", "Generative Expand"])
        self.assertEqual(self.target["size"], (104, 48))
        service.layers = layers
        service.show_variation(1)
        service.keep("selected")
        self.assertEqual(self.target["history"], ["Open", "Generative Expand"])
        self.assertEqual(self.target["size"], (104, 48))  # the canvas stays
        self.assertEqual(len(self.target["layers"][0]["children"]), 1)
        self.pc.run("history.deleteState", state=1)  # one Undo takes the whole expand back
        self.assertEqual((self.target["size"], len(self.target["layers"])), ((64, 48), 1))

    def test_explicit_mask_is_feathered_in_python(self):
        mask = bytes(255 if y > 24 else 0 for y in range(48) for _ in range(64))
        (layer,) = self.bridge.insert_results(
            self.image, [png_bytes("blue")], Rect(0, 0, 64, 48), "Generative Expand", 8.0, None, mask_pixels=mask
        )
        self.bridge.finish(self.image)
        self.assertNotIn("select.modify.feather", self.pc.commands())
        self.assertEqual(self.target["layers"][0]["id"], layer.id)
        self.assertTrue(self.target["layers"][0].get("mask"))

    def test_pending_pixels_change_without_photocraft_then_reorder(self):
        layers = self.bridge.insert_results(
            self.image, [png_bytes("red"), png_bytes("blue")], Rect(0, 0, 64, 48), "Fill", 2.0, None
        )
        before = len(self.pc.calls)
        self.bridge.set_layer_rgba(layers[1], bytes(64 * 48 * 4))
        ordered = self.bridge.reorder_variations(self.image, layers, [1, 0], "Fill")
        self.assertEqual(len(self.pc.calls), before)  # all in memory
        self.assertEqual([layer.name for layer in ordered], ["Fill (1)", "Fill (2)"])
        self.bridge.finish(self.image)
        children = self.target["layers"][0]["children"]
        self.assertEqual([c["id"] for c in children], [layer.id for layer in ordered])
        self.assertEqual([c["visible"] for c in children], [True, False])

    def test_replacing_a_committed_layer(self):
        (layer,) = self.bridge.insert_results(
            self.image, [png_bytes("red")], Rect(0, 0, 64, 48), "Fill", 0.0, None, use_selection_mask=False
        )
        self.bridge.finish(self.image)
        old = layer.id
        self.bridge.set_layer_rgba(layer, bytes(64 * 48 * 4))
        self.assertNotEqual(layer.id, old)
        self.assertNotIn(old, [r["id"] for r in self.target["layers"]])

    def test_resize_commits_first_and_is_an_extending_crop(self):
        self.bridge.insert_results(
            self.image, [png_bytes("red")], Rect(0, 0, 64, 48), "E", 0.0, None, use_selection_mask=False
        )
        self.bridge.resize_canvas(self.image, 100, 80, 10, 20)
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer"])  # the earlier round is in
        self.assertNotIn("image.crop", self.pc.commands())  # the canvas waits for its layers
        self.assertEqual((self.image.get_width(), self.image.get_height()), (100, 80))
        self.bridge.insert_results(
            self.image, [png_bytes("blue", (100, 80))], Rect(0, 0, 100, 80), "E", 0.0, None, False
        )
        self.bridge.finish(self.image)
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer", "Duplicate Layer", "Crop"])
        commands = self.pc.commands()
        last_copy = len(commands) - 1 - commands[::-1].index("layer.copyToDocument")
        self.assertGreater(commands.index("image.crop"), last_copy)  # the layers first, then the canvas
        copy = [p for c, p in self.pc.calls if c == "layer.copyToDocument"][-1]
        self.assertEqual(copy["offset"], [-10, -20])  # placed for the new canvas before it grows
        self.assertIn(("image.crop", {"x": -10, "y": -20, "width": 100, "height": 80}), self.pc.calls)
        self.assertEqual(self.target["size"], (100, 80))

    def test_a_failed_expand_leaves_the_canvas_as_it_was(self):
        self.bridge.resize_canvas(self.image, 100, 80, 10, 20)
        self.bridge.discard(self.image)
        self.bridge.finish(self.image)
        self.assertEqual((self.target["size"], self.target["history"]), ((64, 48), ["Open"]))
        self.assertNotIn("image.crop", self.pc.commands())

    def test_the_selection_comes_from_a_tiff_from_photocraft_0_18(self):
        pc = FakePhotoCraft(self.exchange, version="0.20.2")
        bridge = PhotoCraftBridge(pc, self.exchange, Path(self.tmp.name))
        image = bridge.track(bridge.active_image())
        _png, raw = bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
        self.assertEqual((raw[6 * 64 + 8], raw[15 * 64 + 28], raw[0], raw[15 * 64 + 27]), (255, 0, 0, 255))
        _png, flat = bridge.export_region(image, Rect(0, 0, 64, 48))
        self.assertEqual(flat[:4], bytes((200, 200, 200, 255)))  # the colours, opaque
        saves = [p["path"] for c, p in pc.calls if c == "app.save"]
        self.assertTrue(all(path.endswith(".tif") for path in saves), saves)  # no slow PNG
        commands = pc.commands()
        self.assertNotIn("channel.duplicate", commands)  # no black and white document
        self.assertEqual(commands.count("image.duplicate"), 1)
        self.assertEqual(len(pc.docs), 1)
        self.assertEqual(pc.docs[0]["history"], ["Open"])
        # the duplicate is shown where the document was, at its zoom
        self.assertEqual(pc.views, [{"document": 1, "zoom": 0.5, "center": [0.0, 20.0]}])
        self.assertEqual(list(self.exchange.iterdir()), [])

    def test_the_copy_takes_the_view_after_photocraft_fitted_it(self):
        pc = FakePhotoCraft(self.exchange, version="0.20.2")
        real_call, pending = pc.call, [2]

        def call(method, **params):  # a new document fits itself to the window on its first frames
            reply = real_call(method, **params)
            if method == "ui.inspect" and len(pc.docs) > 1 and pending[0]:
                pending[0] -= 1
                reply["views"][-1]["fit_pending"] = True
            return reply

        pc.call = call
        bridge = PhotoCraftBridge(pc, self.exchange, Path(self.tmp.name))
        image = bridge.track(bridge.active_image())
        bridge.export_region(image, Rect(0, 0, 64, 48))
        self.assertEqual(pending, [0])  # asked until the fit was done
        self.assertEqual(pc.views, [{"document": 1, "zoom": 0.5, "center": [0.0, 20.0]}])

    def test_the_version_photocraft_reports(self):
        for reported, expected in (("0.20.2", (0, 20, 2)), ("0.18.0-beta.1", (0, 18, 0)), (None, (0,))):
            pc = FakePhotoCraft(self.exchange, version=reported)
            self.assertEqual(PhotoCraftBridge(pc, self.exchange, Path(self.tmp.name)).photocraft_version(), expected)

    def test_a_tiff_without_the_selection_falls_back_to_a_mask_document(self):
        pc = FakePhotoCraft(self.exchange, version="0.18.0")
        real_call = pc.call

        def call(method, **params):  # a PhotoCraft that leaves the alpha channel out
            if method == "app.save" and params["path"].endswith(".tif"):
                Image.new("RGB", pc.doc["size"]).save(self.exchange / params["path"], compression="raw")
                return {"path": params["path"]}
            return real_call(method, **params)

        pc.call = call
        bridge = PhotoCraftBridge(pc, self.exchange, Path(self.tmp.name))
        image = bridge.track(bridge.active_image())
        _png, raw = bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
        self.assertEqual((raw[6 * 64 + 8], raw[0]), (255, 0))
        self.assertIn("channel.duplicate", pc.commands())
        self.assertEqual(len(pc.docs), 1)
        self.assertEqual(list(self.exchange.iterdir()), [])

    def _bridge(self, version=None):
        pc = FakePhotoCraft(self.exchange, version=version)
        bridge = PhotoCraftBridge(pc, self.exchange, Path(self.tmp.name))
        return pc, bridge, bridge.track(bridge.active_image())

    def test_a_tab_click_during_the_export_never_writes_into_the_users_document(self):
        # PhotoCraft 0.18 on: the user clicks their own tab while the copy is saved. The selection
        # goes into the copy by name, and every save checks the copy is the one in front.
        pc, bridge, image = self._bridge("0.20.2")
        real_call, fired = pc.call, []

        def call(method, **params):
            out = real_call(method, **params)
            if method == "app.save" and params["path"].endswith(".tif") and not fired:
                fired.append(params["path"])
                pc.active = 0  # the user's click on their own tab
            return out

        pc.call = call
        _png, raw = bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
        self.assertTrue(fired)
        self.assertEqual(pc.docs[0]["channels"], [])  # the user's document got nothing
        self.assertEqual(pc.docs[0]["history"], ["Open"])
        self.assertEqual((raw[6 * 64 + 8], raw[0]), (255, 0))
        self.assertEqual([d["name"] for d in pc.docs], ["photo.png"])
        self.assertEqual(list(self.exchange.iterdir()), [])

    def test_a_tab_click_right_after_the_duplicate_never_closes_the_users_document(self):
        # The copy is found by its own name, never as "the active document": a click on the
        # user's tab right after image.duplicate must not get the user's document saved or closed.
        for version in ("0.20.2", None):
            pc, bridge, image = self._bridge(version)
            real_run, real_call, saved = pc.run, pc.call, []

            def run(command, _pc=pc, _real=real_run, **params):
                out = _real(command, **params)
                if command == "image.duplicate":
                    _pc.active = 0  # the user's click on their own tab
                return out

            def call(method, _pc=pc, _real=real_call, _saved=saved, **params):
                if method == "app.save":
                    _saved.append(_pc.doc["name"])
                return _real(method, **params)

            pc.run, pc.call = run, call
            bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
            self.assertEqual([d["name"] for d in pc.docs], ["photo.png"], version)
            self.assertNotIn("photo.png", saved, version)
            self.assertEqual(pc.docs[0]["channels"], [], version)

    def test_two_tabs_with_the_same_name_after_a_tab_left_of_them_closes(self):
        # Tabs [x.png, photo.png (the job's), photo.png (another)]; x.png closes mid-job, so
        # the other photo.png takes the job's old index: the result still goes to the job's.
        pc = self.pc
        pc.docs.insert(0, pc._doc("x.png", (64, 48), [{"id": 90, "name": "X", "visible": True}]))
        pc.docs.append(pc._doc("photo.png", (64, 48), [{"id": 91, "name": "Other", "visible": True}]))
        pc.active = 1
        image = self.bridge.track(self.bridge.active_image())
        mine, other = pc.docs[1], pc.docs[2]
        self.bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
        self.bridge.insert_results(image, [png_bytes("red")], Rect(0, 0, 64, 48), "Fill", 0.0, None)
        pc.docs.pop(0)  # the user closes x.png while the job runs
        pc.active = 0
        self.bridge.finish(image)
        self.assertEqual(other["history"], ["Open"])
        self.assertEqual(mine["history"], ["Open", "Duplicate Layer"])

    def test_a_two_round_expand_that_fails_in_round_two_leaves_the_canvas(self):
        # Round 2 reads the document, so round 1 (its layer and the bigger canvas) is in it
        # already; a failure or Cancel in round 2 takes that back too.
        bridge, target = self.bridge, self.target
        bridge.export_region(self.image, Rect(-10, 0, 84, 48), transparent_outside=True)
        bridge.resize_canvas(self.image, 84, 48, 10, 0)
        bridge.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        bridge.export_region(self.image, Rect(-10, 0, 104, 48), transparent_outside=True)  # round 2 reads
        self.assertEqual(target["size"], (84, 48))
        bridge.discard(self.image)  # round 2 failed or was cancelled
        self.assertEqual((target["size"], target["history"]), ((64, 48), ["Open"]))
        self.assertEqual([r["name"] for r in target["layers"]], ["Background"])

    def test_a_failed_expand_keeps_what_the_user_did_meanwhile(self):
        bridge, target = self.bridge, self.target
        bridge.export_region(self.image, Rect(-10, 0, 84, 48), transparent_outside=True)
        bridge.resize_canvas(self.image, 84, 48, 10, 0)
        bridge.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        bridge.export_region(self.image, Rect(-10, 0, 104, 48), transparent_outside=True)
        self.pc.run("layer.setProps", layer=2, name="Renamed")  # the user, between the rounds
        history = list(target["history"])
        bridge.discard(self.image)
        self.assertEqual(target["history"], history)  # nothing of theirs is undone

    def test_a_finished_job_is_not_taken_back(self):
        self.bridge.resize_canvas(self.image, 84, 48, 10, 0)
        self.bridge.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        self.bridge.finish(self.image)
        self.bridge.discard(self.image)  # what Service._job's finally does after every job
        self.assertEqual(self.target["size"], (84, 48))
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer", "Crop"])

    def test_the_same_mask_is_made_once_for_all_variations(self):
        from openfill_photocraft import bridge as bridge_mod

        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        self.bridge.insert_results(
            self.image,
            [png_bytes("red"), png_bytes("blue"), png_bytes("green")],
            Rect(0, 0, 64, 48),
            "Fill",
            6.0,
            None,
            grow=4,
        )
        with mock.patch.object(bridge_mod, "_grow", wraps=bridge_mod._grow) as grow:
            self.bridge.finish(self.image)
        self.assertEqual(grow.call_count, 1)
        masks = [c["mask_pixels"] for c in self.pc.opened_psds[-1][0]["children"]]
        self.assertEqual(len(set(masks)), 1)

    def test_a_result_after_a_variations_group_lands_on_top_not_in_it(self):
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        self.bridge.insert_results(
            self.image, [png_bytes("red"), png_bytes("blue"), png_bytes("green")], Rect(0, 0, 64, 48), "Fill", 0.0, None
        )
        self.bridge.finish(self.image)
        group = self.target["layers"][0]
        self.assertEqual(len(group["children"]), 3)
        self.pc.run("layer.select", layer=group["children"][0]["id"])  # what the picker leaves
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        (layer,) = self.bridge.insert_results(self.image, [png_bytes("blue")], Rect(0, 0, 64, 48), "Next", 0.0, None)
        self.bridge.finish(self.image)
        self.assertEqual([r["name"] for r in self.target["layers"]], ["Next", "Fill", "Background"])
        self.assertEqual(self.target["layers"][0]["id"], layer.id)
        self.assertEqual(len(self.target["layers"][1]["children"]), 3)  # the group keeps its three
        self.assertTrue(self.target["layers"][1]["expanded"])  # closed for the copy only
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer", "Duplicate Layer"])

    def test_deep_documents_skip_the_tiff_reads(self):
        pc, bridge, image = self._bridge("0.20.2")
        pc.docs[0]["depth"] = 16
        _png, raw = bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
        self.assertEqual(raw[6 * 64 + 8], 255)
        self.assertFalse(any(p.get("path", "").endswith(".tif") for _c, p in pc.calls))

    # -- second review: the rollback never loses the user's history, and acts on the job's document only

    def _with(self, actions=True):
        """setUp's document again, on a PhotoCraft with or without actions."""
        self.pc = FakePhotoCraft(self.exchange, actions=actions)
        self.bridge = PhotoCraftBridge(self.pc, self.exchange, Path(self.tmp.name))
        self.image = self.bridge.track(self.bridge.active_image())
        self.target = self.pc.docs[0]

    def _other_doc(self, steps=3):
        """Another document of the user's, with ``steps`` history steps of its own."""
        other = self.pc._doc("other.png", (64, 48), [{"id": 70, "name": "Mine", "visible": True}])
        self.pc.docs.append(other)
        for _ in range(steps):
            other["before"] = other.get("before", {}) | {len(other["history"]): [dict(r) for r in other["layers"]]}
            other["history"].append("Layer Properties")
            other["revision"] += 1
        return other

    def _round_one_in(self):
        """A 2-round expand with round 1 in the document (round 2's read commits it)."""
        b = self.bridge
        b.export_region(self.image, Rect(-10, 0, 84, 48), transparent_outside=True)
        b.resize_canvas(self.image, 84, 48, 10, 0)
        b.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        b.export_region(self.image, Rect(-10, 0, 104, 48), transparent_outside=True)
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer", "Crop"])

    def _click_before(self, command, times=1000, where=1, then=None):
        """The user clicks tab ``where`` right before each ``command`` OpenFill sends (``times``)."""
        real_run, left = self.pc.run, [times]

        def run(cmd, **params):
            if cmd == command and left[0]:
                left[0] -= 1
                self.pc.active = where
                if then:
                    then()
            return real_run(cmd, **params)

        self.pc.run = run

    def test_the_rollback_is_one_request_no_click_can_split(self):
        # "Show the job's document" and the history move are played as one throwaway action.
        other = self._other_doc()
        self._round_one_in()
        self._click_before("actions.play")  # every click lands just before the play
        self.assertEqual(self.bridge.discard(self.image), "undone")
        self.assertEqual((self.target["size"], self.target["history"]), ((64, 48), ["Open"]))
        self.assertEqual(self.target["redo"], ["Duplicate Layer", "Crop"])  # moved, never deleted
        self.assertEqual(other["history"], ["Open"] + ["Layer Properties"] * 3)
        self.assertEqual(self.pc.actions, {})  # the throwaway action is gone
        self.assertNotIn("history.deleteState", self.pc.commands())

    def test_the_collapsed_step_is_selected_outside_an_action(self):
        # PhotoCraft 0.20 names an edit played from an action "Action: <name>": the one step a
        # job leaves is selected directly, so it carries the job's name.
        self._round_one_in()
        self.bridge.collapse(self.image, 1, "Generative Expand")
        self.assertEqual(self.target["history"], ["Open", "Generative Expand"])
        self.assertIn("history.selectSnapshot", self.pc.commands())
        self.assertNotIn("history.selectSnapshot", [p["id"] for c, p in self.pc.calls if c == "actions.insert"])

    def test_a_click_recorded_into_openfills_action_is_never_replayed(self):
        other = self._other_doc()
        self._round_one_in()
        self.pc.recorded = [("document.activate", {"document": 1}), ("layer.setProps", {"layer": 70, "name": "x"})]
        self.assertEqual(self.bridge.discard(self.image), "undone")
        self.assertEqual(other["history"], ["Open"] + ["Layer Properties"] * 3)  # nothing replayed on it
        self.assertEqual(self.pc.actions, {})

    def test_without_actions_a_rollback_that_lands_elsewhere_is_put_back(self):
        self._with(actions=False)
        other = self._other_doc()
        self._round_one_in()
        self._click_before("history.selectState")  # the user keeps clicking their other tab
        self.assertEqual(self.bridge.discard(self.image), "kept")  # said so, never "unchanged"
        self.assertEqual(other["history"], ["Open"] + ["Layer Properties"] * 3)  # put back each time
        self.assertNotIn("history.deleteState", self.pc.commands())

    def test_without_actions_one_click_during_the_rollback_is_put_right(self):
        self._with(actions=False)
        other = self._other_doc()
        self._round_one_in()
        self._click_before("history.selectState", times=1)
        self.assertEqual(self.bridge.discard(self.image), "undone")
        self.assertEqual(other["history"], ["Open"] + ["Layer Properties"] * 3)
        self.assertEqual((self.target["size"], self.target["history"]), ((64, 48), ["Open"]))

    def test_a_step_of_the_users_between_the_jobs_steps_is_never_taken_back(self):
        # The user adds a step while round 1's layer is copied in and before its crop: the
        # job's steps are no longer the last ones together, so nothing is taken back.
        self._click_before(
            "actions.play", times=1, where=0, then=lambda: self.pc.run("layer.setProps", layer=2, name="x")
        )
        b = self.bridge
        b.export_region(self.image, Rect(-10, 0, 84, 48), transparent_outside=True)
        b.resize_canvas(self.image, 84, 48, 10, 0)
        b.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        b.export_region(self.image, Rect(-10, 0, 104, 48), transparent_outside=True)
        history = list(self.target["history"])
        self.assertEqual(history, ["Open", "Duplicate Layer", "Layer Properties", "Crop"])
        self.assertEqual(b.discard(self.image), "kept")
        self.assertEqual(self.target["history"], history)

    def test_rollback_never_undoes_a_same_labelled_step_of_the_users(self):
        # The user undoes the job's Crop mid-job and crops the image themselves: the labels are
        # the same, the revision is not, so nothing is taken back.
        self._round_one_in()
        doc = self.target
        doc["history"].pop()  # Edit > Undo of the job's crop
        doc["size"] = doc["sizes"][2]
        doc["revision"] += 1
        self.pc.run("image.crop", x=0, y=0, width=80, height=40)  # their own crop
        self.assertEqual(self.bridge.discard(self.image), "kept")
        self.assertEqual(doc["size"], (80, 40))

    def test_rollback_never_deletes_a_step_made_after_the_check(self):
        # A step of the user's that lands between the checks and the rollback is not deleted:
        # it becomes a redo state with the job's, so Edit > Redo brings it back.
        for actions in (True, False):
            self._with(actions)
            self._round_one_in()
            command = "actions.play" if actions else "history.selectState"
            self._click_before(command, times=1, where=0, then=lambda: self.pc.run("layer.setProps", layer=2, name="x"))
            self.bridge.discard(self.image)
            self.assertIn("Layer Properties", self.target["history"] + self.target.get("redo", []), actions)

    def test_a_user_document_named_like_the_copy_is_never_closed(self):
        real_run = self.pc.run

        def run(command, **params):
            out = real_run(command, **params)
            if command == "image.duplicate":
                name = self.pc.docs[-1]["name"]
                theirs = self.pc._doc(name, (32, 32), [{"id": 80, "name": "their work", "visible": True}])
                self.pc.docs.append(theirs)
                self.pc.active = 1
            return out

        self.pc.run = run
        with self.assertRaises(BridgeError):  # two tabs with OpenFill's name: hands off both
            self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        self.assertIn("their work", [r["name"] for d in self.pc.docs for r in d["layers"]])

    def test_a_click_right_before_the_crop_never_crops_another_document(self):
        for actions in (True, False):
            self._with(actions)
            other = self._other_doc(0)
            self.bridge.export_region(self.image, Rect(-10, 0, 84, 48), transparent_outside=True)
            self.bridge.resize_canvas(self.image, 84, 48, 10, 0)
            self.bridge.insert_results(
                self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False
            )
            self._click_before("actions.play" if actions else "image.crop", times=1)
            self.bridge.finish(self.image)  # the crop lands on the job's document all the same
            self.assertEqual((other["size"], other["history"]), ((64, 48), ["Open"]), actions)
            self.assertEqual(self.target["size"], (84, 48), actions)

    def test_a_crop_that_keeps_landing_elsewhere_stops_and_leaves_that_image_as_it_was(self):
        self._with(actions=False)
        other = self._other_doc(0)
        self.bridge.export_region(self.image, Rect(-10, 0, 84, 48), transparent_outside=True)
        self.bridge.resize_canvas(self.image, 84, 48, 10, 0)
        self.bridge.insert_results(self.image, [png_bytes("red", (84, 48))], Rect(0, 0, 84, 48), "E", 0.0, None, False)
        self._click_before("image.crop")
        with self.assertRaises(BridgeError):  # it stops, and says why
            self.bridge.finish(self.image)
        self.assertEqual((other["size"], other["history"]), ((64, 48), ["Open"]))

    def test_a_picture_read_from_another_tab_is_noticed_and_read_again(self):
        # The user's other tab comes to the front just before the copy is saved, and the copy's
        # tab just after: the check around the save passes. The selection's file, which holds
        # the copy's own channel, shows the picture was another image's: it is read again.
        pc, bridge, image = self._bridge("0.20.2")
        other = pc._doc("other.png", (64, 48), [{"id": 70, "name": "Mine", "visible": True}])
        other["colour"] = (0, 128, 128)
        pc.docs.insert(1, other)
        real_call, flips = pc.call, [1]

        def call(method, **params):
            if method == "app.save" and "visible" in params["path"] and flips:
                flips.pop()
                copy = pc.active
                pc.active = 1  # their other image
                out = real_call(method, **params)
                pc.active = copy  # and the copy again before OpenFill looks
                return out
            return real_call(method, **params)

        pc.call = call
        _png, flat = bridge.export_region(image, Rect(0, 0, 64, 48))
        bridge.export_selection_mask(image, Rect(0, 0, 64, 48))
        self.assertEqual(flat[:3], bytes((200, 200, 200)))  # the job's picture, not theirs
        self.assertFalse(flips)

    def test_the_page_says_what_a_failed_rollback_left(self):
        from openfill_photocraft.service import JobState, Service

        service = Service.__new__(Service)
        service.state = JobState()
        for outcome, expected in (
            ("undone", "Cancelled - the image was left unchanged."),
            ("", "Cancelled - the image was left unchanged."),
        ):
            service.state.state, service.state.message = "cancelled", "Cancelled - the image was left unchanged."
            service._say_what_is_left(outcome)
            self.assertEqual(service.state.message, expected)
        service._say_what_is_left("kept")
        self.assertEqual(
            service.state.message,
            "Cancelled. What OpenFill had already added is still in the image: Edit > Undo takes it back.",
        )
        service.state.state, service.state.message = "error", "ComfyUI stopped."
        service._say_what_is_left("kept")
        self.assertTrue(service.state.message.startswith("ComfyUI stopped. What OpenFill had already added"))

    def test_a_copy_photocraft_could_not_close_is_closed_next_time(self):
        real_run, refused = self.pc.run, []

        def run(command, **params):
            if command == "file.close" and not refused:
                refused.append(1)
                raise control.ControlError("engine.execute: unavailable while a modal dialog is open")
            return real_run(command, **params)

        self.pc.run = run
        notes = []
        self.bridge.notify = notes.append
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        self.assertEqual(len(self.pc.docs), 2)  # the copy is still open (the dialog)
        self.assertTrue(notes and "temporary document" in notes[0])
        self.bridge.insert_results(self.image, [png_bytes("red")], Rect(0, 0, 64, 48), "Fill", 0.0, None)
        self.image._snapshot = None
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))  # the next read closes it
        self.assertEqual([d["name"] for d in self.pc.docs], ["photo.png"])

    def test_older_photocraft_never_tries_the_tiff(self):
        _png, raw = self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        self.assertEqual(raw[6 * 64 + 8], 255)
        self.assertFalse(any(p.get("path", "").endswith(".tif") for _c, p in self.pc.calls))

    def test_a_cancelled_job_leaves_nothing(self):
        self.bridge.insert_results(self.image, [png_bytes("red")], Rect(0, 0, 64, 48), "Fill", 0.0, None)
        self.bridge.discard(self.image)
        self.bridge.finish(self.image)
        self.assertEqual(self.target["history"], ["Open"])

    def test_paths_for_photocraft_use_forward_slashes(self):
        bridge = PhotoCraftBridge(self.pc, self.exchange, Path(self.tmp.name), read_root=Path(self.tmp.name))
        self.assertEqual(bridge._open_path("a b.png"), "exchange/a b.png")  # never a backslash on Windows

    def test_open_file_only_inside_the_read_root(self):
        bridge = PhotoCraftBridge(self.pc, self.exchange, Path(self.tmp.name), read_root=Path(self.tmp.name))
        picture = Path(self.tmp.name) / "holiday.png"
        Image.new("RGB", (8, 8)).save(picture)
        self.assertEqual(bridge.open_file(str(picture))["name"], "holiday.png")
        with self.assertRaises(BridgeError):
            bridge.open_file("/etc/hostname")


class PsdTests(unittest.TestCase):
    def test_layers_masks_names_and_groups_survive(self):
        from openfill_photocraft import psd

        red, blue = bytes((255, 0, 0, 255)) * 6, bytes((0, 0, 255, 128)) * 4
        data = psd.encode(
            10,
            8,
            [
                psd.PsdLayer(
                    "Fill",
                    children=[
                        psd.PsdLayer("Fill (1) é", 1, 2, 3, 2, red, True, (0, 1, 4, 3, bytes(range(12)))),
                        psd.PsdLayer("Fill (2)", -2, 0, 2, 2, blue, False),
                    ],
                ),
                psd.PsdLayer("Below", 0, 0, 2, 2, blue),
            ],
        )
        size, rows = read_psd(data)
        self.assertEqual(size, (10, 8))
        self.assertEqual([r["name"] for r in rows], ["Fill", "Below"])
        first, second = rows[0]["children"]
        self.assertEqual((first["name"], first["visible"], first["rect"]), ("Fill (1) é", True, (1, 2, 3, 2)))
        self.assertEqual((first["mask_rect"], first["mask_pixels"]), ((0, 1, 4, 3), bytes(range(12))))
        self.assertEqual(first["alpha"], bytes([255]) * 6)
        self.assertEqual((second["visible"], second["rect"], "mask" in second), (False, (-2, 0, 2, 2), False))
        self.assertEqual(len(data) % 2, 0)
        with self.assertRaises(ValueError):
            psd.encode(4, 4, [psd.PsdLayer("x", 0, 0, 2, 2, b"short")])

    def test_written_to_a_file_it_is_the_same_psd(self):
        from openfill_photocraft import psd

        layers = [psd.PsdLayer("A", 0, 0, 3, 2, bytes(range(24)), True, (0, 0, 3, 2, bytes(6)))]
        out = io.BytesIO()
        psd.write(out, 5, 4, layers)
        self.assertEqual(out.getvalue(), psd.encode(5, 4, layers))
        self.assertEqual(read_psd(out.getvalue())[1][0]["alpha"], bytes(range(3, 24, 4)))

    def test_grow_is_fast_on_a_big_picture(self):
        import time

        from openfill_photocraft.bridge import _grow

        big = Image.new("L", (6000, 4000), 0)
        big.paste(255, (400, 400, 5600, 3600))
        start = time.monotonic()
        grown = _grow(big, 48)
        self.assertLess(time.monotonic() - start, 5.0)  # about 0.5 s here; it was 16 s
        self.assertEqual(grown.getpixel((352, 2000)), 255)  # straight out: the full radius
        self.assertEqual(grown.getpixel((340, 2000)), 0)

    def test_grow_is_round_and_stays_inside_the_canvas(self):
        from openfill_photocraft.bridge import _grow

        mask = Image.new("L", (40, 40), 0)
        mask.paste(255, (18, 18, 22, 22))
        grown = _grow(mask, 6)
        self.assertEqual(grown.getbbox(), (12, 12, 28, 28))
        self.assertEqual(grown.getpixel((12, 20)), 255)  # straight out: the full radius
        self.assertEqual(grown.getpixel((12, 12)), 0)  # the corner is cut: not a square
        edge = Image.new("L", (10, 10), 0)
        edge.paste(255, (0, 0, 2, 2))
        self.assertEqual(_grow(edge, 3).getbbox(), (0, 0, 5, 5))


class MenuTests(unittest.TestCase):
    def test_plugin_module_shape(self):
        from openfill_photocraft import menu

        label = "OpenFill Generative Fill…"
        data = menu.plugin_module(menu.manifest("fill", label))
        self.assertEqual(data[:8], b"\x00asm\x01\x00\x00\x00")
        start = data.index(b'{"id"')
        found = json.loads(data[start : data.index(b"}", data.index(b'"area"')) + 1].decode("utf-8"))
        self.assertEqual(
            (found["id"], found["name"], found["kind"], found["params"]), ("app.openfill.fill", label, "filter", {})
        )
        for name in (b"pc_abi_version", b"pc_manifest", b"pc_alloc", b"pc_filter", b"memory"):
            self.assertIn(name, data)

    def test_leb128(self):
        from openfill_photocraft import menu

        self.assertEqual(menu._uleb(624485), b"\xe5\x8e\x26")
        self.assertEqual(menu._sleb(-123456), b"\xc0\xbb\x78")
        self.assertEqual(menu._sleb(-1), b"\x7f")
        self.assertEqual(menu._sleb(64), b"\xc0\x00")

    class _MenuPhotoCraft:
        """Two documents, the active one with OpenFill's menu step on top; PhotoCraft's history
        moves (selectState, with redo states), actions played in one go, and hooks to play the
        user: ``before_play`` runs right before an action plays."""

        def __init__(self):
            self.docs = [
                {"name": "a.png", "history": ["Open", "Rectangular Marquee", "OpenFill"], "redo": [], "revision": 3},
                {"name": "b.png", "history": ["Open", "New Layer", "New Layer"], "redo": [], "revision": 7},
            ]
            self.active, self.actions, self.recording, self.before_play = 0, {}, None, None
            self.commands = []

        def call(self, method, **params):
            return {
                "session": {
                    "active": self.active,
                    "documents": [
                        {"index": i, "name": d["name"], "revision": d["revision"]} for i, d in enumerate(self.docs)
                    ],
                }
            }

        def run(self, command, **params):
            self.commands.append(command)
            if command == "actions.list":
                return {"actions": [{"name": n} for n in self.actions], "recording": self.recording}
            if command == "actions.record":
                self.actions[params["name"]] = []
                return {}
            if command == "actions.stop":
                return {"steps": 0}
            if command == "actions.insert":
                self.actions[params["action"]].append((params["id"], params["params"]))
                return {}
            if command == "actions.delete":
                self.actions.pop(params["action"], None)
                return {}
            if command == "actions.play":
                if self.before_play:
                    self.before_play()
                for step, step_params in self.actions[params["action"]]:
                    self._engine(step, step_params)
                return {"ran": 2}
            if command == "history.deleteState":
                raise AssertionError("history.deleteState can't be undone: never used")
            return self._engine(command, params)

        def _engine(self, command, params):
            doc = (
                self.docs[params.get("document", self.active)]
                if command == "document.inspect"
                else self.docs[self.active]
            )
            if command == "document.activate":
                self.active = params["document"]
                return {}
            if command == "history.selectState":
                full = doc["history"] + doc["redo"]
                k = params["state"]
                doc["history"], doc["redo"] = full[: k + 1], full[k + 1 :]
                doc["revision"] += 1
                return {}
            if command == "document.inspect":
                return {
                    "history": list(doc["history"]),
                    "revision": doc["revision"],
                    "width": 10,
                    "height": 8,
                    "hasSelection": True,
                    "selectionBounds": [1, 2, 3, 4],
                    "layers": [],
                    "activeLayer": None,
                }
            raise AssertionError(command)

    def test_watcher_reports_a_pick_and_removes_its_step(self):
        from openfill_photocraft import menu

        pc = self._MenuPhotoCraft()
        picks = []
        watcher = menu.Watcher(pc, picks.append)
        self.assertEqual(watcher.poll(), "panel")
        self.assertEqual(picks, ["panel"])
        a, b = pc.docs
        self.assertEqual((a["history"], a["redo"]), (["Open", "Rectangular Marquee"], ["OpenFill"]))  # never deleted
        self.assertEqual(b["history"], ["Open", "New Layer", "New Layer"])
        self.assertEqual(pc.actions, {})  # the throwaway action is gone
        self.assertEqual(watcher.snapshot()["selection"], [1, 2, 3, 4])
        self.assertIsNone(watcher.poll())  # nothing new

    def test_a_tab_click_during_a_menu_pick_never_touches_another_document(self):
        from openfill_photocraft import menu

        # the click lands right before the one request: it shows the menu's document again and
        # moves its history; the other document is not touched
        pc = self._MenuPhotoCraft()
        pc.before_play = lambda: setattr(pc, "active", 1)
        self.assertEqual(menu.Watcher(pc, lambda _a: None).poll(), "panel")
        a, b = pc.docs
        self.assertEqual(a["history"], ["Open", "Rectangular Marquee"])
        self.assertEqual((b["history"], b["redo"], b["revision"]), (["Open", "New Layer", "New Layer"], [], 7))

        # the other tab is in front before the last look: the step stays where it is
        pc = self._MenuPhotoCraft()
        real_run = pc.run

        def run(command, **params):
            if command == "actions.insert" and params["id"] == "history.selectState":
                pc.active = 1
            return real_run(command, **params)

        pc.run = run
        self.assertEqual(menu.Watcher(pc, lambda _a: None).poll(), "panel")  # still reported
        a, b = pc.docs
        self.assertEqual(a["history"], ["Open", "Rectangular Marquee", "OpenFill"])
        self.assertEqual((b["history"], b["revision"]), (["Open", "New Layer", "New Layer"], 7))
        self.assertEqual(pc.actions, {})

    def test_a_menu_step_is_left_when_the_document_changed_or_an_action_records(self):
        from openfill_photocraft import menu

        pc = self._MenuPhotoCraft()
        real_run = pc.run

        def run(command, **params):  # the user's own step lands after the first look
            if command == "actions.record":
                pc.docs[0]["history"].append("Brush Tool")
                pc.docs[0]["revision"] += 1
            return real_run(command, **params)

        pc.run = run
        menu.Watcher(pc, lambda _a: None).poll()
        self.assertEqual(pc.docs[0]["history"], ["Open", "Rectangular Marquee", "OpenFill", "Brush Tool"])

        pc = self._MenuPhotoCraft()
        pc.recording = "Their action"
        menu.Watcher(pc, lambda _a: None).poll()
        self.assertEqual(pc.docs[0]["history"], ["Open", "Rectangular Marquee", "OpenFill"])
        self.assertNotIn("actions.record", pc.commands)


class InstallTests(unittest.TestCase):
    ORIGINAL = (
        "[Desktop Entry]\nType=Application\nName=PhotoCraft\nTryExec=photocraft\nExec=photocraft %F\n"
        "Icon=ai.storyteller.photocraft\nMimeType=image/png;\nActions=new;\n\n"
        "[Desktop Action new]\nName=New Window\nExec=photocraft --new-instance\n"
    )

    def test_checksums_never_the_signature(self):
        """PhotoCraft 0.20.2 added SHA256SUMS.txt.asc; whatever order GitHub lists the assets in,
        the installer checks the tarball against the list, not the signature."""
        from unittest import mock

        from openfill_photocraft import install

        base = "https://example.invalid/"
        tar = "photocraft-0.20.2-linux-x86_64.tar.gz"
        for names in (
            ["SHA256SUMS.txt.asc", "SHA256SUMS.txt", tar],  # signature listed first (0.20.2 style)
            [tar, "SHA256SUMS.txt", "SHA256SUMS.txt.asc"],
            [tar, "SHA256SUMS.txt"],  # 0.12 to 0.20.1
            ["SHA256SUMS.sig", "SHA256SUMS", tar],
        ):
            release = {"tag_name": "v0.20.2", "assets": [{"name": n, "browser_download_url": base + n} for n in names]}
            with mock.patch.object(install.platform, "machine", return_value="x86_64"):
                name, url, sums = install.linux_asset(release)
            self.assertEqual((name, url), (tar, base + tar))
            self.assertFalse(sums.endswith((".asc", ".sig")), names)
            self.assertTrue(sums.startswith(base + "SHA256SUMS"), names)
        self.assertEqual(install.checksums_url({"SHA256SUMS.txt.asc": "x"}), "")
        with self.assertRaises(RuntimeError):
            install.linux_asset({"tag_name": "v0.20.2", "assets": [{"name": tar, "browser_download_url": "u"}]})

    def test_entry_starts_openfill(self):
        from openfill_photocraft import install

        command = Path("/home/u/.local/bin/openfill-photocraft")
        text = install.entry_for(command, self.ORIGINAL, False, "x")
        self.assertIn(f'Exec="{command}" %F', text)
        self.assertNotIn("TryExec", text)
        self.assertNotIn("Desktop Action", text)
        self.assertNotIn("Actions=", text)
        self.assertIn("MimeType=image/png;", text)
        self.assertIn(install.MARKER, text)

    def test_separate_entry_leaves_images_to_photocraft(self):
        from openfill_photocraft import install

        text = install.entry_for(Path("/x/openfill-photocraft"), self.ORIGINAL, True, "x")
        self.assertIn("Name=PhotoCraft with OpenFill", text)
        self.assertNotIn("MimeType", text)

    def test_install_and_uninstall_keep_a_users_own_entry(self):
        from openfill_photocraft import install

        with tempfile.TemporaryDirectory() as tmp:
            saved = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "HOME")}
            os.environ.update(XDG_DATA_HOME=tmp, HOME=tmp)  # never the real home folder
            try:
                apps = Path(tmp) / "applications"
                apps.mkdir()
                own = apps / install.DESKTOP_ID
                own.write_text(self.ORIGINAL)
                install.install_entry(Path("/x/openfill-photocraft"), None, False)
                self.assertIn(install.MARKER, own.read_text())
                with contextlib.redirect_stdout(io.StringIO()):
                    install.uninstall()
                self.assertEqual(own.read_text(), self.ORIGINAL)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class UnixUninstallTests(unittest.TestCase):
    def test_linux_entry_and_cleanup_preserve_user_data(self):
        from openfill_photocraft import install

        with tempfile.TemporaryDirectory(prefix="uninstall linux ") as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(install, "data_home", return_value=root),
                mock.patch.object(install, "bin_dir", return_value=root / "bin"),
                mock.patch.object(install, "refresh_menus"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app = install.share_dir() / "app"
                app.mkdir(parents=True)
                config = install.share_dir() / "config.json"
                config.write_text("keep")
                install.write_command(app, sys.executable)
                entry = install.install_uninstall_entry()
                self.assertIn("--uninstall-dialog", entry.read_text())
                self.assertIn("Terminal=false", entry.read_text())
                install.uninstall()
                self.assertFalse(entry.exists())
                self.assertFalse(app.exists())
                self.assertEqual(config.read_text(), "keep")

    def test_linux_confirmation_cancel_remove_and_failure(self):
        from openfill_photocraft import install

        with (
            mock.patch.object(install.sys, "platform", "linux"),
            mock.patch("tkinter.Tk") as root,
            mock.patch("tkinter.messagebox.askyesno") as ask,
            mock.patch("tkinter.messagebox.showinfo") as success,
            mock.patch("tkinter.messagebox.showerror") as error,
            mock.patch.object(install, "uninstall") as remove,
        ):
            ask.return_value = False
            self.assertEqual(install.uninstall_dialog(), 0)
            remove.assert_not_called()
            ask.return_value = True
            self.assertEqual(install.uninstall_dialog(), 0)
            remove.assert_called_once_with()
            success.assert_called_once()
            remove.side_effect = PermissionError("close it first")
            self.assertEqual(install.uninstall_dialog(), 1)
            error.assert_called_once()
            self.assertEqual(root.return_value.destroy.call_count, 3)

    def test_mac_confirmation_cancel_and_remove(self):
        from openfill_photocraft import install

        with (
            mock.patch.object(install.sys, "platform", "darwin"),
            mock.patch.object(install.subprocess, "run") as run,
            mock.patch.object(install, "uninstall_mac") as remove,
        ):
            run.return_value.returncode = 1
            install.uninstall_dialog()
            remove.assert_not_called()
            run.return_value.returncode = 0
            install.uninstall_dialog()
            remove.assert_called_once()


class WindowsUninstallTests(unittest.TestCase):
    def test_install_adds_uninstall_shortcut_and_removal_preserves_settings(self):
        from openfill_photocraft import install

        with tempfile.TemporaryDirectory(prefix="openfill windows ") as tmp:
            home = Path(tmp)
            app = home / "app"
            links = [home / "start/PhotoCraft with OpenFill.lnk", home / "desktop/PhotoCraft with OpenFill.lnk"]
            settings = home / "config.json"
            settings.write_text("keep")

            def shortcut(link, *args):
                link.parent.mkdir(parents=True, exist_ok=True)
                link.touch()

            with (
                mock.patch.object(install, "windows_home", return_value=home),
                mock.patch.object(install, "windows_shortcut_paths", return_value=links),
                mock.patch.object(install, "copy_app", side_effect=lambda p: p.mkdir(parents=True)),
                mock.patch.object(install, "make_shortcut", side_effect=shortcut) as make,
                mock.patch("openfill_photocraft.__main__.find_photocraft", return_value=("PhotoCraft.exe", [])),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(install.install_windows("C:/Python/python.exe"), 0)
                uninstaller = install.windows_uninstall_shortcut()
                self.assertTrue(uninstaller.is_file())
                self.assertEqual(make.call_count, 3)
                self.assertIn(install.UNINSTALL_BOOT, make.call_args.args[2])
                compile((app / install.UNINSTALL_BOOT).read_text(), install.UNINSTALL_BOOT, "exec")
                install.uninstall_windows()
                self.assertFalse(app.exists())
                self.assertFalse(uninstaller.exists())
                self.assertEqual(settings.read_text(), "keep")

    def test_native_dialog_cancel_success_and_error(self):
        import ctypes

        from openfill_photocraft import install

        with (
            mock.patch.object(ctypes, "windll", create=True) as dll,
            mock.patch.object(install, "uninstall_windows") as remove,
        ):
            message = dll.user32.MessageBoxW
            message.return_value = 7
            self.assertEqual(install.uninstall_windows_dialog(), 0)
            remove.assert_not_called()
            message.return_value = 6
            self.assertEqual(install.uninstall_windows_dialog(), 0)
            remove.assert_called_once()
            self.assertEqual(message.call_args.args[2], "OpenFill removed")
            remove.side_effect = PermissionError("Close OpenFill first")
            self.assertEqual(install.uninstall_windows_dialog(), 1)
            self.assertEqual(message.call_args.args[2], "Could not remove OpenFill")


class MacInstallTests(unittest.TestCase):
    def test_mac_discovery_checks_applications_without_scanning_personal_folders(self):
        from openfill_photocraft import __main__ as launch

        binary = Path("/test/Applications/PhotoCraft.app/Contents/MacOS/PhotoCraft")
        with (
            mock.patch.object(launch.sys, "platform", "darwin"),
            mock.patch.object(launch.shutil, "which", return_value=None),
            mock.patch.dict(launch.os.environ, {"PHOTOCRAFT_BIN": ""}),
            mock.patch.object(launch.Path, "home", return_value=Path("/test")),
            mock.patch.object(launch.Path, "is_file", autospec=True, side_effect=lambda p: p == binary),
            mock.patch.object(launch.os, "access", return_value=True),
            mock.patch.object(launch.Path, "glob", side_effect=AssertionError("must not scan folders")),
        ):
            self.assertEqual(launch.find_photocraft(None), [str(binary)])

    def test_finder_launcher_keeps_python_architecture(self):
        from openfill_photocraft import install

        with (
            mock.patch.object(install.sys, "platform", "darwin"),
            mock.patch.object(install.platform, "machine", return_value="arm64"),
        ):
            self.assertEqual(install.mac_python_command("/a path/python"), "/usr/bin/arch -arm64 '/a path/python'")

    def test_aqua_borderless_setter_does_not_parse_none_as_boolean(self):
        from openfill_photocraft import ui

        window = mock.Mock()
        window._w = ".pill"
        window.tk.call.return_value = "None"
        with mock.patch.object(ui.sys, "platform", "darwin"):
            self.assertEqual(ui.make_borderless(window, keyboard=False), "override")
        window.tk.call.assert_called_once_with("wm", "overrideredirect", ".pill", True)
        window.overrideredirect.assert_not_called()

    def test_install_launch_and_uninstall_preserve_settings(self):
        import plistlib
        import subprocess

        from openfill_photocraft import install

        with tempfile.TemporaryDirectory(prefix="openfill mac ") as tmp:
            root = Path(tmp)
            bundle = root / "Applications/PhotoCraft with OpenFill.app"
            home = root / "Library/Application Support/OpenFill for PhotoCraft"
            home.mkdir(parents=True)
            config = home / "config.json"
            config.write_text("keep settings")
            # A shell interpreter double records argv, including a path with spaces.
            python = root / "fake python"
            output = root / "args"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > ' + __import__("shlex").quote(str(output)) + "\n")
            python.chmod(0o755)
            with (
                mock.patch.object(install, "mac_app", return_value=bundle),
                mock.patch.object(install, "mac_home", return_value=home),
                mock.patch("openfill_photocraft.__main__.find_photocraft", return_value=["PhotoCraft"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(install.install_mac(str(python)), 0)
                info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
                self.assertEqual(info["CFBundleIdentifier"], install.MAC_BUNDLE_ID)
                if os.name != "nt":  # Windows cannot execute a Unix shell launcher.
                    subprocess.run([str(bundle / "Contents/MacOS/openfill-photocraft"), "a picture.png"], check=True)
                    self.assertEqual(output.read_text().splitlines(), ["-m", "openfill_photocraft", "a picture.png"])
                self.assertTrue((home / "app/openfill_photocraft").is_dir())
                remove_bundle = install.mac_uninstall_app()
                info = plistlib.loads((remove_bundle / "Contents/Info.plist").read_bytes())
                self.assertEqual(info["CFBundleIdentifier"], install.MAC_BUNDLE_ID + ".uninstall")
                if os.name != "nt":  # Windows cannot execute a Unix shell launcher.
                    subprocess.run([str(remove_bundle / "Contents/MacOS/openfill-uninstall")], check=True)
                    self.assertEqual(
                        output.read_text().splitlines(), ["-m", "openfill_photocraft.install", "--uninstall-dialog"]
                    )
                install.uninstall_mac()
                self.assertFalse(remove_bundle.exists())
                self.assertFalse(bundle.exists())
                self.assertFalse((home / "app").exists())
                self.assertEqual(config.read_text(), "keep settings")

    def test_unrelated_app_is_not_replaced(self):
        from openfill_photocraft import install

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "existing.app"
            bundle.mkdir()
            (bundle / "keep").write_text("user file")
            with (
                mock.patch.object(install, "mac_app", return_value=bundle),
                mock.patch("openfill_photocraft.__main__.find_photocraft", return_value=["PhotoCraft"]),
                mock.patch.object(install, "copy_app") as copy,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(install.install_mac("python3"), 1)
                copy.assert_not_called()
                self.assertEqual((bundle / "keep").read_text(), "user file")


class WindowsDiscoveryTests(unittest.TestCase):
    """PhotoCraft's .msi: 0.12 to 0.16 for all users (HKLM, Program Files\\PhotoCraft); from 0.17
    per user by default (HKCU, %LOCALAPPDATA%\\Programs\\ArtCraft\\PhotoCraft) or for all users
    (HKLM, Program Files\\ArtCraft\\PhotoCraft). Read from the .msi files' Registry, Directory
    and CustomAction tables."""

    APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths\photocraft.exe"

    def fake_winreg(self, values):
        """A stand-in for winreg holding ``values``: {(hive, key, value name): data}."""
        from types import SimpleNamespace

        opened = []

        class Key:
            def __init__(self, hive, name):
                self.hive, self.name = hive, name

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def open_key(hive, name, _reserved, _access):
            opened.append((hive, name))
            if not any(h == hive and k == name for h, k, _v in values):
                raise FileNotFoundError(name)
            return Key(hive, name)

        def query(key, name):
            try:
                return values[(key.hive, key.name, name)], 1
            except KeyError:
                raise FileNotFoundError(name) from None

        fake = SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            HKEY_LOCAL_MACHINE="HKLM",
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            KEY_WOW64_32KEY=4,
            OpenKey=open_key,
            QueryValueEx=query,
        )
        return fake, opened

    def test_photocraft_0_12_to_0_16_machine_install(self):
        from openfill_photocraft import __main__ as launch

        fake, _ = self.fake_winreg({("HKLM", r"Software\PhotoCraft", "InstallDir"): "C:/Program Files/PhotoCraft/"})
        self.assertEqual(launch._registry_photocraft(fake), [Path("C:/Program Files/PhotoCraft/photocraft.exe")])

    def test_photocraft_0_17_and_later_per_user_install(self):
        """What a plain double click or msiexec /qn gives from 0.17: nothing in HKLM at all."""
        from openfill_photocraft import __main__ as launch

        folder = "C:/Users/me/AppData/Local/Programs/ArtCraft/PhotoCraft/"
        fake, opened = self.fake_winreg(
            {
                ("HKCU", r"Software\PhotoCraft", "InstallDir"): folder,
                ("HKCU", self.APP_PATHS, ""): folder + "photocraft.exe",
            }
        )
        self.assertEqual(launch._registry_photocraft(fake), [Path(folder) / "photocraft.exe"])
        self.assertIn(("HKLM", r"Software\PhotoCraft"), opened)  # both hives are looked at

    def test_the_users_own_install_comes_first_and_app_paths_alone_is_enough(self):
        from openfill_photocraft import __main__ as launch

        fake, _ = self.fake_winreg(
            {
                ("HKLM", r"Software\PhotoCraft", "InstallDir"): "C:/Program Files/PhotoCraft",
                ("HKCU", self.APP_PATHS, ""): '"D:/Apps/ArtCraft/PhotoCraft/photocraft.exe"',
                ("HKCU", r"Software\PhotoCraft", "InstallFolder"): "",
            }
        )
        self.assertEqual(
            launch._registry_photocraft(fake),
            [Path("D:/Apps/ArtCraft/PhotoCraft/photocraft.exe"), Path("C:/Program Files/PhotoCraft/photocraft.exe")],
        )
        self.assertEqual(launch._registry_photocraft(self.fake_winreg({})[0]), [])

    def find_on_windows(self, exe, registry=()):
        from openfill_photocraft import __main__ as launch

        home = Path("/test/me")
        env = {"PHOTOCRAFT_BIN": "", "LOCALAPPDATA": str(home / "AppData/Local"), "PROGRAMFILES": "/test/pf"}
        with (
            mock.patch.object(launch.sys, "platform", "win32"),
            mock.patch.object(launch.shutil, "which", return_value=None),
            mock.patch.dict(launch.os.environ, env),
            mock.patch.object(launch.Path, "home", return_value=home),
            mock.patch.object(launch.Path, "is_dir", return_value=False),
            mock.patch.object(launch.Path, "is_file", autospec=True, side_effect=lambda p: p == exe),
            mock.patch.object(launch.os, "access", return_value=True),
            mock.patch.object(launch, "_registry_photocraft", return_value=list(registry)),
        ):
            return launch.find_photocraft(None)

    def test_find_photocraft_in_every_install_folder(self):
        for exe in (
            Path("/test/me/AppData/Local/Programs/ArtCraft/PhotoCraft/photocraft.exe"),  # 0.17+, per user
            Path("/test/pf/ArtCraft/PhotoCraft/photocraft.exe"),  # 0.17+, all users
            Path("/test/pf/PhotoCraft/photocraft.exe"),  # 0.12 to 0.16
        ):
            with self.subTest(exe=exe):
                self.assertEqual(self.find_on_windows(exe), [str(exe)])
        moved = Path("/test/E/PhotoCraft/photocraft.exe")  # a folder picked in the installer
        self.assertIsNone(self.find_on_windows(moved))
        self.assertEqual(self.find_on_windows(moved, [moved]), [str(moved)])


class BarLogicTests(unittest.TestCase):
    FACTS = {
        "active": "realvisxl-v5",
        "models": {
            "remove": [
                ("lama-big", "LaMa (big-lama) - prompt-free object removal at full resolution"),
                ("mat-places512", "MAT Places512 (fp16) - prompt-free object removal"),
                ("rorem-sdxl", "RORem (SDXL inpainting, erase-trained) - object removal"),
            ],
            "fill": [
                ("dreamshaper-8-inpainting", "DreamShaper 8 (SD 1.5)"),
                ("realvisxl-v5", "RealVisXL V5.0 (fp16)"),
                ("flux1-fill-dev-gguf-q4", "FLUX.1 Fill [dev] GGUF Q4_0 (+ T5 GGUF, CLIP-L, FLUX VAE)"),
            ],
        },
    }

    def test_removal_menu_defaults_to_lama_and_offers_every_installed_model(self):
        try:
            from openfill_photocraft.bar import removal_choices
        except ImportError:
            self.skipTest("no tkinter")
        choices = removal_choices(self.FACTS)
        self.assertEqual(next(iter(choices.items())), ("LaMa (default)", "default"))
        self.assertEqual(choices["MAT Places512"], "mat-places512")
        self.assertEqual(choices["RORem"], "rorem-sdxl")
        # generation models too, FLUX included: any model can remove when picked
        self.assertEqual(choices["DreamShaper 8"], "dreamshaper-8-inpainting")
        self.assertEqual(choices["FLUX.1 Fill [dev] GGUF Q4_0"], "flux1-fill-dev-gguf-q4")
        self.assertEqual(list(choices.values())[3], "realvisxl-v5")  # the active one first among them
        self.assertNotIn("refine:realvisxl-v5", choices.values())  # no two-pass Remove any more
        only_painters = {"active": "", "models": {"fill": self.FACTS["models"]["fill"]}}
        self.assertNotIn("default", removal_choices(only_painters).values())
        self.assertEqual(removal_choices({}), {"The active model": "default"})

    def test_fill_menu_also_offers_the_removal_models(self):
        try:
            from openfill_photocraft.bar import fill_choices
        except ImportError:
            self.skipTest("no tkinter")
        choices = fill_choices(self.FACTS)
        self.assertEqual(
            list(choices.values())[:3], ["dreamshaper-8-inpainting", "realvisxl-v5", "flux1-fill-dev-gguf-q4"]
        )
        self.assertIn("rorem-sdxl", choices.values())
        self.assertIn("lama-big", choices.values())

    def test_expand_margins(self):
        try:
            from openfill_photocraft.bar import expand_margins
        except ImportError:
            self.skipTest("no tkinter")
        self.assertEqual(expand_margins(1448, 1086, (16, 9)), (241, 0, 242, 0))
        self.assertEqual(expand_margins(1000, 1000, (4, 3)), (166, 0, 167, 0))
        self.assertEqual(expand_margins(1000, 1000, (3, 4)), (0, 166, 0, 167))
        self.assertEqual(expand_margins(1600, 900, (16, 9)), (0, 0, 0, 0))
        self.assertEqual(expand_margins(100, 80, None), (25, 20, 25, 20))


class ModelInfoTests(unittest.TestCase):
    def test_hardware_line(self):
        from types import SimpleNamespace

        from openfill_photocraft.modelinfo import hardware_line

        gb = 1024**3
        hw = SimpleNamespace(
            gpu_name="NVIDIA GeForce RTX 4070 Laptop GPU",
            vram_bytes=8 * gb,
            ram_bytes=32 * gb,
            disk_free_bytes=328 * gb,
        )
        self.assertEqual(hardware_line(hw), "RTX 4070 Laptop · 8 GB VRAM · 32 GB RAM · 328 GB free")
        none = SimpleNamespace(gpu_name=None, vram_bytes=0, ram_bytes=16 * gb, disk_free_bytes=None)
        self.assertEqual(hardware_line(none), "No graphics card found · 16 GB RAM")

    def test_hardware_line_for_an_amd_card(self):
        from types import SimpleNamespace

        from openfill_photocraft.modelinfo import hardware_line

        gb = 1024**3
        hw = SimpleNamespace(
            gpu_name="AMD Radeon RX 7900 XT", vram_bytes=20 * gb, ram_bytes=32 * gb, disk_free_bytes=None
        )
        self.assertEqual(hardware_line(hw), "RX 7900 XT · 20 GB VRAM · 32 GB RAM")
        apu = SimpleNamespace(gpu_name="AMD Radeon(TM) 8060S Graphics", vram_bytes=0, ram_bytes=0, disk_free_bytes=None)
        self.assertEqual(hardware_line(apu), "Radeon 8060S Graphics")

    def test_amd_note_in_the_model_detail(self):
        from openfill_photocraft.modelinfo import model_detail

        row = {
            "capabilities": ["fill"],
            "size": "10.0 GB",
            "amd_note": "on AMD cards GGUF models have been reported to give only noise",
        }
        self.assertEqual(
            model_detail(row), "Fill, remove · 10.0 GB · on AMD cards GGUF models have been reported to give only noise"
        )

    def test_setup_card_lines(self):
        from openfill_photocraft.modelinfo import choose_card_line, download_card_line

        base = {"size": "about 1.9 GB", "folder": r"C:\Users\a\AppData\Local\OpenFill\ComfyUI"}
        self.assertNotIn("very slow", download_card_line(base))
        self.assertIn("No NVIDIA", download_card_line({**base, "cpu": True, "gpu": "none"}))
        amd = download_card_line({**base, "size": "about 1.5 GB", "amd": True, "gpu": "amd"})
        self.assertIn("version for AMD cards (about 1.5 GB", amd)
        self.assertIn("not tested on an AMD card", amd)
        old = download_card_line({**base, "cpu": True, "gpu": "amd"})
        self.assertIn("RX 7000 or newer", old)
        self.assertIn("very slow", old)
        self.assertNotIn("No NVIDIA", old)
        self.assertIn("ROCm", choose_card_line(True))
        self.assertNotIn("ROCm", choose_card_line(False))

    def test_setup_card_lines_for_intel(self):
        from openfill_photocraft.modelinfo import choose_card_line, download_card_line

        base = {"size": "about 1.4 GB", "folder": r"C:\Users\a\AppData\Local\OpenFill\ComfyUI"}
        intel = download_card_line({**base, "intel": True, "gpu": "other"})
        self.assertIn("version for Intel graphics (about 1.4 GB", intel)
        self.assertIn("not tested on Intel", intel)
        self.assertNotIn("very slow", intel)
        old = download_card_line({**base, "cpu": True, "gpu": "other"})
        self.assertIn("Intel Arc", old)
        self.assertIn("very slow", old)
        self.assertNotIn("No NVIDIA", old)
        linux = choose_card_line(False, True)
        self.assertIn("XPU", linux)
        self.assertIn("Not tested on Intel", linux)
        self.assertNotIn("ROCm", linux)
        self.assertNotIn("XPU", choose_card_line(False, False))

    def test_comfy_help_url_per_vendor(self):
        from openfill_photocraft.modelinfo import comfy_help_url

        self.assertTrue(comfy_help_url("amd").endswith("/docs/install.md#amd-graphics-cards"))
        self.assertTrue(comfy_help_url("intel").endswith("/docs/gpus.md#intel-arc-and-core-ultra"))
        for vendor in ("nvidia", "apple", "none", "", None):
            self.assertTrue(comfy_help_url(vendor).endswith("/docs/install.md#openfill-for-photocraft"))

    def test_preflight_carries_the_intel_build(self):
        from types import SimpleNamespace

        from openfill_photocraft.service import Service

        from openfill import comfy_setup
        from openfill.hardware import Hardware

        manifest = comfy_setup.load_manifest()
        hw = Hardware(gpu_name="Intel(R) Arc(TM) B580 Graphics", vram_bytes=12 * 1024**3, gpu_vendor="intel")
        pre = comfy_setup.preflight(manifest, hw, None, 50 * 1024**3, Path("C:/x"), 8188, False, platform="win32")
        fake = SimpleNamespace(app=SimpleNamespace(setup_preflight=lambda d: (manifest, pre)))
        out = Service.comfy_preflight(fake)
        self.assertTrue(out["intel"])
        self.assertFalse(out["amd"])
        self.assertFalse(out["cpu"])
        self.assertTrue(out["url"].endswith("/ComfyUI_windows_portable_intel.7z"))
        queued = []
        fake.comfy_preflight = lambda: out
        fake.app.queue_comfy_setup = lambda *a: queued.append(a) or "task"
        Service.setup_comfy(fake)
        self.assertEqual(queued[0][1:], (False, False, False, True))

    def test_model_detail_and_needed_by(self):
        from types import SimpleNamespace

        from openfill_photocraft.modelinfo import model_detail, needed_by

        row = {"capabilities": ["fill", "expand", "txt2img"], "size": "6.5 GB"}
        self.assertEqual(model_detail(row), "Fill, expand, create, remove · 6.5 GB")  # any fill model can remove
        self.assertEqual(model_detail({"capabilities": ["remove"], "size": "196.1 MB"}), "Remove · 196.1 MB")
        eraser = {"capabilities": ["remove"], "template": "sdxl_eraser", "size": "6.9 GB"}
        self.assertEqual(model_detail(eraser), "Remove (erase-trained) · 6.9 GB")
        entries = [
            SimpleNamespace(id="realvisxl-v5", arch="sdxl", requires_models=("fooocus-inpaint-patch",)),
            SimpleNamespace(id="juggernaut-xl-v9", arch="sdxl", requires_models=("fooocus-inpaint-patch",)),
            SimpleNamespace(id="fooocus-inpaint-patch", arch="sdxl", requires_models=()),
        ]
        users = needed_by(entries, "fooocus-inpaint-patch")
        self.assertEqual(users, "Needed by the SDXL models")
        patch = {"capabilities": ["patch"], "size": "1.2 GB"}
        self.assertEqual(model_detail(patch, users), "Needed by the SDXL models · 1.2 GB")


class FactsCacheTests(unittest.TestCase):
    def test_models_show_from_the_last_run_until_the_first_look(self):
        try:
            from openfill_photocraft.bar import Refresher
        except ImportError:
            self.skipTest("no tkinter")
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "bar-models.json"
            first = Refresher.__new__(Refresher)  # no lookup thread: only the cache
            first.cache, first._saved = cache, ""
            rows = [
                {"id": "lama-big", "name": "LaMa", "installed": True, "runnable": True, "statusText": "Installed"},
                {"id": "x", "name": "X", "installed": False, "runnable": True, "statusText": "Downloading 42%"},
            ]
            facts = {"comfy_ok": True, "models": {"remove": [("lama-big", "LaMa")]}, "rows": rows, "hardware": "h"}
            first._save_cache(facts)
            again = Refresher.__new__(Refresher)
            again.cache = cache
            loaded = again._load_cache()
            self.assertTrue(loaded["cached"])
            self.assertEqual(loaded["models"]["remove"], [("lama-big", "LaMa")])
            self.assertEqual([r["statusText"] for r in loaded["rows"]], ["Installed", ""])  # no stale download
            cache.write_text("{not json")
            self.assertEqual(again._load_cache(), {})


class LookTests(unittest.TestCase):
    """The panel's drawing helpers (no window needed) and its text inputs (with a display)."""

    def test_svg_paths_become_polylines(self):
        try:
            from openfill_photocraft import ui
        except ImportError:
            self.skipTest("no tkinter")
        ((points, closed),) = ui.path_polylines("M6 9l6 6 6-6")
        self.assertEqual((points, closed), ([(6, 9), (12, 15), (18, 9)], False))
        ((ring, closed),) = ui.path_polylines("M2 12a10 10 0 1 1 20 0a10 10 0 1 1-20 0z")
        self.assertTrue(closed)
        self.assertTrue(all(abs(((x - 12) ** 2 + (y - 12) ** 2) ** 0.5 - 10) < 1e-6 for x, y in ring))
        for name in ui.ICONS:
            self.assertEqual(ui.icon_image(name, "#ffffff", 16).size, (16, 16))

    def test_rounded_rectangle_keeps_its_border(self):
        try:
            from openfill_photocraft import ui
        except ImportError:
            self.skipTest("no tkinter")
        im = ui.rrect_image(40, 32, 4, "#2b2d31", "#1e1f22", "#3a3c42")
        self.assertEqual(im.getpixel((20, 0)), (0x3A, 0x3C, 0x42))  # 1 px border
        self.assertEqual(im.getpixel((20, 16)), (0x2B, 0x2D, 0x31))
        self.assertEqual(im.getpixel((0, 0)), (0x1E, 0x1F, 0x22))  # outside the rounded corner

    def test_placeholders_are_not_text(self):
        try:
            import tkinter as tk

            from openfill_photocraft import ui

            root = tk.Tk()
        except Exception:  # noqa: BLE001 - no Tk or no display
            self.skipTest("no display")
        try:
            sent = []
            box = ui.PromptBox(root, lambda: sent.append(box.get()), lambda step: "break")
            box.set_placeholder("Describe the image you want.")
            self.assertEqual(box.get(), "")
            self.assertTrue(box.showing_placeholder)
            box.set("a lighthouse\non a cliff")
            self.assertEqual(box.get(), "a lighthouse\non a cliff")
            box.set("")
            self.assertEqual((box.get(), box.showing_placeholder), ("", True))
            field = ui.Field(root, 200, "hf_...", secret=True)
            self.assertEqual(field.get(), "")
            field.set("hf_abc")
            self.assertEqual((field.get(), field.entry.cget("show")), ("hf_abc", "*"))
            para = ui.small(root, "one two three four five six seven", wrap=60)
            self.assertGreater(int(para.cget("height")), 16)  # wrapped onto more than one 16 px line
            self.assertEqual(
                ui.ellipsize("RealVisXL V5.0 (fp16, with Fooocus inpaint patch)", ui.fonts(root).body, 120)[-3:], "..."
            )
        finally:
            root.destroy()


class ServiceRemoveTests(unittest.TestCase):
    """Remove object runs once, with the model picked (LaMa at full resolution by default)."""

    def run_remove(self, removal):
        from types import SimpleNamespace

        from openfill_photocraft.service import Service

        calls, picked = [], []
        self.picked = picked

        class App:
            def generative_fill(
                self, image, prompt, negative, variations, strength, seed, model, remove=False, as_picked=False
            ):
                calls.append((prompt, variations, strength, model, remove))
                picked.append(as_picked)
                return [SimpleNamespace(name="x")]

        stub = SimpleNamespace(app=App(), OLD_REMOVALS=Service.OLD_REMOVALS)
        image = SimpleNamespace(pending_group="g")
        layers = Service._remove(stub, image, {"removal": removal} if removal is not None else {}, 5, 2)
        self.assertEqual(len(calls), 1)  # one pass, never LaMa then a second model
        self.assertEqual(layers[0].name, "Object Removal")
        self.assertEqual(image.pending_group, "Object Removal")
        return calls[0]

    def test_values(self):
        self.assertEqual(self.run_remove(None), ("", 2, 1.0, "active", True))
        self.assertEqual(self.run_remove("default")[3], "active")
        self.assertFalse(self.picked[0])  # the default may become LaMa
        self.assertEqual(self.run_remove("rorem-sdxl")[3], "rorem-sdxl")
        self.assertEqual(self.run_remove("realvisxl-v5")[3], "realvisxl-v5")
        self.assertTrue(self.picked[0])  # a pick is used as picked, even when it is the active model
        # values saved by older menus
        self.assertEqual(self.run_remove("lama")[3], "lama-big")
        self.assertEqual(self.run_remove("mat")[3], "mat-places512")
        self.assertEqual(self.run_remove("refine:realvisxl-v5")[3], "active")


class ServiceModelTests(unittest.TestCase):
    """Uninstall and Stop downloading, against a stand-in for OpenFill's App and task queue."""

    def make(self, tmp: Path, current=None, queued=()):
        from types import SimpleNamespace

        from openfill_photocraft.service import Service

        tasks = tmp / "tasks"
        tasks.mkdir()
        for i, (kind, model_id) in enumerate(queued):
            (tasks / f"{i}-{kind}.json").write_text(json.dumps({"type": kind, "model_id": model_id}))
        if current:
            (tasks / "9-download.running.json").write_text(json.dumps({"type": "download", "model_id": current}))

        def entry(model_id, needs=()):
            return SimpleNamespace(id=model_id, requires_models=tuple(needs))

        catalog = [
            entry("realvisxl-v5", ["fooocus-inpaint-patch"]),
            entry("juggernaut-xl-v9", ["fooocus-inpaint-patch"]),
            entry("fooocus-inpaint-patch"),
            entry("lama-big"),
        ]
        removed = []

        class Server:
            tasks_dir = tasks

            def pending_tasks(self):
                return [json.loads(p.read_text()) for p in sorted(tasks.glob("*.json"))]

            def task_status(self):
                return {"current": {"type": "download", "model_id": current} if current else None}

        settings = SimpleNamespace(active_model="realvisxl-v5", save=lambda path: None)
        app = SimpleNamespace(
            server=Server(),
            catalog=catalog,
            settings=settings,
            paths=SimpleNamespace(config=tmp / "config.json"),
            remove_model=lambda model_id: removed.append(model_id) or "task-1",
        )
        service = Service.__new__(Service)
        service.app = app
        return service, tasks, removed

    def test_stop_download_drops_the_model_and_its_parts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, tasks, _ = self.make(
                Path(tmp),
                current="fooocus-inpaint-patch",
                queued=[("download", "realvisxl-v5"), ("download", "lama-big")],
            )
            stopped = service.stop_download("realvisxl-v5")
            self.assertEqual(stopped, ["fooocus-inpaint-patch", "realvisxl-v5"])
            left = sorted(json.loads(p.read_text())["model_id"] for p in tasks.glob("*.json"))
            self.assertEqual(left, ["fooocus-inpaint-patch", "lama-big"])  # the running file stays for the worker
            self.assertTrue((tasks / "cancel").exists())  # the patch download it was waiting for stops

    def test_stop_download_keeps_a_part_another_model_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, tasks, _ = self.make(
                Path(tmp),
                current="fooocus-inpaint-patch",
                queued=[("download", "realvisxl-v5"), ("download", "juggernaut-xl-v9")],
            )
            self.assertEqual(service.stop_download("realvisxl-v5"), ["realvisxl-v5"])
            self.assertFalse((tasks / "cancel").exists())
            left = sorted(json.loads(p.read_text())["model_id"] for p in tasks.glob("*.json"))
            self.assertEqual(left, ["fooocus-inpaint-patch", "juggernaut-xl-v9"])

    def test_uninstall_queues_the_removal_and_clears_the_active_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _tasks, removed = self.make(Path(tmp))
            self.assertEqual(service.uninstall("realvisxl-v5"), "task-1")
            self.assertEqual(removed, ["realvisxl-v5"])
            self.assertEqual(service.app.settings.active_model, "")


def dead_pid() -> int:
    """The pid of a process that has exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class EngineFeedbackTests(unittest.TestCase):
    """ComfyUI stopping mid-job or while idle: a clear status and PhotoCraft's own words."""

    def make(self, generate, autostart=True, alive=True):
        from types import SimpleNamespace

        from openfill_photocraft.service import JobState, Service

        service = Service.__new__(Service)
        service.state = JobState()
        service.engine, service._engine_watch, service._warm, service._thread = "ready", None, None, None
        service.bridge = mock.Mock()
        service._one_step = mock.Mock()
        # now: True/False the managed ComfyUI runs/exited, None none recorded; stop: a stop was
        # asked; healthy: a ComfyUI (managed or the user's) answers at the URL
        running = {"now": alive, "stop": False, "healthy": True}

        def death_watch():
            return None if running["now"] is None else (lambda: not running["now"])

        server = SimpleNamespace(
            death_watch=death_watch,
            read_state=lambda: {"stop": True} if running["stop"] else {},
            is_healthy=lambda: running["healthy"],
        )
        service.app = SimpleNamespace(
            generative_fill=generate,
            settings=SimpleNamespace(autostart=autostart, uses_remote=False, uses_cloud=False),
            uses_cloud=False,
            server=server,
            log=SimpleNamespace(warning=lambda *a: None, info=lambda *a: None),
        )
        return service, running

    def lost(self, *_args, **_kwargs):
        from openfill.backend import BackendUnavailable
        from openfill.backends.comfy import CONNECTION_LOST

        raise BackendUnavailable(CONNECTION_LOST)

    def status(self, service):
        try:
            from openfill_photocraft.bar import Bar
        except ImportError:
            self.skipTest("no tkinter")
        bar = Bar.__new__(Bar)
        bar.service = service
        return bar._engine_status({"comfy_ok": True})

    def test_comfyui_lost_mid_job_says_stopped_and_that_generate_restarts_it(self):
        from openfill_photocraft.service import ENGINE_STOPPED

        service, _alive = self.make(self.lost)
        service._job("fill", {"prompt": "a balloon"})
        message = service.state.snapshot()["message"]
        self.assertIn("ComfyUI stopped while it was generating", message)
        self.assertIn("Run it again", message)
        self.assertNotIn("GIMP", message)
        self.assertNotIn("Filters >", message)
        self.assertEqual(service.engine, ENGINE_STOPPED)
        self.assertEqual(self.status(service), "Stopped: Generate restarts it")

        # the next Generate starts it again and the status line follows
        service.app.generative_fill = lambda *a, **k: []
        service._job("fill", {"prompt": "a balloon"})
        self.assertEqual(service.state.snapshot()["state"], "done")
        self.assertEqual(self.status(service), "Ready")

    def test_a_job_that_never_reaches_comfyui_keeps_stopped(self):
        from openfill_photocraft.service import ENGINE_STOPPED

        service, _alive = self.make(self.lost)
        service.engine = ENGINE_STOPPED
        service.bridge.active_image.return_value = None  # no image open: fails before ComfyUI
        service._job("fill", {})
        self.assertEqual(service.engine, ENGINE_STOPPED)

    def test_autostart_off_says_not_running(self):
        service, _alive = self.make(self.lost, autostart=False)
        service._job("fill", {})
        self.assertEqual(self.status(service), "AI engine: not running")

    def test_idle_comfyui_exit_turns_ready_into_stopped_and_back(self):
        from openfill_photocraft.service import ENGINE_STOPPED

        service, running = self.make(self.lost)
        service._engine_ready()
        service.check_engine()
        self.assertEqual(service.engine, "ready")  # still running
        running.update(now=False, healthy=False)
        service.check_engine()
        self.assertEqual(service.engine, ENGINE_STOPPED)
        running["now"] = None  # the supervisor removed its state: still stopped
        service.check_engine()
        self.assertEqual(service.engine, ENGINE_STOPPED)
        running["now"] = True  # started again (Check in Settings > Engine), still loading
        service.check_engine()
        self.assertEqual(self.status(service), "Starting the AI engine...")
        running["healthy"] = True  # it answers
        service.check_engine()
        self.assertEqual(self.status(service), "Ready")

    def test_a_restart_that_dies_while_loading_goes_back_to_stopped(self):
        service, running = self.make(self.lost)
        service.engine_stopped()
        running.update(now=True, healthy=False)
        service.check_engine()
        self.assertEqual(self.status(service), "Starting the AI engine...")
        running["now"] = None
        service.check_engine()
        self.assertEqual(self.status(service), "Stopped: Generate restarts it")

    def test_new_watch_is_in_place_before_ready(self):
        service, _running = self.make(self.lost)
        service._engine_watch = lambda: True  # the watch of the ComfyUI that exited
        seen = []
        original = service.app.server.death_watch

        def death_watch():
            seen.append(service.engine)  # what check_engine on the Refresher could see now
            return original()

        service.app.server.death_watch = death_watch
        service.engine = "starting"
        service._engine_ready()
        self.assertEqual(seen, ["starting"])
        self.assertFalse(service._engine_watch())
        self.assertEqual(service.engine, "ready")

    # Adversarial review proofs (fix/comfy-died-feedback): a stopped status only when OpenFill's
    # ComfyUI is really gone, and it clears by itself once a ComfyUI answers.

    def raiser(self, text):
        from openfill.backend import BackendUnavailable

        def generate(*_args, **_kwargs):
            raise BackendUnavailable(text)

        return generate

    def test_stop_engine_with_a_user_started_comfyui_keeps_ready(self):
        from types import SimpleNamespace

        from openfill_photocraft.panels import EngineView

        service, _running = self.make(self.lost, alive=None)  # no managed ComfyUI recorded
        service._engine_ready()
        service.app.stop_backend = lambda: "No managed backend is running."
        EngineView.stop(SimpleNamespace(bar=SimpleNamespace(service=service), say=lambda *a: None))
        for _ in range(3):
            service.check_engine()
        self.assertEqual(self.status(service), "Ready")

    def test_user_started_comfyui_that_answered_late_returns_to_ready(self):
        service, running = self.make(self.raiser("ComfyUI did not answer for 90 s"), alive=None)
        running["healthy"] = False
        service._job("fill", {"prompt": "x"})
        self.assertNotEqual(self.status(service), "Stopped: Generate restarts it")
        running["healthy"] = True  # it answers again
        service.check_engine()
        self.assertEqual(self.status(service), "Ready")

    def test_autostart_off_not_running_recovers_when_comfyui_answers(self):
        service, running = self.make(self.lost, autostart=False, alive=None)
        running["healthy"] = False
        service._job("fill", {})
        service.check_engine()
        self.assertEqual(self.status(service), "AI engine: not running")
        running["healthy"] = True  # the user started ComfyUI again
        service.check_engine()
        self.assertEqual(self.status(service), "Ready")

    def test_setup_problem_is_not_reported_as_stopped(self):
        port_taken = "Port 8188 on 127.0.0.1 is used by another program that is not ComfyUI."
        service, running = self.make(self.raiser(port_taken), alive=None)
        running["healthy"] = False
        service._job("fill", {"prompt": "x"})
        status = self.status(service)
        self.assertNotEqual(status, "Stopped: Generate restarts it")
        self.assertTrue(status.startswith("AI engine: The AI backend is not available: Port 8188"), status)

    def test_stale_state_does_not_flip_a_running_user_comfyui_to_stopped(self):
        from openfill.config import Paths, Settings
        from openfill.server import ServerManager

        service, _running = self.make(lambda *a, **k: [])
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "openfill").ensure()
            # left behind by a supervisor that was killed: both processes are long gone
            paths.state.write_text(json.dumps({"server_pid": dead_pid(), "supervisor_pid": dead_pid()}))
            service.app.server = ServerManager(Settings(), paths)
            service._job("fill", {"prompt": "x"})  # the user's own ComfyUI did the job
            self.assertEqual(self.status(service), "Ready")
            service.check_engine()
            self.assertEqual(self.status(service), "Ready")

    def test_stop_engine_says_stopped_until_it_is_gone(self):
        from types import SimpleNamespace

        from openfill_photocraft.panels import EngineView
        from openfill_photocraft.service import ENGINE_STOPPED

        service, running = self.make(self.lost)
        service._engine_ready()
        service.app.stop_backend = lambda: "Stopping the AI engine."
        view = SimpleNamespace(bar=SimpleNamespace(service=service), say=lambda *a: None)
        running["stop"] = True  # the supervisor is still shutting it down
        EngineView.stop(view)
        self.assertEqual(service.engine, ENGINE_STOPPED)
        service.check_engine()
        self.assertEqual(service.engine, ENGINE_STOPPED)

    def test_user_started_comfyui_is_not_watched(self):
        service, alive = self.make(self.lost)
        alive["now"] = None  # no managed ComfyUI recorded
        service._engine_ready()
        service.check_engine()
        self.assertEqual(service.engine, "ready")

    def test_engine_messages_point_to_photocraft_and_gimp_keeps_its_own(self):
        from openfill_photocraft.service import describe, photocraft_text

        from openfill.backend import SETTINGS_MENU, BackendUnavailable, GenerationTimeout, describe_error
        from openfill.backends.comfy import CONNECTION_LOST
        from openfill.config import Paths, Settings
        from openfill.server import ServerManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = ServerManager(Settings(autostart=False), Paths(Path(tmp)).ensure())
            with (
                mock.patch.object(ServerManager, "is_healthy", return_value=False),
                self.assertRaises(BackendUnavailable) as ctx,
            ):
                manager.ensure_running()
        texts = [
            describe(ctx.exception),
            describe(BackendUnavailable(CONNECTION_LOST)),
            describe(GenerationTimeout("no result after 600 s")),
            describe(RuntimeError("boom")),
            photocraft_text(f"Download LaMa in {SETTINGS_MENU} > Models."),
        ]
        for text in texts:
            self.assertNotIn("GIMP", text)
            self.assertNotIn("Filters >", text)
            self.assertNotIn("Generative Fill Settings", text)
            self.assertNotIn("\u2014", text)  # no em dash
        self.assertIn('"Start the AI engine with PhotoCraft"', texts[0])
        self.assertIn("then press Check", texts[0])
        self.assertIn("comfyui.log (OpenFill's settings: gear button > Engine > Logs)", texts[1])
        self.assertIn("request_timeout in config.json", texts[2])
        self.assertIn("OpenFill's settings: gear button > Models", texts[4])
        # GIMP's own messages are unchanged
        self.assertIn("folder of your GIMP profile", CONNECTION_LOST)
        self.assertIn(SETTINGS_MENU, describe_error(BackendUnavailable("x")))


class ServiceSetupTests(unittest.TestCase):
    """The ComfyUI download (Windows first run) as the panel follows it."""

    def test_progress_then_the_folder_is_taken_into_use(self):
        from types import SimpleNamespace

        from openfill_photocraft.service import Service

        status = {"current": {"type": "setup_comfy", "state": "downloading", "done": 2**30, "total": 2**31}}
        calls = []
        service = Service.__new__(Service)
        service.app = SimpleNamespace(task_status=lambda: status, reload_settings=lambda: calls.append("reload"))
        service.rescan_models = lambda: calls.append("rescan")
        service.warm_up = lambda: calls.append("warm")
        service.setup_task = "t1"
        step = service.setup_progress()
        self.assertEqual(step["text"], "Downloading ComfyUI: 1.0 GB of 2.0 GB")
        self.assertGreater(step["fraction"], 0)
        status = {"current": None, "history": [{"task_id": "t1", "state": "done"}]}
        self.assertIsNone(service.setup_progress())
        self.assertEqual(calls, ["reload", "rescan", "warm"])
        self.assertEqual(service.setup_task, "")
        service.setup_task = "t2"
        status = {"current": None, "history": [{"task_id": "t2", "state": "error", "message": "no network"}]}
        self.assertEqual(service.setup_progress(), {"error": "no network"})

    def test_preflight_names_the_archive_it_would_download(self):
        from types import SimpleNamespace

        from openfill_photocraft.service import Service

        from openfill import comfy_setup

        manifest = comfy_setup.load_manifest(ROOT / "models" / "comfy_setup.json")
        for older, amd in ((False, False), (True, False), (False, True)):
            pre = SimpleNamespace(
                older_gpu=older,
                amd_build=amd,
                gpu="amd" if amd else "nvidia",
                blocked=False,
                needs_cpu_choice=False,
                text=lambda: "",
            )
            service = Service.__new__(Service)
            service.app = SimpleNamespace(setup_preflight=lambda d, pre=pre: (manifest, pre))
            info = service.comfy_preflight()
            want = manifest.for_amd() if amd else manifest.for_older_gpus() if older else manifest
            self.assertEqual(info["amd"], amd)
            self.assertEqual(info["url"], want.archive_url)
            self.assertEqual(info["bytes"], want.archive_size)
            self.assertTrue(info["url"].startswith("https://github.com/comfyanonymous/ComfyUI/releases/download/"))


class RemoteGpuTests(unittest.TestCase):
    """The Remote GPU choice in the page's settings, the gear and the warm-up."""

    def _api(self, settings):
        from types import SimpleNamespace

        from openfill_photocraft import web

        saved = []
        app = SimpleNamespace(
            settings=settings,
            paths=SimpleNamespace(config="cfg"),
            reload_settings=lambda: None,
            uses_cloud=False,
        )
        settings.save = lambda path: saved.append(path)
        api = web.Api.__new__(web.Api)
        api.service = SimpleNamespace(app=app, warm_up=lambda: None)
        return api, saved

    def test_the_page_picks_the_remote_gpu_and_keeps_the_code_out_of_sight(self):
        from openfill.config import Settings

        code = "A" * 32
        api, saved = self._api(Settings())
        out = api.post(
            "/api/settings",
            {"backend": "remote", "remote_url": f"ofgpu://{code}@100.101.102.103:8390", "remote_ssh": ""},
        )
        settings = api.service.app.settings
        self.assertEqual(settings.backend, "remote")
        self.assertEqual((settings.remote_token, settings.remote_paired), (code, "http://100.101.102.103:8390"))
        self.assertEqual(out["remote_url"], "http://100.101.102.103:8390")
        self.assertEqual(out["backend"], "remote")
        self.assertNotIn(code, json.dumps(out))
        self.assertEqual(saved, ["cfg"])
        out = api.post("/api/settings", {"backend": "comfyui"})
        self.assertEqual(settings.backend, "comfyui")
        self.assertEqual(settings.remote_token, code)  # kept for switching back

    def test_the_gear_switch_moves_a_pasted_code_out_of_the_field(self):
        from types import SimpleNamespace

        from openfill_photocraft.panels import GearView

        from openfill.config import Settings

        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        code = "B" * 32
        gear = GearView.__new__(GearView)
        gear.engine = SimpleNamespace(remote_url=Var(f" ofgpu://{code}@gpu-pc:8390 "), remote_ssh=Var(" becke@gpu-pc "))
        settings = Settings()
        gear._apply_remote(settings)
        self.assertEqual(settings.remote_token, code)
        self.assertEqual(gear.engine.remote_url.get(), "http://gpu-pc:8390")
        self.assertEqual(settings.remote_ssh, "becke@gpu-pc")

    def test_warm_up_connects_to_the_remote_gpu_without_a_local_comfyui(self):
        from types import SimpleNamespace

        from openfill_photocraft.service import Service

        checked = []
        service = Service.__new__(Service)
        service.app = SimpleNamespace(
            uses_cloud=False,
            settings=SimpleNamespace(uses_remote=True, comfy_dir="", autostart=False),
            check_backend=lambda: checked.append(1) or SimpleNamespace(ok=True, detail="ComfyUI 0.39.0"),
            has_model_for=lambda _op: False,
            log=SimpleNamespace(info=lambda *a: None),
        )
        service.engine = ""
        service.warm_up()
        service._warm.join(5)
        self.assertEqual(checked, [1])
        self.assertEqual(service.engine, "ready")


class PageTests(unittest.TestCase):
    def test_key_is_required(self):
        from openfill_photocraft import web

        class StubService:
            state = None

        api = web.Api.__new__(web.Api)
        api.service, api.key = StubService(), "secret-key"
        api.state = lambda: {"ok": True}
        server, url = web.serve(api)
        try:
            base = url.split("/?key=")[0]
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/state", timeout=5)
            self.assertEqual(cm.exception.code, 403)
            with urllib.request.urlopen(base + "/api/state?key=secret-key", timeout=5) as r:
                self.assertEqual(json.loads(r.read()), {"ok": True})
            self.assertTrue(url.endswith("?key=secret-key"))
        finally:
            server.shutdown()

    def test_about_names_openfill_and_the_photocraft_range(self):
        """A Reddit user read OpenFill 0.2.4 as PhotoCraft 0.2.4: the About line says both."""
        import openfill_photocraft as pkg

        oldest, newest = pkg.PHOTOCRAFT_TESTED
        self.assertEqual(
            pkg.about(), f"OpenFill for PhotoCraft {pkg.__version__} · works with PhotoCraft {oldest} to {newest}"
        )
        self.assertNotIn(f"PhotoCraft {pkg.__version__} to", pkg.about())
        self.assertLess(tuple(map(int, oldest.split("."))), tuple(map(int, newest.split("."))))

    def test_state_and_page_show_the_about_line(self):
        from types import SimpleNamespace

        import openfill_photocraft as pkg
        from openfill_photocraft import web

        app = SimpleNamespace(
            active_model_id=lambda: "lama-big",
            settings=SimpleNamespace(comfy_dir="", uses_remote=False, uses_cloud=False),
            uses_cloud=False,
        )
        service = SimpleNamespace(
            app=app,
            state=SimpleNamespace(snapshot=lambda: {}),
            photocraft=lambda: {"connected": False},
            busy=lambda: False,
            setup_progress=lambda: None,
        )
        api = web.Api(service)
        state = api.state()
        self.assertEqual(state["about"], pkg.about())
        self.assertEqual(state["version"], pkg.__version__)
        page = (Path(web.__file__).with_name("static") / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="about"', page)
        self.assertIn("s.about", page)


class SharedModelsPageTests(unittest.TestCase):
    """Engine settings: models of another ComfyUI, read only (issue #6)."""

    def api(self):
        from openfill_photocraft import web

        from openfill.config import Settings
        from openfill.shared_models import Candidate

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = mock.Mock()
        app.settings = Settings()
        app.paths.config = Path(tmp.name) / "config.json"
        app.reload_settings = lambda: None
        app.shared_suggestions.return_value = [Candidate(Path(tmp.name) / "krita" / "server", "Krita AI Diffusion")]

        def check(text, models_root=None):
            from openfill import shared_models
            from openfill.backend import BackendError

            folders = shared_models.parse(text)
            if any(not f.startswith("/") for f in folders):
                raise BackendError("Use the full path of the folder")
            return shared_models.join(folders)

        app.check_shared_models.side_effect = check
        api = web.Api.__new__(web.Api)
        api.service = mock.Mock(app=app)
        return api, app, Path(tmp.name)

    def test_setting_goes_through_the_app_only_when_it_changes(self):
        api, app, _tmp = self.api()
        self.assertIn("shared_models", api.settings())
        api.post("/api/settings", {"steps": 30, "shared_models": ""})
        app.set_shared_models.assert_not_called()
        self.assertEqual(app.settings.steps, 30)
        api.post("/api/settings", {"shared_models": "/k/server"})
        app.set_shared_models.assert_called_once_with("/k/server\n")

    def test_a_bad_folder_changes_no_setting(self):
        """Review B8: the folders are checked before anything is saved."""
        from openfill.backend import BackendError

        api, app, _tmp = self.api()
        with self.assertRaises(BackendError):
            api.post("/api/settings", {"steps": 31, "shared_models": "relative/folder"})
        self.assertEqual(app.settings.steps, 25)
        self.assertFalse(app.paths.config.exists())
        app.set_shared_models.assert_not_called()

    def test_the_field_shows_one_line(self):
        api, app, _tmp = self.api()
        app.settings.shared_models = "/a\n/b\n"
        self.assertEqual(api.settings()["shared_models"], os.pathsep.join(["/a", "/b"]))

    def test_find_adds_what_is_found(self):
        api, _app, tmp = self.api()
        out = api.post("/api/shared/find", {"text": "/already"})
        self.assertEqual(out["text"], os.pathsep.join(["/already", str(tmp / "krita" / "server")]))
        self.assertEqual(out["found"], [f"Krita AI Diffusion ({tmp / 'krita' / 'server'})"])

    def test_page_has_the_field_and_the_read_only_text(self):
        page = (ROOT / "photocraft" / "openfill_photocraft" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="s-shared_models"', page)
        self.assertIn('id="shared-find"', page)
        self.assertIn("never downloads, deletes or installs anything there", page)


class CloudCompanionTests(unittest.TestCase):
    """The optional cloud backend in the companion: settings never hand out the key, every
    tool offers the cloud model, and nothing local starts while the cloud is picked."""

    KEY = "sk-or-v1-" + "fedcba9876543210" * 4

    def setUp(self):
        from types import SimpleNamespace

        from openfill.app import App

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        profile = Path(self.tmp.name)
        (profile / "openfill").mkdir()
        (profile / "openfill" / "config.json").write_text(
            json.dumps({"comfy_dir": str(profile / "none"), "url": "http://127.0.0.1:9"}), encoding="utf-8"
        )
        self.app = App(
            interactive=False,
            io=SimpleNamespace(profile_dir=lambda: str(profile), message=lambda _t: None),
            runner_cls=object,
        )
        self.addCleanup(self._close_log)
        self.config = profile / "openfill" / "config.json"

    def _close_log(self):
        for handler in list(self.app.log.handlers):
            handler.close()
            self.app.log.removeHandler(handler)

    def api(self):
        from types import SimpleNamespace

        from openfill_photocraft import web

        warmed = []
        service = SimpleNamespace(
            app=self.app,
            warm_up=lambda: warmed.append(True),
            state=SimpleNamespace(snapshot=lambda: {}),
            photocraft=lambda: {},
            busy=lambda: False,
            setup_progress=lambda: None,
        )
        return web.Api(service), warmed

    def test_page_settings_save_the_key_but_never_return_it(self):
        api, warmed = self.api()
        with self.assertRaises(ValueError):  # no key yet: the cloud cannot be picked
            api.post("/api/settings", {"backend": "cloud"})
        with self.assertRaises(ValueError):
            api.post("/api/settings", {"backend": "a1111"})  # not offered here
        out = api.post("/api/settings", {"backend": "cloud", "cloud_api_key": self.KEY, "cloud_model": "x/y"})
        self.assertNotIn(self.KEY, json.dumps(out))
        self.assertTrue(out["cloud_key_saved"])
        self.assertEqual(self.app.settings.cloud_api_key, self.KEY)
        if os.name == "posix":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        api.post("/api/settings", {"backend": "cloud", "cloud_api_key": ""})  # empty keeps the saved key
        self.assertEqual(self.app.settings.cloud_api_key, self.KEY)
        state = api.state()
        self.assertNotIn(self.KEY, json.dumps(state))
        self.assertEqual(state["cloud"]["note"], "Cloud: your image is sent to OpenRouter.")
        self.assertEqual(state["activeModel"], "cloud")
        self.assertTrue(state["comfyReady"])
        self.assertEqual(warmed, [])
        api.post("/api/settings", {"backend": "comfyui"})
        self.assertIsNone(api.state()["cloud"])
        self.assertEqual(warmed, [True])  # back to local: ComfyUI may warm up again

    def test_every_tool_offers_the_cloud_model(self):
        from types import SimpleNamespace

        from openfill_photocraft.bar import Refresher, fill_choices, removal_choices, status_color
        from openfill_photocraft.ui import WARN

        self.app.settings.backend = "cloud"
        self.app.settings.set_cloud("openrouter", "", "", self.KEY)
        refresher = Refresher.__new__(Refresher)  # no lookup thread
        refresher.service = SimpleNamespace(app=self.app)
        refresher._vendor = "none"
        facts = refresher.look()
        self.assertTrue(facts["cloud"] and facts["cloud_key"])
        name = "google/gemini-2.5-flash-image (OpenRouter)"
        for tool in ("fill", "remove", "expand", "txt2img"):
            self.assertEqual(facts["models"][tool], [("cloud", name)])
        self.assertEqual(removal_choices(facts), {"google/gemini-2.5-flash-image": "cloud"})
        self.assertEqual(fill_choices(facts), {name: "cloud"})
        self.assertEqual(status_color("Cloud: OpenRouter"), WARN)
        self.assertNotIn(self.KEY, json.dumps(facts))

    def test_bridge_reads_a_jpeg_from_the_service(self):
        buf = io.BytesIO()
        Image.new("RGB", (6, 4), (200, 10, 20)).save(buf, "JPEG")
        width, height, rgba = PhotoCraftBridge.decode_image(None, buf.getvalue())
        self.assertEqual((width, height, len(rgba)), (6, 4, 6 * 4 * 4))
        self.assertEqual(rgba[3], 255)

    def test_switching_service_does_not_send_the_old_key_elsewhere(self):
        if str(ROOT) not in sys.path:
            sys.path.append(str(ROOT))  # the fake service of the engine's tests
        from tests.test_cloud import FakeService

        fake = FakeService()
        self.addCleanup(fake.close)
        fake.json_route("/v1/models", {"data": [{"id": "m"}]})
        api, _warmed = self.api()
        api.post("/api/settings", {"backend": "cloud", "cloud_api_key": self.KEY})  # an OpenRouter key
        # later: OpenAI-compatible at a LiteLLM address, key field left empty
        change = {"backend": "cloud", "cloud_provider": "openai", "cloud_url": fake.url + "/v1", "cloud_model": "m"}
        with self.assertRaises(ValueError) as caught:
            api.post("/api/settings", change)
        self.assertIn("another service or address", str(caught.exception))
        self.assertEqual(self.app.settings.cloud_provider, "openrouter")  # a refused save changes nothing
        # even if the settings say so (edited by hand), the key is not sent there
        self.app.settings.cloud_provider, self.app.settings.cloud_url = "openai", fake.url + "/v1"
        self.app.settings.cloud_model = "m"
        health = self.app.check_backend()
        self.assertFalse(health.ok)
        self.assertEqual(fake.requests, [])
        state = api.state()
        self.assertIn("another service or address", state["cloud"]["keyProblem"])
        self.assertFalse(state["cloud"]["key"])
        self.assertNotIn(self.KEY, json.dumps(state) + json.dumps(api.settings()))
        self.assertTrue(api.settings()["cloud_key_elsewhere"])
        api.post("/api/settings", {**change, "cloud_api_key": "sk-litellm-0123456789abcdef"})  # a key for it
        self.assertTrue(self.app.check_backend().ok)
        self.assertEqual(fake.requests[-1]["headers"]["Authorization"], "Bearer sk-litellm-0123456789abcdef")

    def test_service_answer_never_reaches_ghostscript(self):
        from PIL import EpsImagePlugin

        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 4 4\nnewpath 0 0 moveto 4 4 lineto stroke\nshowpage\n"
        calls = []

        def fake_gs(*a, **k):
            calls.append(True)
            raise OSError("ghostscript blocked by the test")

        with mock.patch.object(EpsImagePlugin, "Ghostscript", fake_gs):
            for data in (eps, b"\xff\xd8\xff" + eps, b"RIFF\0\0\0\0WEBP" + eps):  # EPS behind JPEG/WebP magic too
                with self.assertRaises(Exception):  # noqa: B017 - refused in any way, never run
                    PhotoCraftBridge.decode_image(None, data)
        self.assertEqual(calls, [])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_note_uses_the_exact_host_rule(self):
        """The settings page's note before saving: the same rule as the panel and GIMP."""
        from openfill.backends.cloud import cloud_note

        page = ROOT / "photocraft" / "openfill_photocraft" / "static" / "index.html"
        script = r"""
const fs=require("fs");const html=fs.readFileSync(process.argv[1],"utf8");
const src=html.slice(html.indexOf("function cloudFields"),html.indexOf('["s-backend","s-cloud_provider"'));
const out=[];for(const [p,u] of JSON.parse(process.argv[2])){
  const els={"s-backend":{value:"cloud"},"cloud-fields":{},"s-cloud_provider":{value:p},
    "s-cloud_url":{value:u,placeholder:""},"s-cloud_model":{},"cloud-note":{}};
  const $=id=>els[id];eval(src);cloudFields();out.push(els["cloud-note"].textContent)}
console.log(JSON.stringify(out));
"""
        cases = [
            ["openrouter", ""],
            ["openrouter", "https://evilopenrouter.ai/api/v1"],
            ["openrouter", "https://eu.openrouter.ai/api/v1"],
            ["openai", "https://user:hunter2@LLM.example.com:8443/v1"],
            ["openai", "http://localhost:4000/v1"],
        ]
        run = subprocess.run(
            ["node", "-e", script, str(page), json.dumps(cases)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [cloud_note(p, u) for p, u in cases])

    def test_no_comfyui_warm_up_while_the_cloud_is_picked(self):
        from openfill_photocraft.service import Service

        self.app.settings.backend = "cloud"
        service = Service.__new__(Service)
        service.app = self.app
        service._warm = None
        service.warm_up()
        self.assertIsNone(service._warm)

    def test_engine_view_switches_and_never_shows_the_key(self):
        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception:  # noqa: BLE001 - no Tk or no display
            self.skipTest("no display")
        from types import SimpleNamespace

        from openfill_photocraft.panels import EngineView

        try:
            bar = SimpleNamespace(
                root=root, host="PhotoCraft", service=SimpleNamespace(app=self.app), call_soon=lambda fn, *a: fn(*a)
            )
            view = EngineView(bar, root)
            view.pack()
            self.app.settings.backend = "cloud"
            self.app.settings.set_cloud("openrouter", "", "", self.KEY)
            view.load_backend(self.app.settings)
            root.update()
            self.assertTrue(view.cloud.winfo_ismapped())
            self.assertFalse(view.local.winfo_ismapped())
            self.assertEqual(view.key.get(), "")
            self.assertIn("Saved", view.key.placeholder)
            self.assertEqual(view.note.cget("text"), "Cloud: your image is sent to OpenRouter.")
            view._show_backend("comfyui")
            root.update()
            self.assertTrue(view.local.winfo_ismapped())
            self.assertFalse(view.cloud.winfo_ismapped())
            self.assertEqual(view.backend_values()[0], "comfyui")
        finally:
            root.destroy()
            from openfill_photocraft import ui

            ui.FONTS = None  # the fonts belonged to this root; the next test makes its own


class LaunchTests(unittest.TestCase):
    """How the launcher starts PhotoCraft, which refuses unknown, repeated and conflicting options."""

    def launch(self, files: list[str], env: dict[str, str]) -> tuple[list[str], dict]:
        from unittest import mock

        from openfill_photocraft import __main__ as main

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, env),
            mock.patch.object(main.subprocess, "Popen") as popen,
        ):
            t = Path(tmp)
            main.launch(["photocraft"], 7890, t / "token", t, t / "exchange", files, t / "log")
            args, kwargs = popen.call_args
            kwargs["stdout"].close()
            return args[0], kwargs

    def test_flags_then_files_after_a_separator(self):
        args, _ = self.launch(["-odd name.png", "b.png"], {})
        self.assertEqual(args[:3], ["photocraft", "--control", "7890"])
        for flag in ("--control-token-file", "--automation-read-root", "--automation-write-root"):
            self.assertEqual(args.count(flag), 1, flag)  # a repeated flag is a usage error
        self.assertEqual(args[-3:], ["--", "-odd name.png", "b.png"])

    def test_no_separator_without_files(self):
        args, _ = self.launch([], {})
        self.assertNotIn("--", args)
        self.assertEqual(args[-2], "--automation-write-root")

    def test_photocraft_gets_no_control_variables(self):
        exported = {
            "PHOTOCRAFT_CONTROL_TOKEN": "ab" * 32,
            "PHOTOCRAFT_CONTROL_TOKEN_FILE": "/elsewhere/token",
            "PHOTOCRAFT_CONTROL_PORT": "7878",
            "PHOTOCRAFT_AUTOMATION_READ_ROOT": "/",
            "PHOTOCRAFT_AUTOMATION_WRITE_ROOT": "/",
            "PHOTOCRAFT_CONFIG_DIR": "/keep/this",
        }
        _, kwargs = self.launch(["a.png"], exported)
        env = kwargs["env"]
        for name in exported:
            if name != "PHOTOCRAFT_CONFIG_DIR":
                self.assertNotIn(name, env)
        self.assertEqual(env["PHOTOCRAFT_CONFIG_DIR"], "/keep/this")
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))


if __name__ == "__main__":
    unittest.main()


class DefaultBrowserTests(unittest.TestCase):
    def test_default_browser_on_each_platform(self):
        for platform in ("win32", "darwin", "linux"):
            with (
                self.subTest(platform=platform),
                mock.patch.object(sys, "platform", platform),
                mock.patch.object(launch.webbrowser, "open", return_value=True) as browser,
                mock.patch.object(launch.subprocess, "Popen") as process,
            ):
                self.assertTrue(launch.open_panel("http://127.0.0.1:1234/#fill"))
                browser.assert_called_once_with("http://127.0.0.1:1234/#fill", new=2)
                process.assert_not_called()


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "Linux/macOS shell launchers")
class ShellInterpreterTests(unittest.TestCase):
    def run_script(self, script, explicit):
        with tempfile.TemporaryDirectory(prefix="openfill python reuse ") as temp:
            root = Path(temp)
            shutil.copy2(ROOT / "photocraft" / script, root / script)
            package = root / "openfill_photocraft"
            package.mkdir()
            (package / "__init__.py").write_text("")
            recorder = "import json,os,sys; from pathlib import Path; Path(os.environ['TEST_LOG']).write_text(json.dumps(sys.argv))\n"
            (package / "install.py").write_text(recorder)
            (package / "__main__.py").write_text(recorder)
            # Every interpreter sees Pillow but cannot import Tk.
            (root / "PIL.py").write_text("")
            (root / "tkinter.py").write_text("raise ImportError('test: no Tk')\n")
            env = dict(os.environ, PYTHONPATH=str(root), TEST_LOG=str(root / "result.json"))
            env.pop("OPENFILL_PYTHON", None)
            if explicit:
                python = root / "Comfy Python"
                python.symlink_to(sys.executable)
                env["OPENFILL_PYTHON"] = str(python)
            result = subprocess.run(
                ["sh", str(root / script), "--test-argument", "a path with spaces"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((root / "result.json").read_text())
            self.assertEqual(args[1:], ["--test-argument", "a path with spaces"])

    def test_explicit_no_tk_python_with_spaces(self):
        for script in ("install.sh", "openfill-photocraft.sh"):
            with self.subTest(script=script):
                self.run_script(script, True)

    def test_auto_fallback_without_tk(self):
        for script in ("install.sh", "openfill-photocraft.sh"):
            with self.subTest(script=script):
                self.run_script(script, False)

    def run_as_mac(
        self, script: str, extra: Path | None = None, system: str = "Darwin", pillow: bool = True, **more: str
    ) -> subprocess.CompletedProcess:
        """Run ``script`` with ``uname`` saying ``system``, a fake Pillow (or a broken one) and no ComfyUI."""
        temp = tempfile.mkdtemp(prefix="openfill mac python ")
        self.addCleanup(shutil.rmtree, temp, True)
        root = Path(temp)
        stubs = root / "bin"
        stubs.mkdir()
        (stubs / "uname").write_text(f"#!/bin/sh\necho {system}\n")
        (stubs / "uname").chmod(0o755)
        (root / "PIL.py").write_text("" if pillow else "raise ImportError('test: no Pillow')\n")
        package = root / "openfill_photocraft"
        package.mkdir()
        (package / "__init__.py").write_text("")
        for name in ("install.py", "__main__.py"):
            (package / name).write_text("print('started with', __import__('sys').executable)\n")
        path = [str(stubs)] + ([str(extra)] if extra else []) + ["/usr/bin", "/bin"]
        env = {"PATH": os.pathsep.join(path), "HOME": str(root / "home"), "PYTHONPATH": str(root), **more}
        shutil.copy2(ROOT / "photocraft" / script, root / script)  # it puts its own folder on the path
        return subprocess.run(["sh", str(root / script)], env=env, capture_output=True, text=True, timeout=30)

    @unittest.skipUnless(os.access("/usr/bin/python3", os.X_OK), "needs a /usr/bin/python3 to refuse")
    def test_mac_never_runs_the_system_python(self):
        for script in ("install.sh", "openfill-photocraft.sh"):
            with self.subTest(script=script):
                r = self.run_as_mac(script)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertNotIn("started with", r.stdout)
                self.assertIn("python.org/downloads/macos", r.stderr)
                self.assertIn(f"OPENFILL_PYTHON=~/ComfyUI/venv/bin/python sh {script}", r.stderr)

    @unittest.skipUnless(os.access("/usr/bin/python3", os.X_OK), "needs a /usr/bin/python3 to refuse")
    def test_mac_explicit_system_python_is_refused_with_a_reason(self):
        for script in ("install.sh", "openfill-photocraft.sh"):
            with self.subTest(script=script):
                r = self.run_as_mac(script, OPENFILL_PYTHON="/usr/bin/python3")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertNotIn("started with", r.stdout)
                self.assertIn("OPENFILL_PYTHON is macOS's own Python 3.9", r.stderr)

    @unittest.skipUnless(os.access("/usr/bin/python3", os.X_OK), "needs a /usr/bin/python3")
    def test_linux_explicit_usr_bin_python_is_used(self):
        r = self.run_as_mac("install.sh", system="Linux", OPENFILL_PYTHON="/usr/bin/python3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("started with", r.stdout)

    def test_mac_uses_a_python3_outside_usr_bin(self):
        other = Path(tempfile.mkdtemp(prefix="openfill other python "))
        self.addCleanup(shutil.rmtree, other, True)
        (other / "python3").symlink_to(sys.executable)
        r = self.run_as_mac("install.sh", other)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("started with", r.stdout)

    def test_linux_failure_names_the_packages_to_install(self):
        r = self.run_as_mac("install.sh", system="Linux", pillow=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("sudo dnf install python3-pillow python3-tkinter", r.stderr)
        self.assertIn("sudo apt install python3-pil", r.stderr)

    def test_invalid_explicit_python_does_not_silently_switch(self):
        env = dict(os.environ, OPENFILL_PYTHON="/not/an/interpreter")
        result = subprocess.run(["sh", str(ROOT / "photocraft/install.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("OPENFILL_PYTHON", result.stderr)


class MacInputTests(unittest.TestCase):
    def test_mac_keyboard_window_stays_managed(self):
        from openfill_photocraft import ui

        window = mock.Mock()
        with mock.patch.object(ui.sys, "platform", "darwin"):
            self.assertEqual(ui.make_borderless(window), "managed")
            window.tk.call.assert_not_called()
            self.assertEqual(ui.make_borderless(window, keyboard=False), "override")
            window.tk.call.assert_called_once_with("wm", "overrideredirect", window._w, True)

    def test_native_menu_defers_action_until_dismissed_and_restores_focus(self):
        from openfill_photocraft import ui

        select = mock.Mock()
        select.values, select.value, select.manage = ["LaMa", "DreamShaper"], "LaMa", None
        select._native_menu, select.popdown = None, None
        menu = mock.Mock()
        select.winfo_rootx.return_value, select.winfo_rooty.return_value = 10, 20
        previous = select.focus_get.return_value
        commands = []
        menu.add_radiobutton.side_effect = lambda **kw: commands.append(kw["command"])
        select.close.side_effect = lambda: ui.Select.close(select)

        def choose(*_args):
            commands[1]()
            select.pick.assert_not_called()
            menu.destroy.assert_not_called()

        menu.tk_popup.side_effect = choose
        with mock.patch.object(ui.tk, "Menu", return_value=menu), mock.patch.object(ui.tk, "StringVar"):
            ui.Select._mac_menu(select)
        menu.destroy.assert_called_once()
        select.pick.assert_called_once_with("DreamShaper")
        previous.focus_set.assert_called_once()
        self.assertIsNone(select._native_menu)

    def test_native_menu_closed_while_open_is_destroyed_after_dismissal(self):
        # Aqua keeps running Tk timers while it tracks the menu (TKBackgroundLoop in
        # tkMacOSXMenu.c): a close from one of them must not destroy the menu under it.
        from openfill_photocraft import ui

        select = mock.Mock()
        select.values, select.value, select.manage = ["DreamShaper", "RealVisXL"], "DreamShaper", None
        select._native_menu, select.popdown = None, None
        select.winfo_rootx.return_value, select.winfo_rooty.return_value = 10, 20
        select.close.side_effect = lambda: ui.Select.close(select)
        menu = mock.Mock()
        events = []
        menu.destroy.side_effect = lambda: events.append("destroy")
        commands = []
        menu.add_radiobutton.side_effect = lambda **kw: commands.append(kw["command"])

        def tracking(*_args):
            select.close()  # a timer callback while the native menu is open
            events.append("click")
            commands[1]()
            events.append("dismissed")

        menu.tk_popup.side_effect = tracking
        with mock.patch.object(ui.tk, "Menu", return_value=menu), mock.patch.object(ui.tk, "StringVar"):
            ui.Select._mac_menu(select)
        self.assertEqual(events, ["click", "dismissed", "destroy"])
        select.pick.assert_called_once_with("RealVisXL")
        self.assertIsNone(select._native_menu)
        self.assertFalse(select._tracking)


class WindowsInstallerTextTests(unittest.TestCase):
    """What the Windows .cmd looks for and says; it runs on Windows (CI), this reads it anywhere."""

    TEXT = (ROOT / "photocraft" / "Install OpenFill for PhotoCraft.cmd").read_bytes().decode("ascii")

    def test_finds_current_python_org_installs(self):
        for path in (
            r"%LOCALAPPDATA%\Programs\Python\Python314\python.exe",  # the 3.14 installer, per user
            r"%ProgramFiles%\Python313\python.exe",  # installed for all users
        ):
            self.assertEqual(self.TEXT.count(path), 2, path)  # with Pillow, and Pillow added
        # The install manager (python.org's default from 3.14) gives `py`; Pillow is added to it too.
        self.assertIn('py -3 -c "import sys; print(sys.executable)"', self.TEXT)
        self.assertIn('"%PYEXE%") do (', self.TEXT)

    def test_no_python_says_what_to_do_and_opens_the_page(self):
        self.assertIn("Install Python from python.org", self.TEXT)
        # The zip has README.md (with "Use ComfyUI's Python"), not docs\install.md.
        self.assertIn("Use ComfyUI's Python in README.md", self.TEXT)
        self.assertNotIn("docs\\install.md", self.TEXT)

    def test_py_never_waits_for_an_answer_or_installs(self):
        self.assertIn('set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"', self.TEXT)
        calls = [line for line in self.TEXT.splitlines() if "'py -3" in line]
        self.assertEqual(len(calls), 2)
        for line in calls:
            self.assertIn("^<nul", line)

    def test_the_folder_reaches_python_through_the_environment(self):
        # Built into Python source, an apostrophe in the path was a SyntaxError, and a ! was lost
        # to delayed expansion.
        lines = self.TEXT.splitlines()
        self.assertNotIn("%DIR", self.TEXT)
        self.assertLess(lines.index('set "OPENFILL_SRC=%~dp0"'), lines.index("setlocal EnableDelayedExpansion"))
        self.assertIn("os.environ['OPENFILL_SRC']", self.TEXT)
        self.assertIn("double-click Install OpenFill for PhotoCraft.cmd again", self.TEXT)
        self.assertIn('start "" "https://www.python.org/downloads/windows/"', self.TEXT)
        self.assertIn("\r\n", self.TEXT)


@unittest.skipUnless(os.name == "nt", "Windows command installer")
class WindowsInterpreterTests(unittest.TestCase):
    def test_explicit_comfy_python_without_tk(self):
        with tempfile.TemporaryDirectory(prefix="openfill python reuse ") as temp:
            root = Path(temp)
            script = root / "Install OpenFill for PhotoCraft.cmd"
            shutil.copy2(ROOT / "photocraft" / script.name, script)
            package = root / "openfill_photocraft"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "install.py").write_text(
                "import json,os,sys; from pathlib import Path\n"
                "def main(args):\n"
                "    Path(os.environ['TEST_LOG']).write_text(json.dumps({'python':sys.executable,'args':args}))\n"
                "    return 0\n"
            )
            (root / "tkinter.py").write_text("raise ImportError('test: no Tk')\n")
            env = dict(
                os.environ, OPENFILL_PYTHON=sys.executable, PYTHONPATH=str(root), TEST_LOG=str(root / "result.json")
            )
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", str(script), "--test", "a path with spaces"],
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads((root / "result.json").read_text())
            self.assertEqual(data["python"], sys.executable)
            self.assertEqual(data["args"], ["--test", "a path with spaces"])
            self.assertNotIn("Adding Pillow", result.stdout)
