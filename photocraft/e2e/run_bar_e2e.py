# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""End-to-end check of the OpenFill bar and the Filter > Plug-ins entries (not a unit test).

    python photocraft/e2e/run_bar_e2e.py [--out DIR] [--display :87]

Needs a PhotoCraft started by the OpenFill launcher (its control channel on 7878) and no
companion running (stop it first: this script is the companion). It builds the same
service, watcher and bar the launcher does, then acts like a user: picks OpenFill's
entries from PhotoCraft's Filter menu, types into the bar and presses its buttons.
Checks the document's layers and history through the control channel, and saves
screenshots of the screen (X11 only).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "gimp-openfill")]

from openfill_photocraft import menu  # noqa: E402
from openfill_photocraft.bar import Bar  # noqa: E402
from openfill_photocraft.bridge import PhotoCraftBridge, default_profile  # noqa: E402
from openfill_photocraft.control import ControlClient  # noqa: E402
from openfill_photocraft.service import Service  # noqa: E402
from openfill_photocraft.ui import BAD  # noqa: E402

PROFILE = default_profile()
report: list[dict] = []


def record(case: str, ok: bool, **data) -> None:
    entry = {"case": case, "ok": ok, **data}
    report.append(entry)
    print(json.dumps(entry)[:500], flush=True)


