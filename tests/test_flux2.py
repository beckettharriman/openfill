# SPDX-License-Identifier: Apache-2.0
"""FLUX.2 [klein] (issue #18): the catalog entries, the graph and the sampling settings."""

import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from openfill import catalog as cat
from openfill import flux2_remove, geometry, hardware
from openfill import workflow as w
from openfill.app import App
from openfill.backend import InpaintRequest, ModelMissing, ModelSpec, Sampling, Txt2ImgRequest
from openfill.config import Settings

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "models" / "catalog.json"
KLEIN = ModelSpec(
    arch="flux2",
    template="flux2_edit",
    unet="flux-2-klein-4b-fp8.safetensors",
    clip1="qwen_3_4b.safetensors",
    vae="flux2-vae.safetensors",
)


def request(model=KLEIN, denoise=1.0, batch=1, size=(768, 512), work=(1024, 688)) -> InpaintRequest:
    return InpaintRequest(
        image_png=b"",
        mask_png=b"",
        width=size[0],
        height=size[1],
        work_width=work[0],
        work_height=work[1],
        prompt="a red kite",
        negative="",
        model=model,
        sampling=Sampling(steps=4, cfg=1.0, denoise=denoise, batch=batch),
    )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.entries = cat.load_catalog(CATALOG)

    def test_4b_is_apache_and_uses_comfyuis_file_names(self):
        e = cat.by_id(self.entries, "flux2-klein-4b-fp8")
        self.assertEqual((e.arch, e.template, e.family), ("flux2", "flux2_edit", "flux2-klein"))
        self.assertEqual(e.license_name, "Apache-2.0")
        self.assertFalse(e.gated)
        self.assertEqual(e.requires_nodes, ())
        # the names ComfyUI's own Klein templates use, so files already there count as installed
        self.assertEqual(
            {f.role: f.dest for f in e.files},
            {
                "unet": "diffusion_models/flux-2-klein-4b-fp8.safetensors",
                "clip1": "text_encoders/qwen_3_4b.safetensors",
                "vae": "vae/flux2-vae.safetensors",
            },
        )
        spec = cat.model_spec_for(e, self.entries)
        self.assertEqual((spec.unet, spec.clip1, spec.vae), (KLEIN.unet, KLEIN.clip1, KLEIN.vae))

    def test_9b_is_marked_non_commercial_gated_and_untested(self):
        e = cat.by_id(self.entries, "flux2-klein-9b-fp8")
        self.assertIn("Non-Commercial", e.license_name)
        self.assertTrue(e.gated)
        self.assertTrue(e.file_by_role("unet").gated)
        self.assertIn("untested", e.name)
        self.assertEqual(e.min_vram_gb, 12)

    def test_klein_is_only_used_when_picked(self):
        for mid in ("flux2-klein-4b-fp8", "flux2-klein-9b-fp8"):
            e = cat.by_id(self.entries, mid)
            self.assertTrue(cat.is_opt_in(e))
            self.assertIsNone(cat.auto_active([e]))
            self.assertIsNone(cat.tier_default([e], "quality"))
            self.assertEqual(cat.model_role({"id": mid, "family": e.family}), "used only when picked")
        self.assertIn("flux2-klein", cat.OPT_IN_FAMILIES)

    def test_profile_and_fit(self):
        self.assertEqual(geometry.PROFILES["flux2"].multiple, 16)
        self.assertIn("flux2", hardware.OFFLOAD_FLOOR_GB)


