# SPDX-License-Identifier: Apache-2.0
import tempfile
import unittest
from pathlib import Path

from openfill import ui_text as ut
from openfill.backend import (
    BackendError,
    BackendUnavailable,
    GenerationCancelled,
    GenerationTimeout,
    ModelMissing,
    OutOfMemory,
    WorkflowRejected,
    describe_error,
)
from openfill.config import Settings


class HistoryTests(unittest.TestCase):
    def test_remember_newest_first_without_duplicates(self):
        h = ut.remember_prompt([], "a cat")
        h = ut.remember_prompt(h, "a dog")
        h = ut.remember_prompt(h, "  a cat  ")
        self.assertEqual(h, ["a cat", "a dog"])

    def test_limit_and_empty(self):
        h = []
        for i in range(30):
            h = ut.remember_prompt(h, f"p{i}")
        self.assertEqual(len(h), ut.HISTORY_LIMIT)
        self.assertEqual(h[0], "p29")
        self.assertEqual(ut.remember_prompt(h, "   "), h)

    def test_round_trip_and_junk(self):
        h = ["one", "two\nlines", "unicode \u00e9"]
        self.assertEqual(ut.parse_history(ut.dump_history(h)), h)
        for junk in ("", None, "not json", "{}", "[1, 2]", '["", "  "]'):
            self.assertEqual(ut.parse_history(junk), [])

    def test_persisted_in_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            s = Settings()
            s.prompt_history = ut.dump_history(["a red barn", "fog"])
            s.save(path)
            self.assertEqual(ut.parse_history(Settings.load(path).prompt_history), ["a red barn", "fog"])

    def test_initial_prompt_and_label(self):
        self.assertEqual(ut.initial_prompt("", ["last"]), "last")
        self.assertEqual(ut.initial_prompt("given", ["last"]), "given")
        self.assertEqual(ut.initial_prompt(None, []), "")
        self.assertEqual(ut.history_label("a  b\nc"), "a b c")
        label = ut.history_label("x" * 100, 20)
        self.assertEqual(len(label), 20)
        self.assertTrue(label.endswith("..."))


