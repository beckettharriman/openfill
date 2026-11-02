# OpenFill for Patchy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Patchy companion: connector client, bridge, watcher and installer, without Patchy."""

from __future__ import annotations

import copy
import io
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "patchy"),
    str(ROOT / "patchy" / "tests"),
    str(ROOT / "photocraft"),
    str(ROOT / "gimp-openfill"),
]

from openfill_patchy import __main__ as launch  # noqa: E402
from openfill_patchy import bridge as bridge_module  # noqa: E402
from openfill_patchy import client as client_module  # noqa: E402
from openfill_patchy import install  # noqa: E402
from openfill_patchy.bridge import PatchyBridge  # noqa: E402
from openfill_patchy.client import (  # noqa: E402
    COMPANION_ID,
    COMPANION_PROTOCOL_MAX,
    COMPANION_PROTOCOL_MIN,
    FIRST_PATCHY_WITH_COMPANIONS,
    PatchyClient,
    PatchyError,
    WorkspaceLost,
    find_connector,
    protocol_problem,
)
from openfill_patchy.focus import FocusReturn  # noqa: E402
from openfill_patchy.service import PatchyService, Watcher  # noqa: E402
from PIL import Image  # noqa: E402

from openfill.geometry import Rect  # noqa: E402

SCRIPTS = {
    getattr(bridge_module, name): name
    for name in ("SNAPSHOT_JS", "INSERT_JS", "SHOW_JS", "REORDER_JS", "REMOVE_JS", "SET_PIXELS_JS", "NEW_IMAGE_JS")
}


def png_bytes(color, size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, "PNG")
    return out.getvalue()


class Doc:
    """One document in the fake Patchy: layers (bottom to top), selection and history."""

    def __init__(self, doc_id: str, name: str, width: int, height: int) -> None:
        self.id, self.name, self.width, self.height = doc_id, name, width, height
        self.composite = Image.new("RGBA", (width, height), (200, 100, 50, 255))
        self.selection: Image.Image | None = Image.new("L", (width, height), 0)
        self.selection.paste(255, (8, 6, 28, 16))
        self.layers: list[dict] = [
            {"id": "1", "name": "Background", "x": 0, "y": 0, "visible": True, "background": True, "fills": []}
        ]
        self.undo: list[tuple[str, tuple]] = []  # (state id, saved document) per step
        self.redo: list[tuple[str, tuple]] = []
        self.state_id = "s0"

    def saved(self) -> tuple:
        return copy.deepcopy((self.width, self.height, self.layers))

    def restore(self, saved: tuple) -> None:
        self.width, self.height, self.layers = copy.deepcopy(saved)

    def top(self) -> dict:
        return self.layers[-1]

    def find(self, layer_id: str, rows: list[dict] | None = None) -> tuple[list[dict], dict]:
        for row in self.layers if rows is None else rows:
            if row["id"] == layer_id:
                return (self.layers if rows is None else rows), row
            if "children" in row:
                try:
                    return self.find(layer_id, row["children"])
                except KeyError:
                    pass
        raise KeyError(layer_id)

    def bounds(self) -> dict | None:
        box = self.selection.getbbox() if self.selection is not None else None
        if not box:
            return None
        return {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}


def public(rows: list[dict], canvas: tuple[int, int]) -> list[dict]:
    """Layer rows as get_state reports them: bounds and the raster mask's settings, no pixels.
    The Background covers the canvas."""
    out = []
    for row in rows:
        item = {k: v for k, v in row.items() if k not in ("pixels", "mask", "size", "children", "background", "fills")}
        if row.get("background"):
            item["bounds"] = {"x": 0, "y": 0, "width": canvas[0], "height": canvas[1]}
        elif "size" in row:
            item["bounds"] = {"x": row["x"], "y": row["y"], "width": row["size"][0], "height": row["size"][1]}
        mask = row.get("mask")
        item["hasLayerMask"] = mask is not None
        item["layerMask"] = (
            {
                "bounds": {"x": mask["x"], "y": mask["y"], "width": mask["size"][0], "height": mask["size"][1]},
                "enabled": True,
                "defaultColor": 0,
                "density": 100.0,
                "feather": mask["feather"],
            }
            if mask
            else None
        )
        if "children" in row:
            item["isGroup"], item["children"] = True, public(row["children"], canvas)
        out.append(item)
    return out


