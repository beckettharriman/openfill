# SPDX-License-Identifier: Apache-2.0
"""Remove object at the image's own resolution: OpenFill's LaMa node, its plan, its
install, and the erase-trained models."""

import importlib.util
import itertools
import tempfile
import unittest
from pathlib import Path

from openfill import catalog as cat
from openfill import downloads
from openfill import geometry as g
from openfill import workflow as w
from openfill.backend import ModelSpec, Sampling
from openfill.comfy_nodes import plan as native

ROOT = Path(__file__).resolve().parent.parent
LAMA = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt")
MAT = ModelSpec(arch="inpaint_model", inpaint_model="MAT_Places512_G_fp16.safetensors")
GB = 1024**3
# What an 8 GB card has left for LaMa's working pixels (measured: about 6.2 MP on an
# RTX 2070 Max-Q with the desktop running; 650 bytes per pixel).
BUDGET_8GB = 6_200_000


class NativePlanTests(unittest.TestCase):
    def test_one_pass_at_full_size_when_it_fits(self):
        # the front wheel of the 4344 x 3258 px car: a 1300 x 1360 px context
        p = native.plan_native(1300, 1360, native.Box(292, 292, 716, 776), BUDGET_8GB)
        self.assertTrue(p.one_pass)
        self.assertEqual(p.scale, 1.0)
        self.assertEqual((p.work_w, p.work_h), (1304, 1360))  # padded to a multiple of 8, not resized

    def test_never_below_one_to_one_while_windows_can_see_across_the_hole(self):
        for budget in (1_200_000, 2_000_000, 4_000_000, BUDGET_8GB):
            p = native.plan_native(1300, 1360, native.Box(292, 292, 716, 776), budget)
            self.assertEqual(p.scale, 1.0, budget)
            for b in p.windows:
                self.assertLessEqual(native.padded(b.w) * native.padded(b.h), budget, budget)

    def test_windows_cover_the_hole_overlap_and_stay_inside(self):
        hole = native.Box(400, 300, 1500, 600)
        p = native.plan_native(3000, 1400, hole, 1_000_000, overlap=200)
        self.assertGreater(len(p.windows), 1)
        self.assertEqual(p.scale, 1.0)
        for b in p.windows:
            self.assertTrue(b.x >= 0 and b.y >= 0 and b.right <= 3000 and b.bottom <= 1400)
        for y in range(hole.y, hole.bottom, 7):
            for x in range(hole.x, hole.right, 7):
                self.assertTrue(any(b.x <= x < b.right and b.y <= y < b.bottom for b in p.windows), (x, y))
        xs = sorted({b.x for b in p.windows})
        width = p.windows[0].w
        for a, b in itertools.pairwise(xs):
            self.assertGreaterEqual(a + width - b, 200)  # neighbours overlap by at least the overlap

    def test_too_wide_a_hole_runs_once_at_the_largest_scale_that_fits(self):
        # the whole car on the 4344 x 3258 px image: the hole is wider than any window
        p = native.plan_native(4344, 3258, native.Box(522, 1032, 3246, 1896), BUDGET_8GB)
        self.assertTrue(p.one_pass)
        self.assertLessEqual(p.work_w * p.work_h, BUDGET_8GB)
        self.assertGreater(p.scale, 0.6)  # about 2870 px wide; the old node worked at 256
        self.assertGreater(min(p.work_w, p.work_h), 512)
        self.assertEqual((p.work_w % 8, p.work_h % 8), (0, 0))

    def test_no_hole_is_one_pass(self):
        self.assertTrue(native.plan_native(5000, 5000, None, 1000).one_pass)

    def test_greedy_order_starts_with_the_most_known_pixels(self):
        windows = [native.Box(0, 0, 10, 10), native.Box(10, 0, 10, 10), native.Box(20, 0, 10, 10)]
        known = {0: 0.2, 1: 0.9, 2: 0.5}
        self.assertEqual(native.window_order(windows, lambda i, _done: known[i]), [1, 2, 0])


class BlendTests(unittest.TestCase):
    """The node's window blend (needs torch; skipped without it)."""

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("torch") is None:
            raise unittest.SkipTest("no torch")
        import torch

        cls.torch = torch
        spec = importlib.util.spec_from_file_location(
            "ofnodes", ROOT / "gimp-openfill/openfill/comfy_nodes/__init__.py"
        )
        cls.nodes = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.nodes)

    def test_box_mean_and_dilation(self):
        torch = self.torch
        m = torch.zeros(1, 1, 40, 50)
        m[:, :, 10:20, 15:25] = 1
        self.assertEqual(float(self.nodes._dilate(m, 3).sum()), 16 * 16)
        ref = torch.nn.functional.avg_pool2d(m, 7, 1, 3, count_include_pad=False)
        self.assertLess(float((self.nodes._box(m, 3) - ref).abs().max()), 1e-6)

    def test_windows_blend_without_touching_known_pixels(self):
        torch = self.torch

        class Fake:  # paints every hole pixel with the window's own colour
            calls = 0

            def __call__(self, img, mask):
                Fake.calls += 1
                return img * (1 - mask) + mask * (0.2 * Fake.calls)

        img = torch.rand(1, 3, 200, 300)
        msk = torch.zeros(1, 1, 200, 300)
        msk[:, :, 80:120, 30:270] = 1
        p = native.plan_native(300, 200, native.Box(30, 80, 240, 40), 160 * 168, overlap=64)
        self.assertGreater(len(p.windows), 1)
        out = self.nodes.OpenFillLamaNative()._windows(img, msk, p, Fake(), torch.device("cpu"), torch.float32, native)
        self.assertTrue(torch.equal(out * (1 - msk), img * (1 - msk)))  # outside the hole: untouched
        self.assertTrue(bool(((out - img).abs().sum(dim=1) > 0)[0][msk[0, 0] > 0].all()))  # all filled
        # the second window's colour reaches into the first's part over a ramp, not a hard edge
        row = out[0, 0, 100, 30:270]
        steps = (row[1:] - row[:-1]).abs()
        self.assertLess(float(steps.max()), 0.1)