class StatusTextTests(unittest.TestCase):
    def test_plain_step_text(self):
        self.assertEqual(ut.plain_step_text("Generating... step 12/25"), "Generating, step 12 of 25")
        self.assertEqual(
            ut.plain_step_text("Starting the AI backend... 14 s"),
            "Starting the AI backend, 14 s",
        )
        self.assertEqual(ut.plain_step_text("Downloading result..."), "Placing the result")
        self.assertEqual(ut.plain_step_text("Running node 7..."), "Loading the model")
        self.assertEqual(ut.plain_step_text("Uploading selection..."), "Sending the selection to the AI backend")
        self.assertEqual(ut.plain_step_text("Downloading 1.0 GB of 2.0 GB"), "Downloading 1.0 GB of 2.0 GB")
        self.assertEqual(ut.plain_step_text(""), "")

    def test_no_jargon_in_plain_texts(self):
        texts = [
            ut.plain_step_text(t)
            for t in (
                "Generating... step 3/20",
                "Running node 4...",
                "Submitting workflow...",
                "Waiting for the AI backend...",
            )
        ]
        for text in [*texts, ut.FILL_PLACEHOLDER, ut.REMOVE_EXPLANATION, ut.SHORTCUT_HINT]:
            for word in ("cfg", "sampler", "scheduler", "denoise", "node", "workflow"):
                self.assertNotIn(word, text.lower(), text)

    def test_expand_over_limit(self):
        self.assertFalse(ut.expand_over_limit((1000, 800), 250, 200, 250, 200))
        self.assertTrue(ut.expand_over_limit((1000, 800), 251, 0, 0, 0))
        self.assertTrue(ut.expand_over_limit((1000, 800), 0, 0, 0, 201))
        self.assertFalse(ut.expand_over_limit((1000, 800), 0, 200, 0, 0))
        self.assertFalse(ut.expand_over_limit(None, 999, 999, 999, 999))
        self.assertIn("25%", ut.EXPAND_OVER_LIMIT)
        self.assertIn("SD 1.5", ut.expand_model_note("SD 1.5 Inpainting"))

    def test_dialog_status_lines(self):
        self.assertIn("No selection", ut.selection_status(None, (100, 50)))
        self.assertIn("260 x 200", ut.selection_status((260, 200), (1024, 768)))
        self.assertEqual(ut.selection_status((260, 200), (1024, 768)), "Selection: 260 x 200 px")
        self.assertEqual(
            ut.expand_status((1024, 768), 256, 0, 256, 0), "New size: 1536 x 768 px (+256 left, +256 right)"
        )
        self.assertEqual(ut.expand_status((2176, 1440), 0, 0, 256, 0), "New size: 2432 x 1440 px (+256 right)")
        self.assertEqual(ut.expand_status((100, 100), 8, 8, 8, 8), "New size: 116 x 116 px (+8 each side)")
        self.assertIn(ut.EXPAND_NOTHING, ut.expand_status((1024, 768), 0, 0, 0, 0))
        self.assertEqual(ut.expand_status(None, 1, 1, 1, 1), "")
        self.assertIn("the selection", ut.txt2img_status(0, 0, (300, 200), (768, 512), "layer"))
        self.assertIn("768 x 512", ut.txt2img_status(0, 0, None, (768, 512), "image"))
        self.assertIn("new image", ut.txt2img_status(512, 512, None, (768, 512), "image"))
        self.assertEqual(ut.elapsed_text("0:12", "0:08"), "Elapsed 0:12, about 0:08 left")
        self.assertEqual(ut.elapsed_text("0:12", "--:--"), "Elapsed 0:12")

    def test_dialog_texts_are_short_single_lines(self):
        texts = [v for k, v in vars(ut).items() if k.isupper() and isinstance(v, str) and k != "INTERRUPTED_OUTSIDE"]
        texts += list(ut.DIALOG_TITLES.values()) + list(ut.SIDE_LABELS.values())
        texts += [ut.anchor_tip(a) for a in ("center", "top-left", "bottom")]
        texts += [
            ut.picker_heading(3),
            ut.prompt_free_warning("MAT - prompt-free removal"),
            ut.selection_status(None, (4000, 3000)),
            ut.expand_status((4000, 3000), 4096, 4096, 4096, 4000),
            ut.txt2img_status(0, 0, (300, 200), (768, 512), "layer"),
        ]
        for fields in (ut.GENERAL_FIELDS, ut.ADVANCED_FIELDS, ut.CONNECTION_FIELDS):
            texts += [t for _key, label, hint in fields for t in (label, hint) if t]
        for text in texts:
            self.assertNotIn("\n", text, text)
            self.assertLessEqual(len(text), 100, text)
            self.assertFalse(text.lower().startswith(("tip", "note:")), text)
            self.assertNotIn("  ", text, text)

    def test_no_jargon_in_generation_dialogs(self):
        generation = [
            ut.FILL_PLACEHOLDER,
            ut.EXPAND_PLACEHOLDER,
            ut.TXT2IMG_PLACEHOLDER,
            ut.REMOVE_EXPLANATION,
            ut.REMOVE_MODEL_NOTE,
            ut.KEY_HINT,
            ut.SHORTCUT_HINT,
            ut.EMPTY_PROMPT,
            ut.EXPAND_NOTHING,
            ut.TIP_MODEL,
            ut.TIP_VARIATIONS,
            ut.TIP_CHANGE,
            ut.TIP_SEED,
            ut.AVOID_PLACEHOLDER,
            ut.SIZE_AUTO,
            ut.PICKER_HINT,
            ut.PROGRESS_PREVIEW_HINT,
            ut.prompt_free_warning("LaMa"),
        ]
        jargon = ("cfg", "sampler", "scheduler", "denoise", "node", "workflow", "checkpoint", "latent", "vae", "comfy")
        for text in generation:
            for word in jargon:
                self.assertNotIn(word, text.lower(), text)

    def test_expand_placeholder_says_prompt_is_optional(self):
        self.assertEqual(ut.EXPAND_PLACEHOLDER, "Leave empty to continue the scene")

    def test_shortcut_hint(self):
        self.assertIn("Edit > Keyboard Shortcuts", ut.SHORTCUT_HINT)
        self.assertIn(ut.SUGGESTED_SHORTCUT, ut.SHORTCUT_HINT)


