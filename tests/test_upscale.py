# SPDX-License-Identifier: Apache-2.0
"""Upscale (issue #18): the Real-ESRGAN and SeedVR2 catalog entries, their graphs, the size
limits and App.upscale making a new image of the right size."""

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openfill import catalog as cat
from openfill import png
from openfill import workflow as w
from openfill.backend import BackendUnavailable, GenerationResult, InpaintRequest, ModelMissing, ModelSpec, Sampling
from openfill.backends.comfy import MODEL_FOLDERS, ComfyBackend, model_files
from openfill.geometry import Rect
from tests.sparse import grow_open

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "models" / "catalog.json"
ESRGAN = ModelSpec(arch="upscale", template="upscale_model", upscale_model="RealESRGAN_x4plus.safetensors")
SEEDVR2 = ModelSpec(
    arch="upscale",
    template="seedvr2",
    unet="seedvr2_3b_int8_convrot.safetensors",
    vae="seedvr2_ema_vae_fp16.safetensors",
)


def request(model: ModelSpec = ESRGAN, size=(640, 480), factor=2) -> InpaintRequest:
    return InpaintRequest(
        image_png=b"",
        mask_png=b"",
        width=size[0],
        height=size[1],
        work_width=size[0] * factor,
        work_height=size[1] * factor,
        prompt="",
        negative="",
        model=model,
        sampling=Sampling(steps=1, batch=1),
        mode="upscale",
    )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.entries = cat.load_catalog(CATALOG)

    def test_real_esrgan_entry(self):
        e = cat.by_id(self.entries, "realesrgan-x4plus")
        self.assertEqual(
            (e.tier, e.capabilities, e.arch, e.template), ("upscale", ("upscale",), "upscale", "upscale_model")
        )
        self.assertEqual(e.family, "upscale")
        self.assertEqual(e.license_name, "BSD-3-Clause")
        self.assertFalse(e.gated)
        self.assertEqual(e.requires_nodes, ())  # ComfyUI's own upscale nodes
        f = e.files[0]
        self.assertEqual(f.dest, "upscale_models/RealESRGAN_x4plus.safetensors")
        self.assertTrue(f.filename.endswith(".safetensors"))  # data only, no pickle
        spec = cat.model_spec_for(e, self.entries)
        self.assertEqual(spec.upscale_model, "RealESRGAN_x4plus.safetensors")
        self.assertTrue(spec.upscaler)
        self.assertEqual(cat.job_hint({"capabilities": ["upscale"], "runnable": True}), "Upscale")

    def test_seedvr2_entries_need_no_custom_node(self):
        for mid, vram in (("seedvr2-3b-int8", 8), ("seedvr2-7b-int8", 12)):
            e = cat.by_id(self.entries, mid)
            self.assertEqual(e.template, "seedvr2")
            self.assertEqual(e.requires_nodes, ())  # ComfyUI 0.39 and newer have the SeedVR2 nodes
            self.assertEqual(e.license_name, "Apache-2.0")
            self.assertEqual(e.min_vram_gb, vram)
            spec = cat.model_spec_for(e, self.entries)
            self.assertTrue(spec.unet.startswith("seedvr2_") and spec.vae == "seedvr2_ema_vae_fp16.safetensors")
        self.assertIn("untested", cat.by_id(self.entries, "seedvr2-7b-int8").name)

    def test_new_files_are_pinned_to_a_revision(self):
        ids = ("realesrgan-x4plus", "seedvr2-3b-int8", "seedvr2-7b-int8", "flux2-klein-4b-fp8", "flux2-klein-9b-fp8")
        for mid in ids:
            for f in cat.by_id(self.entries, mid).files:
                self.assertRegex(f.url, r"^https://huggingface\.co/[^/]+/[^/]+/resolve/[0-9a-f]{40}/", f.url)
                self.assertTrue(f.size and f.sha256, f.dest)
                self.assertEqual(f.url.rsplit("/", 1)[1], f.filename)

    def test_upscalers_serve_only_upscale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, nodes = Path(tmp) / "models", Path(tmp) / "custom_nodes"
            e = cat.by_id(self.entries, "realesrgan-x4plus")
            (root / "upscale_models").mkdir(parents=True)
            with open(root / e.files[0].dest, "wb") as fh:
                grow_open(fh, e.files[0].size)
            self.assertEqual([x.id for x in cat.runnable_installed(self.entries, root, nodes, "upscale")], [e.id])
            for operation in ("fill", "remove", "expand", "txt2img", "bgremove"):
                self.assertNotIn(e.id, [x.id for x in cat.runnable_installed(self.entries, root, nodes, operation)])

    def test_upscale_models_folder_is_known(self):
        self.assertIn("upscale_models", MODEL_FOLDERS)
        from openfill import shared_models

        self.assertIn("upscale_models", shared_models.MODEL_FOLDERS)
        self.assertEqual(model_files(ESRGAN), {"RealESRGAN_x4plus.safetensors"})


