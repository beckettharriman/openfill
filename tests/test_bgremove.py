# SPDX-License-Identifier: Apache-2.0
"""Remove Background with BiRefNet: the catalog entry, the model spec, the graph and the
server capability check."""

import tempfile
import unittest
from pathlib import Path

from openfill import catalog as cat
from openfill import workflow as w
from openfill.backend import InpaintRequest, ModelSpec, Sampling
from openfill.backends.comfy import ComfyBackend, model_files
from tests.sparse import grow_open

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "models" / "catalog.json"
BIREFNET = ModelSpec(arch="bg_removal", template="bgremove", bg_removal_model="birefnet.safetensors")


def request(model: ModelSpec = BIREFNET) -> InpaintRequest:
    return InpaintRequest(
        image_png=b"",
        mask_png=b"",
        width=64,
        height=48,
        work_width=64,
        work_height=48,
        prompt="",
        negative="",
        model=model,
        sampling=Sampling(steps=1, batch=1),
        mode="bgremove",
    )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.entries = cat.load_catalog(CATALOG)
        self.entry = cat.by_id(self.entries, "birefnet")

    def test_entry_is_a_runnable_background_removal_model(self):
        self.assertTrue(self.entry.runnable)
        self.assertEqual(self.entry.capabilities, ("bgremove",))
        self.assertEqual(self.entry.template, "bgremove")
        self.assertEqual(self.entry.arch, "bg_removal")
        self.assertEqual(self.entry.family, "removal")
        self.assertEqual(self.entry.files[0].subfolder, "background_removal")
        self.assertEqual(self.entry.files[0].loader_name, "birefnet.safetensors")

    def test_model_spec_names_the_file_the_server_lists(self):
        spec = cat.model_spec_for(self.entry, self.entries)
        self.assertEqual(spec.bg_removal_model, "birefnet.safetensors")
        self.assertEqual(spec.effective_template, "bgremove")
        self.assertTrue(spec.background_removal)
        self.assertFalse(spec.prompt_free)
        self.assertFalse(spec.eraser)

    def test_only_remove_background_can_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, nodes = Path(tmp) / "models", Path(tmp) / "custom_nodes"
            (root / "background_removal").mkdir(parents=True)
            with open(root / self.entry.files[0].dest, "wb") as fh:
                grow_open(fh, self.entry.files[0].size)
            self.assertEqual(
                [e.id for e in cat.runnable_installed(self.entries, root, nodes, "bgremove")], ["birefnet"]
            )
            for operation in ("remove", "fill", "expand", "txt2img"):
                self.assertNotIn(
                    "birefnet", [e.id for e in cat.runnable_installed(self.entries, root, nodes, operation)]
                )

    def test_models_list_names_the_job(self):
        row = {"capabilities": list(self.entry.capabilities), "runnable": True, "template": self.entry.template}
        self.assertEqual(cat.job_hint(row), "Remove background")

    def test_not_picked_as_a_fill_or_removal_default(self):
        self.assertNotIn("birefnet", cat.REMOVE_PREFERENCE)
        self.assertNotIn("birefnet", cat.AUTO_ACTIVE_ORDER)
        self.assertIsNone(cat.tier_default([self.entry], "remove"))


class GraphTests(unittest.TestCase):
    CAPS = w.Capabilities(bg_removal=True)

    def test_graph_loads_model_removes_background_and_saves_rgba(self):
        graph = w.build_fill(request(), "img.png", "mask.png", 0, self.CAPS, filename_prefix="openfill/bgremove-x")
        types = {node["class_type"] for node in graph.values()}
        self.assertEqual(
            types,
            {
                "LoadImage",
                "LoadBackgroundRemovalModel",
                "RemoveBackground",
                "InvertMask",
                "JoinImageWithAlpha",
                "SaveImage",
            },
        )
        self.assertEqual(graph["bg_model"]["inputs"]["bg_removal_name"], "birefnet.safetensors")
        self.assertEqual(graph["load_image"]["inputs"]["image"], "img.png")
        self.assertEqual(graph["foreground"]["inputs"]["bg_removal_model"], ["bg_model", 0])
        self.assertEqual(graph["foreground"]["inputs"]["image"], ["load_image", 0])
        self.assertEqual(graph["transparency"]["inputs"]["mask"], ["foreground", 0])
        self.assertEqual(graph["cutout"]["inputs"]["alpha"], ["transparency", 0])
        self.assertEqual(graph["cutout"]["inputs"]["image"], ["load_image", 0])
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], ["cutout", 0])
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["filename_prefix"], "openfill/bgremove-x")

    def test_the_mask_file_is_not_loaded(self):
        graph = w.build_fill(request(), "img.png", "mask.png", 0, self.CAPS)
        self.assertNotIn("mask.png", repr(graph))

    def test_old_server_gets_a_clear_message(self):
        with self.assertRaisesRegex(ValueError, "ComfyUI 0.39"):
            w.build_fill(request(), "img.png", "mask.png", 0, w.Capabilities())

    def test_needs_the_model_file(self):
        with self.assertRaisesRegex(ValueError, "background removal model"):
            w.build_fill(request(ModelSpec(arch="bg_removal")), "img.png", "mask.png", 0, self.CAPS)

    def test_has_no_sampler_so_progress_follows_the_nodes(self):
        graph = w.build_fill(request(), "img.png", "mask.png", 0, self.CAPS)
        self.assertEqual(w.total_steps(graph), 0)  # comfy_api divides by max(1, total)


class BackendTests(unittest.TestCase):
    class Client:
        def __init__(self, nodes):
            self.nodes = nodes

        def object_info(self, node_class=None):
            return {node_class: {}} if node_class in self.nodes else {}

    def test_capability_is_detected_from_the_server(self):
        with_nodes = ComfyBackend(client=self.Client({"RemoveBackground"}))
        self.assertTrue(with_nodes.capabilities().bg_removal)
        self.assertFalse(with_nodes.capabilities().inpaint_nodes)
        without = ComfyBackend(client=self.Client(set()))
        self.assertFalse(without.capabilities().bg_removal)

    def test_model_file_counts_toward_the_load_estimate(self):
        self.assertEqual(model_files(BIREFNET), {"birefnet.safetensors"})


class OnlyComfyTests(unittest.TestCase):
    """Remove Background is a ComfyUI graph: another backend would fill the whole image instead."""

    def app(self, backend):
        from openfill.app import App

        app = App.__new__(App)
        app.backend = lambda progress=None, cancel=None: backend
        return app

    def test_another_backend_is_refused(self):
        from openfill.backend import BackendUnavailable
        from openfill.backends.a1111 import A1111Backend

        with self.assertRaisesRegex(BackendUnavailable, "ComfyUI only"):
            self.app(A1111Backend("http://127.0.0.1:9", 0, None))._background_removal_backend()

    def test_comfyui_is_used(self):
        comfy = ComfyBackend(client=BackendTests.Client({"RemoveBackground"}))
        self.assertIs(self.app(comfy)._background_removal_backend(), comfy)


if __name__ == "__main__":
    unittest.main()