class ClampTests(unittest.TestCase):
    def test_off_screen_position_comes_back(self):
        areas = [(0, 0, 1920, 1040)]
        x, y = ut.clamp_position(1694, 2328, 380, 500, areas)  # remembered on a monitor that is gone
        self.assertTrue(0 <= x <= 1920 - 380 and 0 <= y <= 1040 - 500, (x, y))

    def test_inside_is_kept_and_second_monitor_used(self):
        areas = [(0, 0, 1920, 1040), (1920, 0, 2560, 1400)]
        self.assertEqual(ut.clamp_position(100, 100, 380, 500, areas), (100, 100))
        self.assertEqual(ut.clamp_position(2500, 300, 380, 500, areas), (2500, 300))
        x, y = ut.clamp_position(5000, -300, 380, 500, areas)
        self.assertEqual((x, y), (1920 + 2560 - 380 - 8, 8))

    def test_too_big_and_no_monitors(self):
        self.assertEqual(ut.clamp_position(50, 50, 4000, 3000, [(0, 0, 1920, 1040)]), (0, 0))
        self.assertEqual(ut.clamp_position(-5, 7, 10, 10, []), (-5, 7))


class ErrorWordingTests(unittest.TestCase):
    def test_every_backend_error_gets_a_next_step_or_advice(self):
        errors = [
            BackendUnavailable("ComfyUI was not found at 'X'. Install the portable build there."),
            BackendUnavailable("ComfyUI is not answering at http://x and auto-start is disabled in the settings."),
            BackendUnavailable("ComfyUI did not come up at http://x within 240 s."),
            BackendUnavailable("could not download the result: boom"),
            ModelMissing(
                "'FLUX' is not downloaded yet. Open Filters > OpenFill > Generative Fill Settings > Models "
                "and press Download."
            ),
            ModelMissing("unknown thing"),
            OutOfMemory("CUDA"),
            GenerationTimeout("no result after 300 s"),
            WorkflowRejected("KSampler: bad"),
        ]
        for err in errors:
            text = ut.with_next_step(describe_error(err))
            self.assertTrue(text.startswith(describe_error(err).strip()), text)  # original wording kept
            self.assertTrue(any(w in text.lower() for w in ("next step", "open", "try", "press")), text)
            self.assertLessEqual(text.lower().count("generative fill settings"), 1, text)  # one step, not two

    def test_cancel_is_reassuring(self):
        text = ut.with_next_step(describe_error(GenerationCancelled()))
        self.assertIn("Nothing in the image was changed", text)

    def test_refusals_are_shown_as_they_are(self):
        # A missing selection or an indexed image is not a glitch: no "try again", no bug report.
        # The wording of app.NO_SELECTION_MESSAGE and app.INDEXED_MESSAGE (app imports GIMP).
        for text in (
            "Nothing is selected. Select the area to fill first, then run Generative Fill again.",
            "This image uses indexed colors, which cannot hold generated pixels without destroying them. "
            "Convert it with Image > Mode > RGB (or Grayscale) and run the command again.",
        ):
            self.assertEqual(ut.with_next_step(describe_error(BackendError(text))), text)

    def test_not_doubled(self):
        once = ut.with_next_step("The AI backend is not available: x")
        self.assertEqual(ut.with_next_step(once), once)

    def test_save_failed(self):
        text = ut.save_failed("C:/p/config.json", "Permission denied")
        self.assertIn("C:/p/config.json: Permission denied", text)
        self.assertIn("Next step", text)

    def test_labels_match_backend_wording(self):
        self.assertIn('"Start backend automatically"', ut.next_step("auto-start is disabled"))
        self.assertIn("press Check backend", ut.next_step("The AI backend is not available: x"))

    def test_unexpected(self):
        text = ut.unexpected_error("ValueError", "bad value")
        self.assertIn("ValueError: bad value", text)
        self.assertIn("Next step", text)

    def test_health_text(self):
        self.assertEqual(ut.health_text(True, "", 4, True), "Backend OK: 4 models, inpaint nodes installed.")
        self.assertIn("1 model,", ut.health_text(True, "", 1, False))
        bad = ut.health_text(False, "ComfyUI was not found at 'C:\\nope'.", 0, False)
        self.assertTrue(bad.startswith("Could not reach the backend: ComfyUI was not found"), bad)
        self.assertIn("Next step: open Filters > OpenFill > Generative Fill Settings", bad)
        generic = ut.health_text(False, "connection refused", 0, False)
        self.assertIn("Next step: check the backend folder", generic)