class GraphTests(unittest.TestCase):
    def test_esrgan_graph_upscales_and_resizes_to_the_exact_size(self):
        graph = w.build_fill(request(), "img.png", "mask.png", 3, w.Capabilities(), "openfill/upscale-x")
        self.assertEqual(
            {n["class_type"] for n in graph.values()},
            {"LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "ImageScale", "SaveImage"},
        )
        self.assertEqual(graph["upscale_model"]["inputs"]["model_name"], "RealESRGAN_x4plus.safetensors")
        self.assertEqual(graph["upscale"]["inputs"]["image"], ["load_image", 0])
        scale = graph["out_scale"]["inputs"]
        self.assertEqual((scale["width"], scale["height"], scale["upscale_method"]), (1280, 960, "lanczos"))
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], ["out_scale", 0])
        self.assertNotIn("mask.png", repr(graph))

    def test_esrgan_needs_its_file(self):
        with self.assertRaisesRegex(ValueError, "upscale model"):
            w.build_fill(request(ModelSpec(arch="upscale")), "i", "m", 0, w.Capabilities())

    def test_seedvr2_graph_follows_comfyuis_template(self):
        graph = w.build_fill(request(SEEDVR2, factor=4), "img.png", "m.png", 9, w.Capabilities(seedvr2=True))
        types = [n["class_type"] for n in graph.values()]
        for cls in ("SeedVR2Preprocess", "VAEEncodeTiled", "SeedVR2Conditioning", "KSampler", "VAEDecodeTiled"):
            self.assertIn(cls, types)
        self.assertEqual(graph["resize"]["inputs"]["width"], 2560)
        self.assertEqual(graph["sampler"]["inputs"]["steps"], 1)
        self.assertEqual(graph["sampler"]["inputs"]["seed"], 9)
        self.assertEqual(graph["sampler"]["inputs"]["positive"], ["condition", 0])
        self.assertEqual(graph["finish"]["inputs"]["original_resized_images"], ["resize", 0])
        self.assertEqual(graph["encode"]["inputs"]["tile_size"], w.SEEDVR2_TILE)
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], ["finish", 0])

    def test_seedvr2_on_an_old_comfyui_says_so(self):
        with self.assertRaisesRegex(ValueError, "0.39"):
            w.build_fill(request(SEEDVR2), "i", "m", 0, w.Capabilities())

    def test_upscalers_never_make_text_to_image(self):
        from openfill.backend import Txt2ImgRequest

        with self.assertRaises(ValueError):
            w.build_txt2img(Txt2ImgRequest(64, 64, "x", "", ESRGAN), 1)

    def test_seedvr2_capability_is_detected(self):
        class Client:
            def object_info(self, node_class=None):
                return {node_class: {}} if node_class == "SeedVR2Conditioning" else {}

        self.assertTrue(ComfyBackend(client=Client()).capabilities().seedvr2)


class FakeIo:
    def __init__(self, size=(320, 200), indexed=False):
        self.size, self.indexed, self.new_images = size, indexed, []

    def is_indexed(self, image):
        return self.indexed

    def image_rect(self, image):
        return Rect(0, 0, *self.size)

    def export_region(self, image, rect):
        rgba = b"\x10\x20\x30\xff" * (rect.width * rect.height)
        return png.encode(rect.width, rect.height, rgba, 4), rgba

    def new_image_from_pngs(self, images, name, temp):
        self.new_images.append((png.read_header(images[0])[:2], name, len(images)))
        return "new-image"


class FakeRunner:
    def __init__(self, app, title, model, interactive=True):
        self.title, self.failed = title, None

    def run(self, job):
        return job(None, None, None)

    def fail(self, err):
        self.failed = err

    def done(self, text):
        self.text = text


class FakeBackend(ComfyBackend):
    def __init__(self, size_of=None):
        self.requests, self.size_of = [], size_of

    def inpaint(self, request, progress=None, preview=None, cancel=None):
        self.requests.append(request)
        w_, h_ = self.size_of or (request.work_width, request.work_height)
        return GenerationResult([png.encode(w_, h_, b"\0\0\0" * (w_ * h_), 3)], 1)