class FakePatchy:
    """Enough of Patchy for the bridge: ``patchy-mcp --attach`` and the Patchy behind it, in
    this process. Newline-delimited JSON-RPC on two pipes, with the tools, state tokens and
    history steps Patchy has; scripts are recognised by their text (the bridge's constants)
    and acted out here."""

    def __init__(self) -> None:
        self.docs = [Doc("7", "photo.png", 64, 48)]
        self.active = "7"
        self.version = 0  # anything the state token covers
        self.ids = itertools.count(100)
        self.states = itertools.count(1)
        self.calls: list[tuple[str, dict]] = []
        self.initialize: dict = {}
        self.busy = 0  # answer this many tool calls with "busy"
        self.disconnect = False  # answer the next edit with "workspace_disconnected"
        self.fail_insert = (
            ""  # the next insert fails once: "partial" after its first layer, "after-undo" after its Undos
        )
        self.on_call = None  # called with (tool, arguments) before a tool runs (the artist's moves)
        self.info = {  # get_info
            "workspace": "attached",
            "workspaceAvailable": True,
            "version": "1.25",
            "mainWindow": {"nativeId": "4242"},
            "companionProtocol": {"min": 1, "max": 1},
        }
        client_in, self._out = os.pipe()  # the fake writes, the client reads
        self._in, client_out = os.pipe()
        self.stdout = os.fdopen(client_in, "rb")
        self.stdin = os.fdopen(client_out, "wb")
        self._writer = os.fdopen(self._out, "wb")
        self._lock = threading.Lock()
        self.returncode = None
        threading.Thread(target=self._serve, daemon=True).start()

    # the process face the client sees
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9

    def doc(self, doc_id: str | None = None) -> Doc:
        return next(d for d in self.docs if d.id == (doc_id or self.active))

    @property
    def token(self) -> str:
        return f"nonce/{self.version}"

    def send(self, message: dict) -> None:
        with self._lock:
            self._writer.write((json.dumps(message) + "\n").encode())
            self._writer.flush()

    def emit(self, event: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": "notifications/patchy/event", "params": event})

    def _serve(self) -> None:
        with os.fdopen(self._in, "rb") as reader:
            for line in reader:
                message = json.loads(line)
                if "id" not in message:
                    continue
                if message["method"] == "initialize":
                    self.initialize = message["params"]
                    self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-11-25"}})
                    continue
                params = message["params"]
                name, args = params["name"], params.get("arguments") or {}
                self.calls.append((name, args))
                try:
                    data, error = self.tool(name, args)
                except Exception as err:  # noqa: BLE001
                    data, error = {"error": "operation_failed", "message": str(err)}, True
                self.send(
                    {"jsonrpc": "2.0", "id": message["id"], "result": {"isError": error, "structuredContent": data}}
                )
        self.returncode = 0

    def state(self) -> dict:
        documents = []
        for d in self.docs:
            bounds = d.bounds()
            documents.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "width": d.width,
                    "height": d.height,
                    "historyStateId": d.state_id,
                    "canUndo": bool(d.undo),
                    "canRedo": bool(d.redo),
                    "selection": {"exists": bounds is not None, **({"bounds": bounds} if bounds else {})},
                    "layers": public(d.layers, (d.width, d.height)),
                }
            )
        return {"activeDocumentId": self.active, "documents": documents, "stateToken": self.token}

    def step(self, d: Doc) -> None:
        """A new history step (the run's one snapshot)."""
        d.undo.append((d.state_id, d.saved()))
        d.redo.clear()
        d.state_id = f"s{next(self.states)}"
        self.version += 1

    def do_undo(self, d: Doc, redo: bool = False) -> bool:
        source, target = (d.redo, d.undo) if redo else (d.undo, d.redo)
        if not source:
            return False
        state_id, saved = source.pop()
        target.append((d.state_id, d.saved()))
        d.state_id = state_id
        d.restore(saved)
        self.version += 1
        return True

    def tool(self, name: str, args: dict) -> tuple[dict, bool]:
        if self.on_call:
            self.on_call(name, args)
        if name == "get_info":
            return copy.deepcopy(self.info), False
        if self.busy:
            self.busy -= 1
            return {"error": "busy", "message": "Finish the current gesture"}, True
        if name == "get_state":
            return self.state(), False
        if args.get("expectedState") != self.token:
            return {"error": "stale_state", "message": "The workspace changed", "state": self.state()}, True
        if self.disconnect:
            self.disconnect = False
            return {"error": "workspace_disconnected", "message": "gone", "retrySafe": False}, True
        if name in ("undo", "redo"):
            changed = self.do_undo(self.doc(args["documentId"]), redo=name == "redo")
            return {"changed": changed, "state": self.state()}, False
        first, body = args["code"].split("\n", 1)
        a = json.loads(first[len("var A = ") : -1])
        kind = SCRIPTS[body]
        try:
            result = getattr(self, "run_" + kind.lower())(a)
        except Exception as err:  # noqa: BLE001 - a script error: Patchy reports "failed"
            return {"status": "failed", "logs": [{"message": str(err)}], "state": self.state()}, True
        return {"status": "done", "result": result, "state": self.state()}, False

    # the scripts --------------------------------------------------------------------

    def run_snapshot_js(self, a: dict) -> dict:
        d = self.doc(a["doc"])
        d.composite.save(a["visible"])
        if d.selection is not None and d.selection.getbbox():
            d.selection.save(a["mask"])
        return {
            "width": d.width,
            "height": d.height,
            "selection": d.selection is not None and bool(d.selection.getbbox()),
        }

    def run_insert_js(self, a: dict) -> dict:
        d = self.doc(a["doc"])
        blocks = {}
        for op in a["ops"]:
            for s in op.get("layers") or []:
                with Image.open(s["pixels"]) as im:
                    blocks[s["pixels"]] = im.convert("RGBA").copy()
                if s.get("mask"):
                    with Image.open(s["mask"]) as im:
                        blocks[s["mask"]] = im.convert("L").copy()
        for _ in range(a["undo"]):
            if not self.do_undo(d):
                raise RuntimeError("OpenFill's result is no longer the last step")
        if self.fail_insert == "after-undo":
            self.fail_insert = ""  # once
            raise RuntimeError("addLayer: out of memory")
        self.step(d)
        made = []
        for op in a["ops"]:
            if op["kind"] == "canvas":
                # resizeCanvas(w, h, {x, y, fill}), as Image > Canvas Size: the content and the
                # selection move, the Background is filled out to the new canvas.
                if not isinstance(op.get("x"), int) or not isinstance(op.get("y"), int):
                    raise RuntimeError("resizeCanvas without an offset leaves the content where it was")
                if not re.fullmatch(r"#[0-9a-f]{6}", str(op.get("fill"))):
                    raise RuntimeError(f"resizeCanvas: bad fill {op.get('fill')!r}")
                d.width, d.height = op["width"], op["height"]
                for row in d.layers:
                    if row.get("background"):
                        row["fills"].append(op["fill"])
                        continue
                    for moved in row.get("children", [row]):
                        for part in (moved, moved.get("mask") or {}):
                            if part:
                                part["x"] += op["x"]
                                part["y"] += op["y"]
                if d.selection is not None:
                    moved_selection = Image.new("L", (d.width, d.height), 0)
                    moved_selection.paste(d.selection, (op["x"], op["y"]))
                    d.selection = moved_selection
                continue
            group = None
            if op["group"]:
                group = {
                    "id": str(next(self.ids)),
                    "name": op["group"],
                    "visible": True,
                    "children": [],
                    "x": 0,
                    "y": 0,
                }
                d.layers.append(group)
            ids = []
            for s in reversed(op["layers"]):
                row = {
                    "id": str(next(self.ids)),
                    "name": s["name"],
                    "x": s["x"],
                    "y": s["y"],
                    "visible": s["visible"],
                    "pixels": blocks[s["pixels"]].tobytes(),
                    "size": blocks[s["pixels"]].size,
                }
                if s.get("mask"):
                    m = blocks[s["mask"]]
                    row["mask"] = {
                        "x": s["x"],
                        "y": s["y"],
                        "size": m.size,
                        "data": m.tobytes(),
                        "feather": s["feather"],
                    }
                (group["children"] if group else d.layers).append(row)
                if self.fail_insert == "partial":
                    self.fail_insert = ""  # once
                    raise RuntimeError("setLayerMask: the layer's pixels are locked")
                ids.insert(0, row["id"])
            made.append({"group": group["id"] if group else None, "layers": ids})
        return {"ops": made}

    def run_show_js(self, a: dict) -> bool:
        d = self.doc(a["doc"])
        self.step(d)
        for row in a["layers"]:
            d.find(row["id"])[1]["visible"] = row["visible"]
        return True

    def run_remove_js(self, a: dict) -> bool:
        d = self.doc(a["doc"])
        self.step(d)
        for layer_id in a["ids"]:
            rows, row = d.find(layer_id)
            rows.remove(row)
        return True

    def run_reorder_js(self, a: dict) -> bool:
        d = self.doc(a["doc"])
        self.step(d)
        for row in a["order"]:
            rows, layer = d.find(row["id"])
            rows.remove(layer)
            rows.append(layer)
            layer["name"], layer["visible"] = row["name"], row["visible"]
        return True

    def run_set_pixels_js(self, a: dict) -> bool:
        d = self.doc(a["doc"])
        self.step(d)
        with Image.open(a["pixels"]) as im:
            d.find(a["layer"])[1]["pixels"] = im.convert("RGBA").tobytes()
        return True

    def run_new_image_js(self, a: dict) -> dict:
        with Image.open(a["images"][0]["pixels"]) as im:
            size = im.size
        d = Doc(str(next(self.ids)), a["name"], *size)
        d.selection = None
        d.layers[0]["name"] = a["images"][0]["name"]
        for extra in a["images"][1:]:
            d.layers.append({"id": str(next(self.ids)), "name": extra["name"], "x": 0, "y": 0, "visible": False})
        self.docs.append(d)
        self.active = d.id
        self.version += 1
        return {"doc": d.id}

    # the artist's moves -------------------------------------------------------------------

    def select(self, box: tuple[int, int, int, int] | None) -> None:
        d = self.doc()
        d.selection = Image.new("L", (d.width, d.height), 0)
        if box:
            d.selection.paste(255, box)
        self.version += 1

    def edit(self) -> None:
        """An edit of the artist's own (a rename, say): a step OpenFill did not make."""
        d = self.doc()
        self.step(d)
        d.layers[0]["name"] = "Renamed"