class Scenario:
    def __init__(self, bar: Bar, pc: ControlClient, out: Path, display: str) -> None:
        self.bar, self.pc, self.out, self.display = bar, pc, out, display

    # helpers ------------------------------------------------------------------------

    def ui(self, fn, *args):
        """Run ``fn`` on the bar's thread and wait for its result."""
        done = threading.Event()
        box: dict = {}

        def call() -> None:
            try:
                box["value"] = fn(*args)
            except BaseException as err:  # noqa: BLE001
                box["error"] = err
            done.set()

        self.bar.call_soon(call)
        if not done.wait(30):
            raise TimeoutError("the bar did not answer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def shot(self, name: str) -> None:
        try:
            from PIL import ImageGrab

            ImageGrab.grab(xdisplay=self.display).save(self.out / f"{name}.png")
        except Exception as err:  # noqa: BLE001
            print("screenshot failed:", err)

    def history(self) -> list[str]:
        return self.pc.run("document.inspect")["history"]

    def layers(self) -> list[dict]:
        return self.pc.run("layer.list", flat=True)["layers"]

    def menu_pick(self, action: str) -> None:
        """Filter > Plug-ins > OpenFill, then the bar's tab (and Remove's button) as a user would."""
        self.pc.call("ui.menu.invoke", id=f"plugin.filter.{menu.PLUGIN_PREFIX}panel")
        self.wait_for(lambda: self.ui(lambda: self.bar.root.state() == "normal"))
        if action != "panel":
            self.ui(lambda: self.bar.tab_buttons[action].invoke())
        if action == "remove":
            self.ui(lambda: self.bar.go.invoke())

    def wait_job(self, timeout: float = 900) -> dict:
        t0 = time.time()
        while time.time() - t0 < 5 and not self.bar.service.busy():
            time.sleep(0.1)
        shot_taken = False
        while time.time() - t0 < timeout:
            st = self.bar.service.state.snapshot()
            if st["state"] == "running" and st["previewSeq"] > 2 and not shot_taken and st["fraction"] > 0.4:
                self.shot(f"{self.case}-running")
                shot_taken = True
            if not self.bar.service.busy() and st["state"] != "running":
                st["seconds"] = round(time.time() - t0, 1)
                return st
            time.sleep(0.5)
        raise TimeoutError("job")

    def wait_for(self, check, timeout: float = 10.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if check():
                return True
            time.sleep(0.2)
        return False

    # the run --------------------------------------------------------------------------

    def run(self) -> None:
        for step in (
            self.open_image,
            self.panel_layout,
            self.fill_from_menu,
            self.pick_and_keep,
            self.remove_from_menu,
            self.fill_needs_selection,
            self.expand_preset,
            self.create_image,
            self.hide_and_panel,
            self.strip_and_header,
        ):
            self.case = step.__name__
            try:
                step()
            except Exception as err:  # noqa: BLE001
                record(self.case, False, error=f"{err}", trace=traceback.format_exc()[-800:])
                self.shot(f"{self.case}-error")
        (self.out / "report.json").write_text(json.dumps(report, indent=2))
        bad = [r["case"] for r in report if not r["ok"]]
        print("FAILED:" if bad else "ALL DONE", bad, flush=True)
        self.bar.call_soon(self.bar.root.quit)

    def open_image(self) -> None:
        for _ in range(10):
            if not self.pc.call("ui.inspect")["session"]["documents"]:
                break
            self.pc.run("file.close")
        self.bar.service.bridge.open_file(str(PROFILE / "exchange" / "porsche-v2.png"))
        ok = self.wait_for(lambda: (self.bar.watcher.snapshot().get("document") or {}).get("name") == "porsche-v2.png")
        record("open_image", ok, context=self.ui(lambda: self.bar.context.cget("text")))

    def fill_from_menu(self) -> None:
        self.pc.run("select.rect", x=60, y=60, width=380, height=300)
        before = self.history()
        self.menu_pick("fill")
        shown = self.wait_for(
            lambda: self.ui(lambda: self.bar.tab.get() == "fill" and self.bar.root.state() == "normal")
        )
        no_step = self.wait_for(lambda: self.history() == before, 3)
        focus = self.ui(lambda: str(self.bar.root.focus_get()))
        self.shot("fill-menu-opened")

        def type_and_go() -> None:
            self.bar.prompt.set("a red and yellow hot air balloon")
            self.bar.var_buttons[2].invoke()  # 3 variations
            self.bar.go.invoke()

        self.ui(type_and_go)
        st = self.wait_job()
        added = self.history()[len(before) :]
        self.shot("fill-done")
        thumbs = self.ui(lambda: len(self.bar.thumb_cells) == 3 and self.bar.thumbs.winfo_ismapped())
        footer = self.ui(lambda: [b.label for b in self.bar.actions.pack_slaves()])
        record(
            "fill_from_menu",
            shown and no_step and st["state"] == "done" and added == ["Duplicate Layer"] and len(st["variations"]) == 3,
            menu_step_removed=no_step,
            focus=focus,
            state=st["state"],
            message=st["message"],
            seconds=st["seconds"],
            history_added=added,
            variations=len(st["variations"]),
            thumbnails_shown=thumbs,
            footer=footer,
        )

    def pick_and_keep(self) -> None:
        def click_second() -> None:
            self.bar.thumb_cells[1].event_generate("<Button-1>", x=10, y=10)

        self.ui(click_second)
        names = self.bar.service.state.snapshot()["variations"]
        shown = self.wait_for(
            lambda: [r["name"] for r in self.layers() if r.get("visible") and r["name"] in names] == [names[1]]
        )
        self.shot("variation-2")
        self.ui(lambda: self.bar.keep_button.invoke())
        left = self.wait_for(lambda: sum(1 for r in self.layers() if r["name"] in names) == 1)
        record("pick_and_keep", shown and left, kept=[r["name"] for r in self.layers() if r["name"] in names])

    def remove_from_menu(self) -> None:
        self.pc.run("select.rect", x=470, y=470, width=700, height=360, ellipse=True)
        before = self.history()
        self.menu_pick("remove")
        st = self.wait_job()
        added = self.history()[len(before) :]
        self.shot("remove-done")
        record(
            "remove_from_menu",
            st["state"] == "done" and added == ["Duplicate Layer"],
            state=st["state"],
            message=st["message"],
            seconds=st["seconds"],
            history_added=added,
            top=self.layers()[0]["name"],
        )

    def fill_needs_selection(self) -> None:
        self.pc.run("select.deselect")
        self.wait_for(lambda: self.bar.watcher.snapshot().get("selection") is None)
        self.ui(lambda: (self.bar.select_tab("fill"), self.bar.go.invoke()))
        text, color = self.ui(lambda: (self.bar.context.cget("text"), self.bar.context.cget("fg")))
        record(
            "fill_needs_selection",
            "Select an area" in text and color == BAD and not self.bar.service.busy(),
            message=text,
            color=color,
        )

    def expand_preset(self) -> None:
        d = self.pc.run("document.inspect")
        w, h = d["width"], d["height"]
        self.menu_pick("expand")
        self.wait_for(lambda: self.ui(lambda: self.bar.tab.get() == "expand"))
        self.ui(lambda: (self.bar.prompt.set(""), self.bar.expand_view.ratio_buttons["16:9"].invoke()))
        size_text = self.ui(lambda: self.bar.expand_view.size.cget("text"))
        # the editor's thumbnail is read through a short-lived copy of the document: wait for it
        self.wait_for(lambda: not self.bar._thumb_busy and not self.bar.expand_view.needs_thumbnail, 30)
        before = self.history()
        self.ui(lambda: self.bar.go.invoke())
        st = self.wait_job()
        d = self.pc.run("document.inspect")
        added = self.history()[len(before) :]
        self.shot("expand-done")
        want = (round(h * 16 / 9), h) if w * 9 < h * 16 else (w, round(w * 9 / 16))
        record(
            "expand_preset",
            st["state"] == "done" and (d["width"], d["height"]) == want and len(added) == 2,
            size_text=size_text,
            canvas=[d["width"], d["height"]],
            expected=list(want),
            history_added=added,
            seconds=st["seconds"],
            message=st["message"],
        )

    def create_image(self) -> None:
        docs = len(self.pc.call("ui.inspect")["session"]["documents"])
        self.menu_pick("txt2img")
        self.wait_for(lambda: self.ui(lambda: self.bar.tab.get() == "txt2img"))

        def go() -> None:
            self.bar.prompt.set("a lighthouse on a cliff at sunset, photo")
            self.bar.size_buttons["Portrait"].invoke()
            self.bar.var_buttons[0].invoke()
            if self.bar.target.get() != "image":
                self.bar.toggle_target()
            self.bar.go.invoke()

        self.ui(go)
        st = self.wait_job()
        d = self.pc.run("document.inspect")
        self.shot("create-done")
        now = len(self.pc.call("ui.inspect")["session"]["documents"])
        # The model's size limit may scale it down (SDXL: 1024 on the long side); the shape stays.
        portrait = d["height"] > d["width"] and abs(d["width"] / d["height"] - 832 / 1216) < 0.03
        record(
            "create_image",
            st["state"] == "done" and now == docs + 1 and portrait,
            size=[d["width"], d["height"]],
            documents=[docs, now],
            seconds=st["seconds"],
            message=st["message"],
        )

    def hide_and_panel(self) -> None:
        self.bar.prefs["told_reopen"] = True  # no message box in a test
        self.ui(self.bar.hide)
        hidden = self.ui(lambda: self.bar.root.state())
        self.menu_pick("panel")
        back = self.wait_for(lambda: self.ui(lambda: self.bar.root.state()) == "normal")
        record("hide_and_panel", hidden == "withdrawn" and back, hidden=hidden)

    def panel_layout(self) -> None:
        """The redesigned panel: no footer chips, one Model select without "(active)", 1 to 5
        variations, the action at the bottom, and Models and settings behind the gear."""

        def look() -> dict:
            bar = self.bar
            bar.select_tab("fill")
            out = {
                "status": bar.status.cget("text"),
                "variations": [k for k, _label in bar.var_seg.items],
                "models": list(bar.model_box.values),
                "footer": [b.label for b in bar.actions.pack_slaves()],
                "title_bar": bar.frameless,
            }
            bar.select_view("models")
            bar.root.update_idletasks()
            out["gear"] = (bar.view, bar.gear.section, bar.gear_button.active, bar.footer.winfo_manager() == "")
            bar.select_view("engine")
            out["engine_footer"] = [b.label for b in bar.actions.pack_slaves()]
            bar.select_tab("fill")
            out["back"] = (bar.view, bar.gear_button.active)
            return out

        self.wait_for(lambda: bool(self.bar.refresher.get().get("models")), 30)
        got = self.ui(look)
        ok = (
            got["variations"] == [1, 2, 3, 4, 5]
            and got["models"]
            and not any("(active)" in m for m in got["models"])
            and got["footer"] == ["Generate"]
            and got["gear"] == ("settings", "models", True, True)
            and got["engine_footer"] == ["Save"]
            and got["back"] == ("", False)
        )
        record("panel_layout", ok, **got)

    def strip_and_header(self) -> None:
        """Collapse to the strip and open it again; drag the panel by its header."""
        self.ui(self.bar.collapse)
        collapsed = self.wait_for(
            lambda: self.ui(lambda: self.bar.pill.winfo_viewable() and not self.bar.root.winfo_viewable())
        )
        strip = self.ui(lambda: (self.bar.pill.winfo_width(), self.bar.pill.winfo_height(), self.bar.pill_text))
        self.shot("strip")
        self.ui(self.bar.expand)
        opened = self.wait_for(lambda: self.ui(lambda: self.bar.root.winfo_viewable()))

        def drag() -> tuple:
            bar = self.bar
            x0, y0 = bar.root.winfo_x(), bar.root.winfo_y()
            hx, hy = bar.header.winfo_rootx() + 200, bar.header.winfo_rooty() + 20
            bar.header.event_generate("<ButtonPress-1>", x=200, y=20, rootx=hx, rooty=hy)
            bar.header.event_generate("<B1-Motion>", x=160, y=0, rootx=hx - 40, rooty=hy - 20)
            bar.header.event_generate("<ButtonRelease-1>", x=160, y=0, rootx=hx - 40, rooty=hy - 20)
            bar.root.update()
            return x0, y0, bar.root.winfo_x(), bar.root.winfo_y()

        x0, y0, x1, y1 = self.ui(drag)
        record(
            "strip_and_header",
            collapsed and opened and strip[:2] == (236, 36) and (x0 - x1, y0 - y1) == (40, 20),
            strip=list(strip),
            moved=[x1 - x0, y1 - y0],
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE / "out-bar"))
    parser.add_argument("--display", default=":87")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pc = ControlClient(7878, PROFILE / "control.token")
    exchange = PROFILE / "exchange"
    bridge = PhotoCraftBridge(pc, exchange, PROFILE, read_root=Path.home())
    service = Service(pc, bridge)
    holder: dict = {}
    watcher = menu.Watcher(pc, lambda action: holder["bar"].call_soon(holder["bar"].show, action))
    service.watcher = watcher
    pc.on_wait = watcher.set_waiting
    menu.install(pc)
    watcher.start()
    bar = holder["bar"] = Bar(service, watcher, "http://127.0.0.1:1/", PROFILE)
    scenario = Scenario(bar, pc, out, args.display)
    threading.Thread(target=scenario.run, daemon=True).start()
    bar.root.mainloop()
    watcher.stop()
    return 1 if any(not r["ok"] for r in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
