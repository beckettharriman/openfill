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

    def __init__(self, exchange: Path, size=(64, 48)) -> None:
        self.exchange = exchange
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 10
        self.docs = [self._doc("photo.png", size, [{"id": 2, "name": "Background", "visible": True}])]
        self.docs[0]["selection"] = [8, 6, 20, 10]
        self.active = 0

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
            if not doc["selection"]:
                raise RuntimeError("no selection")
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

        state = {"history": ["Open", "Rectangular Marquee", "OpenFill Remove Object"], "revision": 3}

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
        self.assertEqual(watcher.poll(), "remove")
        self.assertEqual(picks, ["remove"])
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


class BarLogicTests(unittest.TestCase):
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