class AppTests(unittest.TestCase):
    def app(self, installed=("realesrgan-x4plus",), backend=None, io=None):
        from openfill.app import App
        from openfill.config import Settings

        app = App.__new__(App)
        app.io = io or FakeIo()
        app.log = logging.getLogger("test")
        app.settings = Settings(backend="comfyui")
        app.interactive = False
        app.runner_cls = FakeRunner
        app.server = SimpleNamespace(touch=lambda: None)
        app.paths = SimpleNamespace(temp=Path(tempfile.gettempdir()))
        entries = cat.load_catalog(CATALOG)
        app.catalog = entries
        chosen = [cat.by_id(entries, mid) for mid in installed]
        app.upscale_models = lambda: chosen
        app.resolve_model = lambda mid: (cat.by_id(entries, mid), cat.model_spec_for(cat.by_id(entries, mid), entries))
        app._backend = backend or FakeBackend()
        app.backend = lambda progress=None, cancel=None: app._backend
        return app

    def test_2x_makes_a_new_image_twice_the_size(self):
        app = self.app()
        self.assertEqual(app.upscale(object(), 2), "new-image")
        self.assertEqual(app.io.new_images, [((640, 400), "Upscaled 2x", 1)])
        req = app._backend.requests[0]
        self.assertEqual((req.width, req.height, req.work_width, req.work_height), (320, 200, 640, 400))
        self.assertEqual(req.mode, "upscale")
        self.assertEqual(req.model.upscale_model, "RealESRGAN_x4plus.safetensors")

    def test_4x(self):
        app = self.app()
        app.upscale(object(), 4)
        self.assertEqual(app.io.new_images[0][0], (1280, 800))

    def test_a_result_of_the_wrong_size_is_not_used(self):
        from openfill.backend import BackendError

        app = self.app(backend=FakeBackend(size_of=(10, 10)))
        with self.assertRaisesRegex(BackendError, "10 x 10"):
            app.upscale(object(), 2)
        self.assertEqual(app.io.new_images, [])

    def test_no_upscale_model_says_what_to_download(self):
        with self.assertRaisesRegex(ModelMissing, "Real-ESRGAN"):
            self.app(installed=()).upscale(object(), 2)

    def test_a_fill_model_is_not_an_upscaler(self):
        with self.assertRaisesRegex(ModelMissing, "not an upscale model"):
            self.app().upscale(object(), 2, "dreamshaper-8-inpainting")

    def test_only_2x_and_4x(self):
        with self.assertRaises(ValueError):
            self.app().upscale(object(), 3)

    def test_too_large_is_refused_before_anything_is_sent(self):
        from openfill.app import UpscaleTooLarge

        app = self.app(io=FakeIo((6000, 4000)))
        with self.assertRaisesRegex(UpscaleTooLarge, "Try 2x"):
            app.upscale(object(), 4)
        self.assertEqual(app._backend.requests, [])

    def test_seedvr2_keeps_results_smaller(self):
        from openfill.app import SEEDVR2_MAX_PIXELS, UpscaleTooLarge

        app = self.app(installed=("seedvr2-3b-int8",))
        self.assertEqual(app.upscale_size(1024, 1024, 4, SEEDVR2), (4096, 4096))
        self.assertLessEqual(4096 * 4096, SEEDVR2_MAX_PIXELS)
        with self.assertRaisesRegex(UpscaleTooLarge, "Real-ESRGAN can make bigger"):
            app.upscale_size(3000, 2000, 2, SEEDVR2)
        self.assertEqual(app.upscale_size(3000, 2000, 2, ESRGAN), (6000, 4000))

    def test_indexed_images_are_refused(self):
        from openfill.app import UnsupportedImage

        with self.assertRaises(UnsupportedImage):
            self.app(io=FakeIo(indexed=True)).upscale(object(), 2)

    def test_runs_on_comfyui_only(self):
        from openfill.backends.a1111 import A1111Backend

        app = self.app(backend=A1111Backend("http://127.0.0.1:9", 0, None))
        with self.assertRaisesRegex(BackendUnavailable, "ComfyUI only"):
            app.upscale(object(), 2)

    def test_default_prefers_real_esrgan(self):
        from unittest import mock

        from openfill.app import App
        from openfill.config import Settings

        app = App.__new__(App)
        entries = cat.load_catalog(CATALOG)
        app.catalog = entries
        app.settings = Settings(comfy_dir="/nonexistent")
        order = [cat.by_id(entries, "seedvr2-3b-int8"), cat.by_id(entries, "realesrgan-x4plus")]
        with mock.patch.object(cat, "runnable_installed", return_value=order) as found:
            self.assertEqual([e.id for e in App.upscale_models(app)], ["realesrgan-x4plus", "seedvr2-3b-int8"])
        self.assertEqual(found.call_args.args[3], "upscale")


if __name__ == "__main__":
    unittest.main()