class PrimaryButtonTests(unittest.TestCase):
    def row(self, **kw):
        base = {
            "id": "m",
            "installed": False,
            "gated": False,
            "partial": [],
            "missing_models": [],
            "missing_nodes": [],
            "runnable": True,
            "active": False,
        }
        base.update(kw)
        return base

    def test_state_sequence(self):
        self.assertEqual(ut.primary_action(self.row(), None), ("Download", "download", True))
        cur = {"model_id": "m", "type": "download", "done": 43, "total": 100}
        self.assertEqual(ut.primary_action(self.row(), cur), ("Downloading 43% - Cancel", "cancel", True))
        self.assertEqual(ut.primary_action(self.row(installed=True), None), ("Set active", "activate", True))
        self.assertEqual(ut.primary_action(self.row(installed=True, active=True), None), ("Active", None, False))

    def test_other_states(self):
        self.assertEqual(ut.primary_action(self.row(gated=True), None, has_token=False), ("Download", "token", True))
        self.assertEqual(ut.primary_action(self.row(partial=["x"]), None)[0], "Resume download")
        self.assertEqual(
            ut.primary_action(self.row(), {"model_id": "o", "type": "download"}, ["m"]), ("Queued", None, False)
        )
        self.assertEqual(ut.primary_action(self.row(installed=True, missing_nodes=["n"]), None)[1], "nodes")
        self.assertEqual(ut.primary_action(self.row(installed=True, runnable=False), None), ("Downloaded", None, False))
        self.assertEqual(ut.primary_action(self.row(), {"model_id": "m", "type": "install_nodes"})[2], False)

    def test_sort_and_summary(self):
        rows = [self.row(id="a"), self.row(id="b", installed=True), self.row(id="c", installed=True, active=True)]
        ordered = sorted(rows, key=ut.model_sort_key)
        self.assertEqual([r["id"] for r in ordered], ["c", "b", "a"])
        text = ut.model_summary(
            {
                "capabilities": ["fill", "expand", "txt2img"],
                "runnable": True,
                "size": "2.0 GB",
                "min_vram_gb": 4,
                "gated": True,
            }
        )
        self.assertEqual(
            text, "Fill, expand, text to image. 2.0 GB. Needs 4 GB of graphics memory. Needs a free Hugging Face token."
        )
        self.assertIn("Helper file", ut.model_summary({"capabilities": ["patch"], "runnable": False}))
        self.assertEqual(ut.model_option_label("SD", True, True), "SD (active model)")
        self.assertEqual(ut.model_option_label("SD", False, False), "SD (not downloaded)")
        self.assertEqual(ut.model_option_label("MAT - prompt-free removal", True, True), "MAT (active model)")