class GraphTests(unittest.TestCase):
    def test_reference_edit_with_a_noise_mask(self):
        graph = w.build_fill(request(), "img.png", "mask.png", 7, w.Capabilities(), "openfill/fill-x")
        self.assertEqual(
            graph["unet"], {"class_type": "UNETLoader", "inputs": {"unet_name": KLEIN.unet, "weight_dtype": "default"}}
        )
        self.assertEqual(graph["clip"]["inputs"], {"clip_name": KLEIN.clip1, "type": "flux2"})
        self.assertEqual(graph["pos_ref"]["inputs"], {"conditioning": ["pos", 0], "latent": ["encode", 0]})
        self.assertEqual(graph["noise_mask"]["inputs"]["samples"], ["encode", 0])
        self.assertEqual(graph["guider"]["inputs"]["cfg"], 1.0)
        self.assertEqual(graph["scheduler"]["inputs"], {"steps": 4, "width": 1024, "height": 688})
        self.assertEqual(graph["noise"]["inputs"]["noise_seed"], 7)
        self.assertEqual(graph["sampler"]["inputs"]["sigmas"], ["scheduler", 0])
        self.assertEqual(graph["sampler"]["inputs"]["latent_image"], ["noise_mask", 0])
        self.assertEqual(graph["out_scale"]["inputs"]["width"], 768)  # back to the crop's size
        self.assertEqual(w.total_steps(graph), 4)

    def test_partial_strength_trims_the_sigmas(self):
        graph = w.build_fill(request(denoise=0.6), "i", "m", 1, w.Capabilities())
        self.assertEqual(graph["sigmas_split"]["inputs"]["denoise"], 0.6)
        self.assertEqual(graph["sampler"]["inputs"]["sigmas"], ["sigmas_split", 1])

    def test_variations_repeat_the_latent(self):
        graph = w.build_fill(request(batch=3), "i", "m", 1, w.Capabilities())
        self.assertEqual(graph["batch"]["inputs"]["amount"], 3)
        self.assertEqual(graph["sampler"]["inputs"]["latent_image"], ["batch", 0])

    def test_gguf_files_use_the_gguf_loaders(self):
        spec = ModelSpec(arch="flux2", unet="flux-2-klein-9b-Q4_K_M.gguf", clip1="qwen3-8b-Q4.gguf", vae="v")
        graph = w.build_fill(request(spec), "i", "m", 1, w.Capabilities())
        self.assertEqual(graph["unet"]["class_type"], "UnetLoaderGGUF")
        self.assertEqual(graph["clip"]["class_type"], "CLIPLoaderGGUF")

    def test_removal_adapter_uses_a_marked_reference_and_masks_the_result(self):
        req = request(replace(KLEIN, lora=flux2_remove.LORA_NAME), batch=3)
        req.flux2_remove_box = (24, 32, 80, 96)
        req.prompt = flux2_remove.PROMPT
        graph = w.build_fill(req, "i", "m", 1, w.Capabilities(inpaint_nodes=True))
        self.assertEqual(graph["encode"]["inputs"]["pixels"], ["highlight_reference", 0])
        self.assertEqual(graph["highlight_red"]["inputs"]["color"], 0xFF0000)
        self.assertEqual(graph["highlight_reference"]["inputs"]["x"], 32)
        self.assertEqual(graph["highlight_reference"]["inputs"]["y"], 43)
        self.assertEqual(graph["remove_lora"]["inputs"]["lora_name"], flux2_remove.LORA_NAME)
        self.assertEqual(graph["remove_lora"]["inputs"]["strength_model"], 1.1)
        self.assertEqual(graph["empty"]["inputs"]["batch_size"], 3)
        self.assertEqual(graph["reference_batch"]["inputs"]["image"], ["image_scale", 0])
        self.assertEqual(graph["remove_composite"]["inputs"]["mask"], ["mask_stable", 0])
        self.assertEqual(graph["color_match"]["inputs"]["reference"], ["image_scale", 0])
        self.assertNotIn("noise_mask", graph)

    def test_removal_outline_handles_a_one_pixel_selection(self):
        req = request(replace(KLEIN, lora=flux2_remove.LORA_NAME), size=(512, 512), work=(512, 512))
        req.flux2_remove_box = (511, 511, 512, 512)
        graph = w.build_fill(req, "i", "m", 1, w.Capabilities())
        self.assertEqual(graph["highlight_red"]["inputs"]["width"], 1)
        self.assertEqual(graph["highlight_red"]["inputs"]["height"], 1)
        self.assertEqual(graph["highlight_reference"]["inputs"]["mask"], ["highlight_outer", 0])

    def test_stock_fill_and_expand_do_not_load_the_adapter(self):
        for mode in ("fill", "expand"):
            with self.subTest(mode=mode):
                req = replace(request(replace(KLEIN, lora=flux2_remove.LORA_NAME)), mode=mode)
                graph = w.build_fill(req, "i", "m", 1, w.Capabilities())
                self.assertEqual(graph["encode"]["inputs"]["pixels"], ["image_scale", 0])
                self.assertFalse(any(node["class_type"] == "LoraLoaderModelOnly" for node in graph.values()))

    def test_no_text_to_image(self):
        with self.assertRaises(ValueError):
            w.build_txt2img(Txt2ImgRequest(64, 64, "x", "", KLEIN), 1)


class RemoveSettingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = App.__new__(App)
        self.app.settings = Settings(comfy_dir=tmp.name)
        self.app.interactive = False
        self.app.log = logging.getLogger(__name__)
        self.app.io = SimpleNamespace(
            is_indexed=lambda _image: False,
            selection_rect=lambda _image: geometry.Rect(180, 220, 80, 134),
            image_rect=lambda _image: geometry.Rect(0, 0, 448, 512),
            export_region=lambda _image, _rectangle: (b"image", b"pixels"),
            export_selection_mask=lambda _image, _rectangle: (b"mask", b"pixels"),
        )
        self.app.resolve_model = lambda choice: (SimpleNamespace(id=choice, name=choice), KLEIN)
        self.app._is_default_choice = lambda _choice: False
        self.stop = RuntimeError("request captured")
        self.inpaint = Mock(side_effect=self.stop)
        self.app.backend = lambda *_args: SimpleNamespace(inpaint=self.inpaint)
        runner = Mock()
        runner.run.side_effect = lambda run: run(None, None, None)
        self.app.runner_cls = lambda *_args, **_kwargs: runner
        self.lora = Path(tmp.name) / "models" / "loras" / flux2_remove.LORA_NAME
        self.lora.parent.mkdir(parents=True)

    def fill(self, model="flux2-klein-4b-fp8", *, remove=True, as_picked=True):
        return self.app.generative_fill(object(), "", "", 1, 1.0, 7, model, remove=remove, as_picked=as_picked)

    def captured(self, *args, **kwargs):
        with self.assertRaisesRegex(RuntimeError, "request captured"):
            self.fill(*args, **kwargs)
        return self.inpaint.call_args.args[0]

    def test_default_off_keeps_stock_remove(self):
        self.lora.touch()
        self.assertFalse(self.app.settings.flux2_remove_lora)
        req = self.captured()
        self.assertIsNone(req.flux2_remove_box)
        self.assertEqual(req.model.lora, "")

    def test_enabled_runs_only_for_an_explicit_local_klein_4b_remove(self):
        self.lora.touch()
        self.app.settings.flux2_remove_lora = True
        req = self.captured()
        self.assertIsNotNone(req.flux2_remove_box)
        self.assertEqual(req.model.lora, flux2_remove.LORA_NAME)
        self.assertEqual(req.prompt, flux2_remove.PROMPT)
        for model, opts in (
            ("flux2-klein-4b-fp8", {"remove": False}),
            ("flux2-klein-4b-fp8", {"as_picked": False}),
            ("flux2-klein-9b-fp8", {}),
        ):
            with self.subTest(model=model, opts=opts):
                self.assertIsNone(self.captured(model, **opts).flux2_remove_box)
        self.app.settings.backend = "remote"
        self.assertIsNone(self.captured().flux2_remove_box)

    def test_enabled_with_missing_adapter_explains_how_to_install_it(self):
        self.app.settings.flux2_remove_lora = True
        with self.assertRaisesRegex(ModelMissing, "models/loras"):
            self.fill()
        self.inpaint.assert_not_called()


class SamplingTests(unittest.TestCase):
    def test_klein_samples_in_four_steps_without_guidance(self):
        from openfill.app import FLUX2_KLEIN_STEPS

        app = App.__new__(App)
        app.settings = Settings(steps=30, cfg=7.0)
        s = app.sampling(KLEIN, 2, 5, 1.0)
        self.assertEqual((s.steps, s.cfg, s.batch, s.seed), (FLUX2_KLEIN_STEPS, 1.0, 2, 5))
        self.assertEqual(FLUX2_KLEIN_STEPS, 4)


if __name__ == "__main__":
    unittest.main()
