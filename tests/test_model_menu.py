# SPDX-License-Identifier: Apache-2.0
"""The model menus (issue #14, model_menu.py): order, the heading over the user's own model
files, search, and models hidden from the menus (the ``hidden_models`` setting)."""

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from openfill import model_menu
from openfill.config import Settings

LOCAL = [(f"local-{i:012x}", f"juggernautXL_v{i}.safetensors") for i in range(96)]
OPTIONS = [
    ("default", "LaMa (default)"),
    ("mat-places512", "MAT Places512"),
    ("realvisxl-v5", "RealVisXL V5.0"),
    *LOCAL,
]


class ArrangeTests(unittest.TestCase):
    def test_the_tools_own_models_first_then_the_users_files_under_a_heading(self):
        rows = model_menu.arrange(OPTIONS)
        self.assertEqual(rows[0], model_menu.Row("model", "LaMa (default)", "default", False))
        self.assertEqual([r.model_id for r in rows[:3]], ["default", "mat-places512", "realvisxl-v5"])
        self.assertEqual(rows[3], model_menu.Row("head", model_menu.OWN_FILES))
        self.assertEqual(len(rows), len(OPTIONS) + 1)
        self.assertTrue(all(r.kind == "model" and model_menu.own_file(r.model_id) for r in rows[4:]))

    def test_the_users_files_keep_their_place_when_the_tool_has_none_of_its_own(self):
        rows = model_menu.arrange(LOCAL[:3])
        self.assertEqual([r.kind for r in rows], ["model"] * 3)  # no heading over a list of one kind
        mixed = [LOCAL[0], ("lama-big", "LaMa"), LOCAL[1]]
        self.assertEqual([r.model_id for r in model_menu.arrange(mixed)], ["lama-big", "", LOCAL[0][0], LOCAL[1][0]])

    def test_hidden_models_are_left_out_unless_shown_and_the_one_in_use_always_stays(self):
        hidden = {"mat-places512", LOCAL[5][0], "default"}
        rows = model_menu.arrange(OPTIONS, hidden, keep="default")
        ids = [r.model_id for r in rows]
        self.assertIn("default", ids)  # in use: listed although hidden
        self.assertTrue(next(r for r in rows if r.model_id == "default").hidden)
        self.assertNotIn("mat-places512", ids)
        self.assertNotIn(LOCAL[5][0], ids)
        shown = model_menu.arrange(OPTIONS, hidden, keep="default", show_hidden=True)
        self.assertEqual(len(shown), len(OPTIONS) + 1)
        self.assertTrue(next(r for r in shown if r.model_id == "mat-places512").hidden)
        self.assertEqual(model_menu.hidden_count(OPTIONS, hidden, keep="default"), 2)

    def test_search_keeps_the_models_with_every_word_in_any_case(self):
        rows = model_menu.arrange(OPTIONS, query="lama")
        self.assertEqual([r.label for r in rows], ["LaMa (default)"])
        rows = model_menu.arrange(OPTIONS, query="JUGGERNAUT v12")
        self.assertEqual([r.model_id for r in rows], [LOCAL[12][0]])  # the heading goes with nothing above it
        self.assertEqual(model_menu.arrange(OPTIONS, query="no such model"), [])
        self.assertEqual(model_menu.arrange(OPTIONS, query="   "), model_menu.arrange(OPTIONS))
        self.assertTrue(model_menu.matches("RealVisXL V5.0 (SDXL)", "sdxl real"))
        self.assertFalse(model_menu.matches("RealVisXL V5.0", "real flux"))


