# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PhotoCraft companion: control client, bridge and page API, without PhotoCraft."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
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

from openfill_photocraft import control  # noqa: E402
from openfill_photocraft.bridge import BridgeError, PhotoCraftBridge  # noqa: E402
from PIL import Image  # noqa: E402

from openfill.geometry import Rect  # noqa: E402


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

    def __init__(self, exchange: Path, size=(64, 48)) -> None:
        self.exchange = exchange
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 10
        self.docs = [self._doc("photo.png", size, [{"id": 2, "name": "Background", "visible": True}])]
        self.docs[0]["selection"] = [8, 6, 20, 10]
        self.active = 0
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
        if method == "ui.inspect":
            return {
                "session": {
                    "active": self.active,
                    "documents": [
                        {"index": i, "name": d["name"], "revision": d["revision"]} for i, d in enumerate(self.docs)
                    ],
                }
            }
        if method == "app.save":
            kind = "L" if self.doc["name"] == "OpenFill mask" else "RGBA"
            im = Image.new(kind, self.doc["size"], 0 if kind == "L" else (200, 200, 200, 255))
            if kind == "L":
                x, y, w, h = self.doc["selection_source"]
                im.paste(255, (x, y, x + w, y + h))
            im.save(self.exchange / params["path"])
            return {"path": params["path"]}
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
            doc["history"].append(self.EDITS[command])
            doc["revision"] += 1

    def run(self, command: str, **params):
        self.calls.append((command, params))
        doc = self.doc
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
                "hasSelection": bool(doc["selection"]),
                "selectionBounds": doc["selection"],
                "history": list(doc["history"]),
                "revision": doc["revision"],
                "activeLayer": doc["layers"][0]["id"] if doc["layers"] else None,
                "layers": doc["layers"],
                "channels": {"alpha": doc["channels"]},
            }
        if command == "image.duplicate":
            self.docs.append(self._doc(params.get("name", "copy"), doc["size"], [{"id": self._new_id(), "name": "x"}]))
            self.active = len(self.docs) - 1
            return {}
        if command == "select.saveSelection":
            self.docs[params["document"]]["channels"].append({"name": params["name"], "rect": doc["selection"]})
            return {}
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
            for layer_id in params["layers"]:
                layer = next(row for row in src["layers"] if row["id"] == layer_id)
                copy = json.loads(json.dumps(layer))
                copy["id"] = self._new_id()
                for child in copy.get("children", []):
                    child["id"] = self._new_id()
                dest["layers"].insert(0, copy)
                ids.append(copy["id"])
            self.active = params["document"]
            self._edit(dest, command)
            return {"document": params["document"], "layers": ids}
        if command == "select.loadSelection":
            doc["selection"] = [0, 0, *doc["size"]]
        elif command == "layer.layerMask.revealSelection":
            doc["selection"] = None  # adding a mask uses the selection up, as in PhotoCraft
            next(row for row in doc["layers"] if row["id"] == params["layer"])["mask"] = True
        elif command == "layer.setProps":
            row = next(r for r in self._all(doc) if r["id"] == params["layer"])
            row.update({k: v for k, v in params.items() if k in ("name", "visible")})
        elif command == "layer.new.group":
            group = {"id": self._new_id(), "name": params.get("name"), "visible": True, "children": []}
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
        expand = [p for c, p in self.pc.calls if c == "select.modify.expand"]
        self.assertEqual(len(expand), 3)  # the selection is loaded again for each mask
        self.assertEqual((expand[0]["radius"], expand[0]["applyAtCanvasBounds"]), (4, False))
        self.assertNotIn("edit.copy", self.pc.commands())  # the clipboard is never used

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
        self.pc.deselects = 1000  # every try loses the selection
        with self.assertRaises(control.ControlError):
            service.keep("selected")
        self.assertEqual(self.target["history"], history)
        self.assertEqual(self.target["layers"], before)  # the variations are all still there
        self.assertEqual(len(self.pc.docs), 1)  # scratch documents closed
        self.assertEqual(service.layers, layers)  # and still in the picker
        self.assertEqual(len(service.state.variations), 3)
        self.pc.deselects = 0
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
        self.assertEqual(self.target["history"], ["Open", "Duplicate Layer", "Crop"])
        self.assertIn(("image.crop", {"x": -10, "y": -20, "width": 100, "height": 80}), self.pc.calls)

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

    def test_watcher_reports_a_pick_and_removes_its_step(self):
        from openfill_photocraft import menu

        state = {"history": ["Open", "Rectangular Marquee", "OpenFill"], "revision": 3}

        class Client:
            def call(self, method, **params):
                return {
                    "session": {
                        "active": 0,
                        "documents": [{"index": 0, "name": "a.png", "revision": state["revision"]}],
                    }
                }

            def run(self, command, **params):
                if command == "history.deleteState":
                    del state["history"][params["state"] :]
                    state["revision"] += 1
                    return {}
                return {
                    "history": list(state["history"]),
                    "revision": state["revision"],
                    "width": 10,
                    "height": 8,
                    "hasSelection": True,
                    "selectionBounds": [1, 2, 3, 4],
                    "layers": [],
                    "activeLayer": None,
                }

        picks = []
        watcher = menu.Watcher(Client(), picks.append)
        self.assertEqual(watcher.poll(), "panel")
        self.assertEqual(picks, ["panel"])
        self.assertEqual(state["history"], ["Open", "Rectangular Marquee"])
        self.assertEqual(watcher.snapshot()["selection"], [1, 2, 3, 4])
        self.assertIsNone(watcher.poll())  # nothing new


class InstallTests(unittest.TestCase):
    ORIGINAL = (
        "[Desktop Entry]\nType=Application\nName=PhotoCraft\nTryExec=photocraft\nExec=photocraft %F\n"
        "Icon=ai.storyteller.photocraft\nMimeType=image/png;\nActions=new;\n\n"
        "[Desktop Action new]\nName=New Window\nExec=photocraft --new-instance\n"
    )

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
            self.assertEqual(ui.make_borderless(window), "override")
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