class NativeGraphTests(unittest.TestCase):
    def _req(self, model, plan):
        return w.PlannedInpaintRequest(
            image_png=b"",
            mask_png=b"",
            width=plan.context.width,
            height=plan.context.height,
            work_width=plan.work.width,
            work_height=plan.work.height,
            prompt="",
            negative="",
            model=model,
            sampling=Sampling(steps=20, batch=1, seed=5),
            grow=48,
            tiles=plan.tiles,
        )

    def test_lama_runs_at_full_size_through_openfills_node(self):
        plan = g.plan_native_removal(g.Rect(570, 1080, 3150, 1800), g.Rect(0, 0, 4344, 3258))
        self.assertEqual(plan.work.scale, 1.0)
        self.assertEqual((plan.work.width, plan.work.height), (plan.context.width, plan.context.height))
        graph = w.build_fill(self._req(LAMA, plan), "i", "m", 3, w.Capabilities(True, native_lama=True))
        classes = [n["class_type"] for n in graph.values()]
        self.assertIn(w.NATIVE_LAMA_NODE, classes)
        self.assertNotIn("INPAINT_InpaintWithModel", classes)
        self.assertNotIn("ImageScale", classes)  # never resized
        self.assertEqual(graph["mask_grow"]["inputs"]["blur"], 0)  # hard, grown mask as before
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], ["remove", 0])

    def test_without_the_node_lama_uses_comfyui_inpaint_nodes(self):
        plan = g.plan_native_removal(g.Rect(1700, 1960, 620, 680), g.Rect(0, 0, 4344, 3258))
        graph = w.build_fill(self._req(LAMA, plan), "i", "m", 3, w.Capabilities(True))
        self.assertEqual(graph["remove"]["class_type"], "INPAINT_InpaintWithModel")

    def test_mat_keeps_its_512_px_node(self):
        plan = g.plan_fill(
            g.Rect(1700, 1960, 620, 680), g.Rect(0, 0, 4344, 3258), g.INPAINT_MODEL, "inpaint_model", True, 0.5
        )
        graph = w.build_fill(self._req(MAT, plan), "i", "m", 3, w.Capabilities(True, native_lama=True))
        self.assertEqual(graph["remove"]["class_type"], "INPAINT_InpaintWithModel")
        self.assertFalse(MAT.native_lama)
        self.assertTrue(LAMA.native_lama)