class HiddenSettingTests(unittest.TestCase):
    def test_parse_dump_and_set(self):
        self.assertEqual(model_menu.parse_hidden(""), [])
        self.assertEqual(model_menu.parse_hidden("not json"), [])
        self.assertEqual(model_menu.parse_hidden('{"a": 1}'), [])
        self.assertEqual(model_menu.parse_hidden('["a", "", 3, "a", "b"]'), ["a", "b"])
        self.assertEqual(model_menu.dump_hidden([]), "")
        text = model_menu.set_hidden("", "local-1", True)
        self.assertEqual(json.loads(text), ["local-1"])
        text = model_menu.set_hidden(text, "mat-places512", True)
        self.assertEqual(
            model_menu.parse_hidden(model_menu.set_hidden(text, "mat-places512", True)), ["local-1", "mat-places512"]
        )
        self.assertEqual(
            model_menu.set_hidden(model_menu.set_hidden(text, "local-1", False), "mat-places512", False), ""
        )

    def test_the_setting_is_saved_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings = Settings()
            self.assertEqual(settings.hidden_models, "")
            settings.hidden_models = model_menu.set_hidden("", "local-abc", True)
            settings.save(path)
            self.assertEqual(model_menu.parse_hidden(Settings.load(path).hidden_models), ["local-abc"])

    def test_the_app_hides_and_lists_again_and_saves(self):
        from openfill.app import App

        with tempfile.TemporaryDirectory() as tmp:
            app = App.__new__(App)
            app.settings = Settings()
            app.paths = type("P", (), {"config": Path(tmp) / "config.json"})()
            app.log = logging.getLogger("test")
            self.assertEqual(app.hidden_models(), [])
            self.assertEqual(app.set_model_hidden("local-abc", True), ["local-abc"])
            self.assertEqual(app.set_model_hidden("lama-big", True), ["local-abc", "lama-big"])
            self.assertEqual(
                model_menu.parse_hidden(Settings.load(app.paths.config).hidden_models), ["local-abc", "lama-big"]
            )
            self.assertEqual(app.set_model_hidden("local-abc", False), ["lama-big"])
            self.assertEqual(app.hidden_models(), ["lama-big"])


@unittest.skipUnless(os.environ.get("OPENFILL_TK_TESTS"), "opens GTK windows: set OPENFILL_TK_TESTS=1 (under Xvfb)")
class GimpModelPickerTests(unittest.TestCase):
    """The GIMP dialogs' Model field with a hundred models (GTK 3, as GIMP 3 has it)."""

    def setUp(self):
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk

            from openfill import dialogs
        except (ImportError, ValueError):
            self.skipTest("no GTK 3")
        self.Gtk, self.dialogs = Gtk, dialogs
        self.saved = []
        options = [("active", "DreamShaper 8 (active model)"), *OPTIONS[1:]]
        self.dialog = dialogs.GenerationDialog(
            "fill",
            {"prompt": "", "model": "active", "variations": 1},
            [],
            options,
            hidden_models=["mat-places512"],
            on_hide_model=lambda i, h: self.saved.append((i, h)),
        )
        self.addCleanup(self.dialog.destroy)
        self.picker = self.dialog.model_combo

    def pump(self, seconds=0.0):
        import time

        end = time.monotonic() + seconds
        while True:
            while self.Gtk.events_pending():
                self.Gtk.main_iteration()
            if time.monotonic() >= end:
                return
            time.sleep(0.02)

    def test_search_hide_and_pick(self):
        picker = self.picker
        self.assertEqual(picker.get_active_id(), "active")
        self.assertNotIn("mat-places512", [r.model_id for r in picker.rows()])
        picker.clicked()
        self.pump()
        chooser = picker.chooser
        self.assertTrue(chooser.search.get_visible())
        self.assertLessEqual(chooser.get_allocated_height(), chooser.MAX_HEIGHT)
        chooser.search.set_text("v42")
        self.pump(0.4)  # GTK waits a moment before search-changed
        self.assertEqual([r.model_id for r in chooser.rows], [LOCAL[42][0]])
        chooser.search.set_text("")
        self.pump(0.4)
        item = next(r for r in chooser.rows if r.model_id == "realvisxl-v5")
        hide = next(w for w in item.get_child().get_children() if isinstance(w, self.Gtk.Button))
        hide.clicked()
        self.pump()
        self.assertEqual(self.saved, [("realvisxl-v5", True)])
        self.assertNotIn("realvisxl-v5", [r.model_id for r in chooser.rows])
        chooser.show_hidden.set_active(True)
        self.pump()
        self.assertIn("mat-places512", [r.model_id for r in chooser.rows])
        chooser.search.set_text("mat places")
        self.pump(0.4)
        chooser.search.emit("activate")  # Enter in the search field picks the first match
        self.pump()
        self.assertEqual(picker.get_active_id(), "mat-places512")
        self.assertIsNone(picker.chooser)
        self.assertEqual(self.dialog.collect()["model"], "mat-places512")


if __name__ == "__main__":
    unittest.main()