class FakeClient(PatchyClient):
    def __init__(self, fake: FakePatchy, **kwargs) -> None:
        super().__init__(["patchy-mcp", "--attach"], **kwargs)
        self.fake = fake

    def _spawn(self, kwargs):
        return self.fake


class Fixture(unittest.TestCase):
    """A started client on a fresh fake Patchy, and the bridge over it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.exchange = Path(self.tmp.name) / "exchange"
        self.fake = FakePatchy()
        self.events: list[dict] = []
        self.client = FakeClient(self.fake, on_event=self.events.append)
        self.client.start()
        self.bridge = PatchyBridge(self.client, self.exchange, Path(self.tmp.name))
        self.image = self.bridge.track(self.bridge.active_image())
        self.target = self.fake.doc()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def edits(self) -> list[str]:
        return [name for name, _args in self.fake.calls if name != "get_state"]

    def fill(self, variations=1, grow=4, feather=6.0, name="Generative Fill"):
        """A fill as ``App.generative_fill`` makes it: read, wait for the generator, insert."""
        self.bridge.selection_rect(self.image)
        self.bridge.export_region(self.image, Rect(0, 0, 64, 48))
        self.bridge.export_selection_mask(self.image, Rect(0, 0, 64, 48))
        colors = ["red", "blue", "green", "white", "black"][:variations]
        layers = self.bridge.insert_results(
            self.image, [png_bytes(c) for c in colors], Rect(0, 0, 64, 48), name, feather, None, grow=grow
        )
        return layers


class ConnectorClientTests(Fixture):
    def test_initialize_asks_for_events_and_names_the_companion(self):
        experimental = self.fake.initialize["capabilities"]["experimental"]
        self.assertEqual(
            experimental,
            {
                "patchy/events": True,
                "patchy/companion": {
                    "id": COMPANION_ID,
                    "name": "OpenFill",
                    "version": launch.__version__,
                    "minProtocol": COMPANION_PROTOCOL_MIN,
                    "maxProtocol": COMPANION_PROTOCOL_MAX,
                },
            },
        )
        self.assertEqual(self.fake.initialize["protocolVersion"], "2025-11-25")

    def test_busy_is_sent_again_until_patchy_answers(self):
        self.fake.busy = 3
        waits = []
        self.client.on_wait = waits.append
        state = self.client.get_state()
        self.assertEqual(state["activeDocumentId"], "7")
        self.assertEqual([n for n, _a in self.fake.calls].count("get_state"), 2 + 3)  # the fixture's, 3 busy, 1 answer
        self.assertEqual(waits, [])  # a moment's busy (a drag) never shows the warning

    def test_waiting_is_reported_only_when_patchy_stays_busy(self):
        watcher = Watcher(self.client, lambda action: None, fallback=60.0)
        self.client.on_wait = watcher.set_waiting
        seen = []
        self.fake.on_call = lambda name, args: seen.append(watcher.info.waiting)
        with mock.patch.object(client_module, "BUSY_NOTICE_S", 0.4):
            self.fake.busy = 2  # about 0.2 s: a drag of the selection's handles
            self.client.get_state()
            self.assertNotIn(True, seen)
            self.assertFalse(watcher.info.waiting)
            seen.clear()
            self.fake.busy = 12  # well past the notice time
            self.client.get_state()
        self.assertIn(True, seen)  # shown while Patchy stayed busy
        self.assertFalse(seen[0])  # not at the first busy reply
        self.assertFalse(watcher.info.waiting)  # and cleared once it answered

    def test_an_edit_lost_with_the_workspace_is_never_sent_again(self):
        self.client.get_state()
        self.fake.disconnect = True
        before = len(self.fake.calls)
        with self.assertRaises(WorkspaceLost):
            self.client.run_script("var A = {};\n" + bridge_module.REMOVE_JS)
        self.assertEqual(len(self.fake.calls), before + 1)  # one try, no replay
        self.assertTrue(self.client.lost)

    def test_a_stale_edit_is_sent_again_with_the_newer_token(self):
        self.client.get_state()
        self.fake.version += 1  # the artist did something meanwhile
        layers = self.fill()
        self.fake.version += 1
        self.bridge.finish(self.image)
        self.assertEqual(len(self.target.undo), 1)
        self.assertEqual(layers[0].id, self.target.top()["id"])

    def test_events_arrive_in_order_on_their_own_thread(self):
        self.fake.emit({"type": "selectionChanged", "documentId": "7", "bounds": None})
        self.fake.emit({"type": "companionCommand", "companion": COMPANION_ID, "action": "show"})
        for _ in range(100):
            if len(self.events) == 2:
                break
            time.sleep(0.01)
        self.assertEqual([e["type"] for e in self.events], ["selectionChanged", "companionCommand"])

    def test_the_connector_is_found_from_patchys_variable_first(self):
        with mock.patch.dict(os.environ, {"PATCHY_MCP": r"C:\Patchy\patchy-mcp.exe"}):
            self.assertEqual(find_connector(), [r"C:\Patchy\patchy-mcp.exe", "--attach"])
            self.assertEqual(find_connector("/opt/patchy-mcp"), ["/opt/patchy-mcp", "--attach"])


class BridgeTests(Fixture):
    def test_reading_leaves_the_document_unchanged(self):
        self.assertEqual(self.bridge.selection_rect(self.image), Rect(8, 6, 20, 10))
        _png, raw = self.bridge.export_selection_mask(self.image, Rect(0, 0, 32, 24))
        self.assertEqual(len(raw), 32 * 24)
        self.assertEqual(raw[6 * 32 + 8], 255)  # inside the selection
        self.assertEqual(raw[0], 0)
        _png, flat = self.bridge.export_region(self.image, Rect(48, 0, 32, 24), transparent_outside=True)
        self.assertEqual(set(flat[3::4]), {255})  # past the canvas: flattened over the mean colour
        self.assertEqual(self.target.undo, [])
        self.assertEqual(self.edits().count("execute_script"), 1)  # one run serves both reads
        self.assertEqual(list(self.exchange.iterdir()), [])

    def test_a_fill_is_one_step_with_the_jobs_own_mask(self):
        layers = self.fill(grow=2, feather=6.0)
        self.assertIsNone(layers[0].id)  # nothing in Patchy yet
        self.fake.select((40, 30, 50, 40))  # the artist selects something else while it works
        self.bridge.finish(self.image)
        self.assertEqual(len(self.target.undo), 1)  # one Undo takes it back
        top = self.target.top()
        self.assertEqual((top["name"], top["id"], top["visible"]), ("Generative Fill", layers[0].id, True))
        mask = Image.frombytes("L", top["mask"]["size"], top["mask"]["data"])
        self.assertEqual(mask.getbbox(), (6, 4, 30, 18))  # the job's selection (8, 6, 28, 16), grown by 2
        self.assertEqual(top["mask"]["feather"], 3.0)  # Patchy's sigma: half GIMP's feather
        self.assertEqual(self.target.bounds(), {"x": 40, "y": 30, "width": 10, "height": 10})  # untouched
        self.assertEqual(list(self.exchange.iterdir()), [])
        self.client.get_state()
        self.client.undo("7")
        self.assertEqual([r["name"] for r in self.target.layers], ["Background"])

    def test_variations_reach_the_document_in_one_step_best_first(self):
        layers = self.fill(variations=3, name="Fill")
        ordered = self.bridge.reorder_variations(self.image, layers, [2, 0, 1], "Fill")
        self.assertEqual([x.name for x in ordered], ["Fill (1)", "Fill (2)", "Fill (3)"])
        self.bridge.finish(self.image)
        self.assertEqual(len(self.target.undo), 1)
        group = self.target.top()
        self.assertEqual(group["name"], "Fill")
        children = list(reversed(group["children"]))  # top first
        self.assertEqual([c["name"] for c in children], ["Fill (1)", "Fill (2)", "Fill (3)"])
        self.assertEqual([c["visible"] for c in children], [True, False, False])
        self.assertEqual(children[0]["pixels"][:4], bytes([0, 128, 0, 255]))  # green, ranked best
        self.assertEqual([x.id for x in ordered], [c["id"] for c in children])
        self.assertTrue(all(x.group == group["id"] for x in ordered))

    def test_an_expand_is_one_step_and_later_rounds_see_the_earlier(self):
        # round one: 16 px more on the right
        self.bridge.export_region(self.image, Rect(32, 0, 48, 48), transparent_outside=True)
        self.bridge.resize_canvas(self.image, 80, 48, 0, 0)
        mask = bytes([0] * 32 + [255] * 16) * 48
        self.bridge.insert_results(
            self.image, [png_bytes("blue", (48, 48))], Rect(32, 0, 48, 48), "Expand", 0.0, None, mask_pixels=mask
        )
        self.assertEqual((self.image.get_width(), self.image.get_height()), (80, 48))
        # round two reads the canvas as round one left it, then 8 px more on the left
        _png, flat = self.bridge.export_region(self.image, Rect(0, 0, 80, 48))
        self.assertEqual(flat[(10 * 80 + 70) * 4 : (10 * 80 + 70) * 4 + 3], bytes([0, 0, 255]))
        self.bridge.resize_canvas(self.image, 88, 48, 8, 0)
        self.bridge.insert_results(
            self.image,
            [png_bytes("red", (24, 48))],
            Rect(0, 0, 24, 48),
            "Expand",
            0.0,
            None,
            mask_pixels=bytes([255] * 24 * 48),
        )
        self.assertEqual(self.target.undo, [])
        self.bridge.finish(self.image)
        self.assertEqual(len(self.target.undo), 1)  # canvas and both rounds, one step
        self.assertEqual((self.target.width, self.target.height), (88, 48))
        by_name = [(r["name"], r["x"]) for r in self.target.layers]
        self.assertEqual(by_name, [("Background", 0), ("Expand", 40), ("Expand", 0)])  # round one moved with the canvas
        rows = self.fake.state()["documents"][0]["layers"]
        self.assertEqual(rows[0]["bounds"], {"x": 0, "y": 0, "width": 88, "height": 48})  # it covers the canvas
        # filled with the photo's mean colour, both rounds (round one's blue is not mixed in)
        self.assertEqual(self.target.layers[0]["fills"], ["#c86432", "#c86432"])
        self.assertEqual([r["hasLayerMask"] for r in rows], [False, True, True])
        self.assertEqual(rows[1]["layerMask"]["bounds"], {"x": 40, "y": 0, "width": 48, "height": 48})
        self.client.get_state()
        self.client.undo("7")
        self.assertEqual((self.target.width, self.target.height, len(self.target.layers)), (64, 48, 1))

    def test_an_expand_moves_the_selection_with_the_content(self):
        self.bridge.export_region(self.image, Rect(0, 0, 80, 60), transparent_outside=True)
        self.bridge.resize_canvas(self.image, 80, 60, 16, 12)
        self.bridge.insert_results(
            self.image, [png_bytes("blue", (80, 60))], Rect(0, 0, 80, 60), "Expand", 0.0, None, use_selection_mask=False
        )
        self.bridge.finish(self.image)
        self.assertEqual(self.target.bounds(), {"x": 24, "y": 18, "width": 20, "height": 10})  # (8, 6) + (16, 12)
        self.assertEqual(self.fake.state()["documents"][0]["layers"][0]["bounds"]["width"], 80)

    def test_a_failed_insert_leaves_the_document_as_it_was(self):
        self.fill(variations=2)
        self.fake.fail_insert = "partial"
        with self.assertRaises(PatchyError):
            self.bridge.finish(self.image)
        self.assertEqual([r["name"] for r in self.target.layers], ["Background"])  # the half step was undone
        self.assertEqual(self.target.undo, [])

    def test_a_cancelled_job_leaves_nothing(self):
        self.fill()
        self.bridge.discard(self.image)
        self.bridge.finish(self.image)
        self.assertEqual(self.target.undo, [])
        self.assertNotIn(
            "INSERT",
            " ".join(SCRIPTS[c["code"].split("\n", 1)[1]] for n, c in self.fake.calls if n == "execute_script"),
        )

    def test_text_to_image_opens_a_new_document(self):
        image = self.bridge.new_image_from_pngs([png_bytes("red"), png_bytes("blue")], "Text to Image: a fox", None)
        d = self.fake.doc(image.id)
        self.assertEqual([r["name"] for r in d.layers], ["Text to Image: a fox (1)", "Text to Image: a fox (2)"])
        self.assertEqual([r["visible"] for r in d.layers], [True, False])


class KeepTests(Fixture):
    def service(self, variations=3):
        layers = self.fill(variations=variations, name="Fill")
        self.bridge.finish(self.image)
        from openfill_photocraft.service import JobState

        service = PatchyService.__new__(PatchyService)
        service.bridge, service.client, service.image, service.layers = self.bridge, self.client, self.image, layers
        service.state = JobState()
        service.app = SimpleNamespace(log=SimpleNamespace(warning=lambda *a: None))
        service.focus = mock.Mock()
        return service, layers

    def test_keep_and_discard_hand_the_keyboard_back_to_patchy(self):
        service, _layers = self.service()
        service.keep("all")
        self.assertEqual(service.focus.request.call_count, 1)
        service.keep("none")
        self.assertEqual(service.focus.request.call_count, 2)

    def test_generate_hands_the_keyboard_back_once_the_job_has_started(self):
        service, _layers = self.service()
        with mock.patch("openfill_photocraft.service.Service.start") as started:
            service.start("fill", {"prompt": "a door"})
        started.assert_called_once()
        service.focus.request.assert_called_once()
        service.focus.reset_mock()
        busy = RuntimeError("A job is already running.")
        with mock.patch("openfill_photocraft.service.Service.start", side_effect=busy), self.assertRaises(RuntimeError):
            service.start("fill", {"prompt": "a door"})
        service.focus.request.assert_not_called()  # no job, nothing to hand back for

    def test_keep_after_picking_is_still_one_step(self):
        service, _layers = self.service()
        service.show_variation(2)
        service.show_variation(1)
        service.keep("selected")
        self.assertEqual(len(self.target.undo), 1)  # one Undo takes it back
        top = self.target.top()
        self.assertEqual((top["name"], top["visible"], "children" in top), ("Fill", True, False))
        self.assertEqual(top["pixels"][:4], bytes([0, 0, 255, 255]))  # the second variation, blue
        self.assertEqual(service.layers[0].id, top["id"])
        self.assertTrue(top.get("mask"))

    def test_discard_leaves_no_step(self):
        service, _layers = self.service()
        service.show_variation(1)
        service.keep("none")
        self.assertEqual(self.target.undo, [])
        self.assertEqual([r["name"] for r in self.target.layers], ["Background"])

    def test_after_an_edit_of_the_artists_nothing_is_undone(self):
        service, _layers = self.service()
        self.fake.edit()
        service.keep("selected")
        self.assertEqual(len(self.target.undo), 3)  # the result, the artist's edit, the delete
        self.assertEqual(self.target.layers[0]["name"], "Renamed")
        self.assertEqual(len(self.target.top()["children"]), 1)

        service, _layers = self.service()
        self.fake.edit()
        service.keep("none")  # the whole group goes, in one step
        self.assertEqual(self.target.top()["name"], "Fill")  # only the first job's kept layer is left
        self.assertNotIn("Fill (1)", [r["name"] for r in self.target.layers])

    def test_a_keep_that_fails_never_loses_the_variations(self):
        for failure in ("after-undo", "partial"):
            with self.subTest(failure):
                self.fake = FakePatchy()
                self.client = FakeClient(self.fake)
                self.client.start()
                self.bridge = PatchyBridge(self.client, self.exchange, Path(self.tmp.name))
                self.image = self.bridge.track(self.bridge.active_image())
                self.target = self.fake.doc()
                service, _layers = self.service()
                service.show_variation(1)
                self.fake.fail_insert = failure
                with self.assertRaises(PatchyError):
                    service.keep("selected")
                self.fake.fail_insert = ""
                group = self.target.top()
                self.assertEqual([c["name"] for c in group["children"]], ["Fill (3)", "Fill (2)", "Fill (1)"])
                self.assertEqual(len([r for r in self.target.layers if "children" in r]), 1)  # once, not twice
                self.assertTrue(self.bridge.unchanged(self.image))  # Keep can be tried again
                service.keep("selected")
                self.assertEqual(self.target.top()["name"], "Fill")
                self.assertEqual(len(self.target.undo), 1)
                self.client.close()


class ProtocolTests(Fixture):
    """The companion protocol check the launcher makes after ``get_info``."""

    def problem(self) -> str | None:
        return protocol_problem(self.client.get_info())

    def test_a_patchy_that_serves_this_protocol(self):
        self.assertIsNone(self.problem())
        self.fake.info["companionProtocol"] = {"min": 1, "max": 3}
        self.assertIsNone(self.problem())

    def test_a_patchy_from_before_companions_names_the_release_needed(self):
        del self.fake.info["companionProtocol"]
        self.fake.info["version"] = "1.24"
        problem = self.problem()
        self.assertIn(f"Patchy {FIRST_PATCHY_WITH_COMPANIONS} or newer", problem)
        self.assertIn("Patchy 1.24", problem)
        self.assertIn("Update Patchy", problem)

    def test_ranges_that_do_not_overlap_say_which_side_to_update(self):
        self.fake.info["companionProtocol"] = {"min": COMPANION_PROTOCOL_MAX + 1, "max": COMPANION_PROTOCOL_MAX + 2}
        self.assertIn("Update OpenFill", self.problem())  # Patchy left this OpenFill's protocol behind
        # A Patchy too old for this OpenFill: only possible once OpenFill needs protocol 2.
        with (
            mock.patch("openfill_patchy.client.COMPANION_PROTOCOL_MIN", 2),
            mock.patch("openfill_patchy.client.COMPANION_PROTOCOL_MAX", 2),
        ):
            self.fake.info["companionProtocol"] = {"min": 1, "max": 1}
            self.assertIn("needs a newer version of Patchy", self.problem())

    def test_an_unreadable_range_is_a_problem_not_a_crash(self):
        for served in ({"min": "1", "max": 1}, {"min": 2, "max": 1}, {"max": 1}, {"min": True, "max": True}):
            self.fake.info["companionProtocol"] = served
            self.assertIn("does not understand", self.problem(), served)


class WatcherTests(Fixture):
    def test_events_keep_the_panel_current_without_polling(self):
        picks = []
        watcher = Watcher(self.client, picks.append, fallback=60.0)
        self.client.on_event = watcher.on_event
        watcher.poll()
        self.assertEqual(watcher.snapshot()["selection"], [8, 6, 20, 10])
        reads = len(self.fake.calls)
        self.fake.emit(
            {"type": "selectionChanged", "documentId": "7", "bounds": {"x": 1, "y": 2, "width": 3, "height": 4}}
        )
        self.fake.emit({"type": "companionCommand", "companion": COMPANION_ID, "action": "show"})
        self.fake.emit({"type": "companionCommand", "companion": "io.example.other", "action": "show"})
        for _ in range(100):
            if picks and watcher.snapshot()["selection"] == [1, 2, 3, 4]:
                break
            time.sleep(0.01)
        self.assertEqual(watcher.snapshot()["selection"], [1, 2, 3, 4])
        self.assertEqual(picks, ["panel"])
        self.assertEqual(len(self.fake.calls), reads)  # no get_state for either

    def test_quit_from_patchys_menu_ends_openfill_and_unknown_actions_are_ignored(self):
        picks: list[str] = []
        quit_asked = threading.Event()
        watcher = Watcher(self.client, picks.append, on_quit=quit_asked.set, fallback=60.0)
        alive = launch.until_quit(lambda: True, quit_asked)
        self.client.on_event = watcher.on_event
        self.fake.emit({"type": "companionCommand", "companion": "io.example.other", "action": "quit"})
        self.fake.emit({"type": "companionCommand", "companion": COMPANION_ID, "action": "dance"})
        self.fake.emit({"type": "companionCommand", "companion": COMPANION_ID, "action": "show"})
        for _ in range(100):
            if picks:
                break
            time.sleep(0.01)
        self.assertEqual(picks, ["panel"])
        self.assertTrue(alive())  # another companion's quit and an unknown action change nothing
        self.fake.emit({"type": "companionCommand", "companion": COMPANION_ID, "action": "quit"})
        quit_asked.wait(1.0)
        self.assertFalse(alive())  # the launcher's loop ends, as when Patchy closes
        self.assertEqual(picks, ["panel"])

    def test_another_document_is_read_on_the_watchers_thread(self):
        watcher = Watcher(self.client, lambda action: None, interval=0.01, fallback=60.0)
        self.client.on_event = watcher.on_event
        watcher.start()
        try:
            other = Doc("9", "other.png", 10, 20)
            self.fake.docs.append(other)
            self.fake.active = "9"
            self.fake.emit({"type": "activeDocumentChanged", "documentId": "9"})
            for _ in range(200):
                if (watcher.snapshot().get("document") or {}).get("name") == "other.png":
                    break
                time.sleep(0.01)
            self.assertEqual(
                watcher.snapshot()["document"], {"name": "other.png", "width": 10, "height": 20, "mode": "RGB"}
            )
            self.assertEqual(watcher.native_id, 4242)  # read for the tether
        finally:
            watcher.stop()


class FakeWin32:
    """The user32 calls ``FocusReturn`` makes: windows by handle, owned by (pid, thread)."""

    PATCHY, PANEL, DIALOG, BROWSER = 100, 200, 201, 300

    def __init__(self) -> None:
        me = os.getpid()
        self.owners = {self.PATCHY: (4242, 1), self.PANEL: (me, 2), self.DIALOG: (me, 2), self.BROWSER: (77, 3)}
        self.fg = self.PANEL
        self.minimized: set[int] = set()
        self.refuse = False  # SetForegroundWindow says no (the attached fallback is tried)
        self.calls: list[tuple[str, int]] = []

    def foreground(self) -> int:
        return self.fg

    def owner(self, hwnd: int) -> tuple[int, int]:
        return self.owners.get(hwnd, (0, 0))

    def usable(self, hwnd: int) -> bool:
        return hwnd in self.owners and hwnd not in self.minimized

    def set_foreground(self, hwnd: int) -> bool:
        self.calls.append(("set", hwnd))
        if self.refuse:
            return False
        self.fg = hwnd
        return True

    def set_foreground_attached(self, hwnd: int) -> bool:
        self.calls.append(("attached", hwnd))
        self.fg = hwnd
        return True


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.win = FakeWin32()
        self.pending: list = []
        self.focus = FocusReturn(
            lambda: FakeWin32.PATCHY,
            lambda: FakeWin32.PANEL,
            win32=self.win,
            later=lambda _delay, fn: self.pending.append(fn),
        )

    def fire(self) -> None:
        for fn in self.pending:
            fn()
        self.pending.clear()

    def test_a_click_in_the_panel_gives_patchy_the_keyboard(self):
        self.assertTrue(self.focus.request())
        self.assertEqual(self.win.calls, [])  # not at once: a moment after the click
        self.fire()
        self.assertEqual(self.win.calls, [("set", FakeWin32.PATCHY)])
        self.assertEqual(self.win.fg, FakeWin32.PATCHY)

    def test_nothing_moves_from_a_dialog_the_page_or_another_program(self):
        for fg in (FakeWin32.DIALOG, FakeWin32.BROWSER, FakeWin32.PATCHY, 0):
            self.win.fg = fg
            self.assertFalse(self.focus.request(), fg)
        self.fire()
        self.assertEqual(self.win.calls, [])

    def test_nothing_moves_when_the_keyboard_went_elsewhere_meanwhile(self):
        self.assertTrue(self.focus.request())
        self.win.fg = FakeWin32.DIALOG  # a dialog opened, or the artist switched programs
        self.fire()
        self.assertEqual(self.win.calls, [])

    def test_nothing_moves_to_a_minimized_or_closed_patchy(self):
        self.win.minimized.add(FakeWin32.PATCHY)
        self.focus.request()
        self.fire()
        gone = FocusReturn(lambda: 0, lambda: FakeWin32.PANEL, win32=self.win, later=lambda _d, fn: fn())
        gone.request()
        self.assertEqual(self.win.calls, [])
        self.assertEqual(self.win.fg, FakeWin32.PANEL)

    def test_the_attached_input_fallback_when_windows_refuses(self):
        self.win.refuse = True
        self.focus.request()
        self.fire()
        self.assertEqual(self.win.calls, [("set", FakeWin32.PATCHY), ("attached", FakeWin32.PATCHY)])

    def test_no_windows_api_no_change(self):
        with mock.patch.object(sys, "platform", "darwin"):
            focus = FocusReturn(lambda: FakeWin32.PATCHY, lambda: FakeWin32.PANEL)
        self.assertIsNone(focus.win32)
        self.assertFalse(focus.request())


class InstallTests(unittest.TestCase):
    def test_manifest_has_patchys_keys_and_uninstall_leaves_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "companions"
            path = install.install(sys.executable, folder)
            entry = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(entry),
                {"id", "name", "menuLabel", "command", "args", "workingDirectory", "minProtocol", "maxProtocol"},
            )
            self.assertEqual(entry["id"], COMPANION_ID)
            self.assertEqual(
                (entry["minProtocol"], entry["maxProtocol"]), (COMPANION_PROTOCOL_MIN, COMPANION_PROTOCOL_MAX)
            )
            self.assertEqual(entry["args"], ["-m", "openfill_patchy"])
            self.assertTrue((Path(entry["workingDirectory"]) / "openfill_patchy" / "__main__.py").is_file())
            self.assertTrue(install.uninstall(folder))
            self.assertFalse(path.exists())
            path.write_text(json.dumps({"id": "io.example.other"}), encoding="utf-8")
            self.assertFalse(install.uninstall(folder))  # not ours: left alone
            self.assertTrue(path.exists())

    def test_folder_is_patchys_app_data(self):
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\a\AppData\Roaming"}),
        ):
            self.assertEqual(
                install.companions_dir(), Path(r"C:\Users\a\AppData\Roaming") / "RTsoft" / "Patchy" / "companions"
            )


class FullJobTests(Fixture):
    """``openfill.app.App`` itself, with the stub generator in place of ComfyUI."""

    def test_generative_fill_inserts_an_inverted_masked_layer_one_undo_removes(self):
        import stub_backend

        from openfill.app import App

        app = App(interactive=False, io=self.bridge, runner_cls=stub_backend.QuietRunner)
        stub_backend.install(app)
        app.settings.seam_correction = app.settings.grain_match = False
        layers = app.generative_fill(self.image, "a red door", "", 1, 1.0, 7, "active")
        self.bridge.finish(self.image)
        self.assertEqual(len(self.target.undo), 1)
        top = self.target.top()
        self.assertEqual(top["id"], layers[0].id)
        pixels = Image.frombytes("RGBA", top["size"], top["pixels"])
        r, g, b, _a = pixels.getpixel((top["size"][0] // 2, top["size"][1] // 2))
        self.assertEqual((r, g, b), (55, 155, 205))  # (200, 100, 50) inverted
        self.assertTrue(top.get("mask"))
        self.client.get_state()
        self.client.undo("7")
        self.assertEqual([r["name"] for r in self.target.layers], ["Background"])


if __name__ == "__main__":
    unittest.main()
