# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PhotoCraft companion: control client, bridge and page API, without PhotoCraft."""

from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "photocraft"), str(ROOT / "gimp-openfill")]

from openfill_photocraft import control  # noqa: E402
from openfill_photocraft.bridge import SELECTION_CHANNEL, PCImage, PhotoCraftBridge  # noqa: E402
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

    def test_reconnects_after_idle(self):
        server = FakeServer("a" * 64, lambda m, p: 1)
        client = control.ControlClient(server.port, self.token)
        client.call("ui.inspect")
        client._last -= control.IDLE_RECONNECT_S + 1  # as if idle for longer than PhotoCraft allows
        client.call("ui.inspect")
        self.assertEqual(server.connections, 2)
        server.close()


class FakePhotoCraft:
    """Enough of PhotoCraft for the bridge: one document, layers, a selection, file round trips."""

    def __init__(self, exchange: Path, size=(64, 48)) -> None:
        self.exchange = exchange
        self.size = size
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 10
        self.layers = [{"id": 2, "name": "Background", "depth": 0, "visible": True}]
        self.selection = [8, 6, 20, 10]
        self.docs = ["photo.png"]
        self.active = 0
        self.clipboard_from: str | None = None

    def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "ui.inspect":
            return {
                "session": {
                    "active": self.active,
                    "documents": [{"index": i, "name": n} for i, n in enumerate(self.docs)],
                }
            }
        if method == "app.save":
            Image.new("RGBA" if "visible" in params["path"] else "L", self.size, 200).save(
                self.exchange / params["path"]
            )
            return {"path": params["path"]}
        if method == "app.open":
            self.docs.append(params["path"])
            self.active = len(self.docs) - 1
            return {"name": params["path"]}
        if method == "engine.execute":
            return self.run(params["command"], **params["params"])
        raise AssertionError(method)

    def run(self, command: str, **params):
        self.calls.append((command, params))
        if command == "document.inspect":
            return {
                "width": self.size[0],
                "height": self.size[1],
                "hasSelection": bool(self.selection),
                "selectionBounds": self.selection,
                "channels": {"alpha": []},
                "activeLayer": self.layers[0]["id"],
            }
        if command in ("image.duplicate", "channel.duplicate"):
            self.docs.append("temp")
            self.active = len(self.docs) - 1
            return {}
        if command == "file.close":
            self.docs.pop(self.active)
            self.active = 0
            return None
        if command == "document.activate":
            self.active = params["document"]
            return None
        if command == "edit.pasteSpecial.pasteInPlace":
            self.next_id += 1
            self.layers.insert(0, {"id": self.next_id, "name": "Layer", "depth": 0, "visible": True})
            return {"layer": self.next_id}
        if command == "layer.new.group":
            self.next_id += 1
            self.layers.insert(0, {"id": self.next_id, "name": params.get("name"), "depth": 0, "visible": True})
            return {"layer": self.next_id}
        if command == "layer.list":
            return {"activeLayer": self.layers[0]["id"], "layers": self.layers}
        if command == "layer.delete":
            self.layers = [row for row in self.layers if row["id"] != params["layer"]]
        return None

    def commands(self) -> list[str]:
        return [m if m != "engine.execute" else p["command"] for m, p in self.calls if m != "engine.execute"]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exchange = Path(self.tmp.name) / "exchange"
        self.pc = FakePhotoCraft(self.exchange.parent / "exchange")
        self.exchange.mkdir()
        self.bridge = PhotoCraftBridge(self.pc, self.exchange, Path(self.tmp.name))
        self.image = self.bridge.track(PCImage(self.bridge, "photo.png"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_selection_and_region_export(self):
        self.assertEqual(self.bridge.selection_rect(self.image), Rect(8, 6, 20, 10))
        _png, raw = self.bridge.export_selection_mask(self.image, Rect(0, 0, 32, 24))
        self.assertEqual(len(raw), 32 * 24)
        self.assertTrue(self.image.saved_selection)
        self.assertIn(("select.saveSelection", {"name": SELECTION_CHANNEL}), self.pc.calls)
        # past the canvas: transparent, then flattened over the mean colour (opaque)
        _png, flat = self.bridge.export_region(self.image, Rect(48, 0, 32, 24), transparent_outside=True)
        self.assertEqual(len(flat), 32 * 24 * 4)
        self.assertEqual(set(flat[3::4]), {255})
        self.assertEqual(list(self.exchange.iterdir()), [])  # exchange files are cleaned up

    def test_insert_with_selection_mask_grows_and_feathers_inside_the_canvas(self):
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (255, 0, 0)).save(buf, "PNG")
        layers = self.bridge.insert_results(
            self.image, [buf.getvalue()], Rect(0, 0, 64, 48), "Generative Fill", 6.0, None, grow=4
        )
        self.assertEqual(len(layers), 1)
        expand = next(p for c, p in self.pc.calls if c == "select.modify.expand")
        feather = next(p for c, p in self.pc.calls if c == "select.modify.feather")
        self.assertEqual((expand["radius"], expand["applyAtCanvasBounds"]), (4, False))
        self.assertEqual((feather["radius"], feather["applyAtCanvasBounds"]), (6.0, False))
        self.assertIn("layer.layerMask.revealSelection", self.pc.commands())
        self.assertEqual(layers[0].mask_kind, "selection")

    def test_insert_with_explicit_mask_feathers_in_python_and_groups_variations(self):
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (0, 0, 255)).save(buf, "PNG")
        mask = bytes(255 if y > 24 else 0 for y in range(48) for _ in range(64))
        layers = self.bridge.insert_results(
            self.image,
            [buf.getvalue(), buf.getvalue()],
            Rect(0, 0, 64, 48),
            "Generative Expand",
            8.0,
            None,
            mask_pixels=mask,
        )
        self.assertEqual([layer.name for layer in layers], ["Generative Expand (1)", "Generative Expand (2)"])
        self.assertNotIn("select.modify.feather", self.pc.commands())  # feathered in Python instead
        loads = [p for c, p in self.pc.calls if c == "select.loadSelection"]
        self.assertTrue(any(p.get("channel") == "transparency" for p in loads))
        moves = [p for c, p in self.pc.calls if c == "layer.moveTo"]
        self.assertEqual(len(moves), 2)
        self.assertTrue(all(p["position"] == "into" for p in moves))

    def test_replacing_pixels_rebuilds_the_mask_and_deletes_the_old_layer(self):
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        buf = io.BytesIO()
        Image.new("RGB", (64, 48)).save(buf, "PNG")
        (layer,) = self.bridge.insert_results(self.image, [buf.getvalue()], Rect(0, 0, 64, 48), "Fill", 2.0, None)
        old = layer.id
        reveals = self.pc.commands().count("layer.layerMask.revealSelection")
        self.bridge.set_layer_rgba(layer, bytes(64 * 48 * 4))
        self.assertNotEqual(layer.id, old)
        self.assertEqual(self.pc.commands().count("layer.layerMask.revealSelection"), reveals + 1)
        self.assertIn(("layer.delete", {"layer": old}), self.pc.calls)

    def test_resize_canvas_is_an_extending_crop(self):
        self.bridge.resize_canvas(self.image, 100, 80, 10, 20)
        self.assertIn(("image.crop", {"x": -10, "y": -20, "width": 100, "height": 80}), self.pc.calls)


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


if __name__ == "__main__":
    unittest.main()