class EraserTests(unittest.TestCase):
    def setUp(self):
        self.entries = cat.load_catalog(ROOT / "models" / "catalog.json")
        self.rorem = cat.by_id(self.entries, "rorem-sdxl")
        self.flux = cat.by_id(self.entries, "flux1-fill-object-removal-lora")

    def test_erasers_are_in_the_catalog_with_files_sizes_and_licenses(self):
        for e in (self.rorem, self.flux):
            self.assertIn("remove", e.capabilities)
            self.assertIn(e.template, cat.ERASER_TEMPLATES)
            self.assertEqual(e.family, "removal")
            self.assertTrue(all(f.size and f.sha256 for f in e.files))
            self.assertLessEqual(e.min_vram_gb, 8)
        self.assertIn("Apache-2.0", self.rorem.license_name)
        self.assertIn("Non-Commercial", self.flux.license_name)  # FLUX.1 [dev] terms carry over
        self.assertEqual(self.flux.requires_models, ("flux1-fill-dev-gguf-q4",))
        self.assertGreater(self.rorem.total_size, 6 * GB)

    def test_lama_needs_openfills_node_pack(self):
        lama = cat.by_id(self.entries, "lama-big")
        bundled = [n for n in lama.requires_nodes if n.bundled]
        self.assertEqual([n.folder for n in bundled], ["openfill-nodes"])
        self.assertEqual(bundled[0].commit, downloads.bundled_node_version())

    def test_flux_eraser_runs_on_the_fill_model_files(self):
        spec = cat.model_spec_for(self.flux, self.entries)
        base = cat.model_spec_for(cat.by_id(self.entries, "flux1-fill-dev-gguf-q4"), self.entries)
        self.assertEqual((spec.unet, spec.clip1, spec.clip2, spec.vae), (base.unet, base.clip1, base.clip2, base.vae))
        self.assertEqual(spec.lora, "objectRemovalv1_r32.safetensors")
        self.assertTrue(spec.eraser)
        self.assertFalse(spec.prompt_free)

    def _graph(self, entry, plan):
        spec = cat.model_spec_for(entry, self.entries)
        prompt, negative = w.ERASER_PROMPTS[spec.effective_template]
        req = w.PlannedInpaintRequest(
            image_png=b"",
            mask_png=b"",
            width=plan.context.width,
            height=plan.context.height,
            work_width=plan.work.width,
            work_height=plan.work.height,
            prompt=prompt,
            negative=negative,
            model=spec,
            sampling=Sampling(steps=25, cfg=30.0 if spec.arch == "flux" else 1.0, batch=1, seed=5),
            tiles=plan.tiles,
            full_grow=24,
            full_blur=12,
        )
        return w.build_fill(req, "i", "m", 3, w.Capabilities(True))

    def test_rorem_graph(self):
        plan = g.plan_fill(g.Rect(400, 300, 300, 200), g.Rect(0, 0, 1448, 1086), g.SDXL, "sdxl", True, 0.5)
        graph = self._graph(self.rorem, plan)
        self.assertEqual(graph["unet"]["class_type"], "UNETLoader")
        self.assertEqual(graph["clip"]["inputs"]["type"], "sdxl")
        self.assertEqual(graph["encode"]["class_type"], "InpaintModelConditioning")
        self.assertEqual(graph["sampler"]["inputs"]["cfg"], 1.0)
        self.assertIn("photorealistic", graph["pos"]["inputs"]["text"])

    def test_flux_eraser_graph(self):
        plan = g.plan_fill(g.Rect(400, 300, 300, 200), g.Rect(0, 0, 1448, 1086), g.FLUX, "flux", True, 0.5)
        graph = self._graph(self.flux, plan)
        self.assertEqual(graph["lora"]["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(graph["lora"]["inputs"]["model"], ["unet", 0])
        self.assertEqual(graph["guidance"]["class_type"], "FluxGuidance")
        self.assertEqual(graph["sampler"]["inputs"]["cfg"], 1.0)  # FLUX guidance goes through FluxGuidance

    def test_erasers_tile_large_selections_at_full_resolution(self):
        plan = g.plan_fill(
            g.Rect(570, 1080, 3150, 1800), g.Rect(0, 0, 4344, 3258), g.SDXL, "sdxl", True, 0.5, min_scale=1.0
        )
        self.assertGreater(len(plan.tiles), 1)
        self.assertEqual(plan.tile_scale, 1.0)
        graph = self._graph(self.rorem, plan)
        n = len(plan.tiles)
        self.assertEqual(sum(1 for v in graph.values() if v["class_type"] == "KSampler"), n)
        self.assertEqual(graph["t0_enc"]["class_type"], "InpaintModelConditioning")
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], [f"t{n - 1}_paste", 0])


class BundledNodeInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nodes = Path(self.tmp.name)
        self.spec = cat.NodeSpec(cat.BUNDLED_REPO, "openfill-nodes", downloads.bundled_node_version())

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_version_and_status(self):
        self.assertTrue(cat.node_missing(self.nodes, self.spec))
        target = downloads.install_custom_node(
            self.spec.repo, self.spec.folder, self.spec.commit, self.nodes, Path("python")
        )
        self.assertTrue((target / "__init__.py").is_file())
        self.assertTrue((target / "plan.py").is_file())
        self.assertFalse((target / "__pycache__").exists())
        self.assertFalse(cat.node_missing(self.nodes, self.spec))
        # an older copy counts as missing and is replaced
        (target / "VERSION").write_text("0\n", encoding="utf-8")
        self.assertTrue(cat.node_missing(self.nodes, self.spec))
        self.assertTrue(downloads.ensure_bundled_nodes(self.nodes))
        self.assertFalse(cat.node_missing(self.nodes, self.spec))

    def test_a_folder_openfill_did_not_make_is_left_alone(self):
        (self.nodes / "openfill-nodes").mkdir()
        (self.nodes / "openfill-nodes" / "mine.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            downloads.install_bundled_node("openfill-nodes", self.spec.commit, self.nodes)
        self.assertFalse(downloads.ensure_bundled_nodes(self.nodes))
        self.assertTrue((self.nodes / "openfill-nodes" / "mine.py").is_file())

    def test_wrong_version_is_refused(self):
        with self.assertRaises(RuntimeError):
            downloads.install_bundled_node("openfill-nodes", "999", self.nodes)


class InpaintRequestTests(unittest.TestCase):
    def test_eraser_flags(self):
        self.assertTrue(ModelSpec(template="sdxl_eraser", arch="sdxl").eraser)
        self.assertFalse(ModelSpec(template="sdxl_fooocus", arch="sdxl").eraser)


if __name__ == "__main__":
    unittest.main()