class ExpandDiscardRestoreTests(unittest.TestCase):
    """``_canvas_restore`` in the plug-in entry file (it imports GIMP, so the
    pure function is compiled on its own from the file's syntax tree)."""

    @classmethod
    def setUpClass(cls):
        import ast

        src = Path(__file__).resolve().parents[1] / "gimp-openfill" / "gimp-openfill.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_canvas_restore")
        namespace: dict = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(src), "exec"), namespace)
        cls.restore = staticmethod(namespace["_canvas_restore"])

    def test_right_margin_only(self):
        # 256 px added on the right: the old content did not move
        self.assertEqual(self.restore((1920, 1440, (0, 0), None), (2176, 1440, (0, 0), None)), (1920, 1440, 0, 0))

    def test_left_and_top_margins_shift_back(self):
        # 128 px left and 64 px top: the old layer moved by (128, 64), resizing back shifts by (-128, -64)
        before = (800, 600, (10, 20), None)
        after = (1056, 728, (138, 84), None)
        self.assertEqual(self.restore(before, after), (800, 600, -128, -64))

    def test_unchanged_or_unknown_canvas_needs_nothing(self):
        self.assertIsNone(self.restore((800, 600, (0, 0), None), (800, 600, (0, 0), None)))
        self.assertIsNone(self.restore(None, (800, 600, (0, 0), None)))
        self.assertIsNone(self.restore((800, 600, (0, 0), None), None))


class DownloadWordingTests(unittest.TestCase):
    def test_unverified_question_names_the_model(self):
        self.assertIn("Some Model", ut.unverified_download_question("Some Model"))

    def test_timeout_message_of_a_download_keeps_its_own_step(self):
        msg = "Could not reach x: the connection timed out. Check it, then press Download again."
        self.assertNotIn("Generation time limit", ut.with_next_step(msg))


class ExpandCropTextTests(unittest.TestCase):
    def test_status_with_a_crop(self):
        self.assertEqual(
            ut.expand_status((1000, 800), -62, 0, -62, 0), "New size: 876 x 800 px (-62 left, -62 right cropped)"
        )
        self.assertEqual(
            ut.expand_status((1000, 800), -100, 0, 300, 0), "New size: 1200 x 800 px (+300 right, -100 left cropped)"
        )

    def test_over_limit_counts_only_expanded_sides(self):
        self.assertFalse(ut.expand_over_limit((1000, 800), -900, -700, 100, 100))
        self.assertTrue(ut.expand_over_limit((1000, 800), -100, 0, 300, 0))

    def test_part_suffix_survives_the_plain_text(self):
        self.assertEqual(
            ut.plain_step_text("Generating... step 5/20 (part 2 of 3)"), "Generating, step 5 of 20 (part 2 of 3)"
        )
        self.assertEqual(ut.plain_step_text("Generating... step 5/20"), "Generating, step 5 of 20")
        self.assertEqual(
            ut.plain_step_text("Uploading (part 1 of 2)"), "Sending the selection to the AI backend (part 1 of 2)"
        )


if __name__ == "__main__":
    unittest.main()


class CpuBackendMessageTests(unittest.TestCase):
    def test_names_the_flag_or_the_pytorch(self):
        self.assertIn("--cpu", ut.cpu_backend_message("--cpu"))
        self.assertIn("Extra ComfyUI options", ut.cpu_backend_message("--cpu"))
        self.assertIn("PyTorch", ut.cpu_backend_message(""))
        for flags in ("", "--cpu"):
            self.assertNotIn("\\u2014", ut.cpu_backend_message(flags))


class HealthDeviceTests(unittest.TestCase):
    def test_device_and_cpu_warning(self):
        gpu = ut.health_text(True, "", 3, True, "NVIDIA GeForce RTX 3060 (cuda, 12.0 GB)", "gpu")
        self.assertIn("Running on: NVIDIA GeForce RTX 3060", gpu)
        self.assertNotIn("processor", gpu)
        cpu = ut.health_text(True, "", 3, True, "cpu (cpu, 31.2 GB)", "cpu")
        self.assertIn("processor", cpu)
        self.assertEqual(ut.health_text(True, "", 4, True), "Backend OK: 4 models, inpaint nodes installed.")


class FluxHintTests(unittest.TestCase):
    def test_short_plain_line(self):
        self.assertLessEqual(len(ut.FLUX_PROMPT_HINT), 100)
        self.assertIn("FLUX", ut.FLUX_PROMPT_HINT)
        self.assertNotIn("—", ut.FLUX_PROMPT_HINT)
