# SPDX-License-Identifier: Apache-2.0
"""FLUX.2 [klein] (issue #18): the catalog entries, the graph and the sampling settings."""

import unittest
from pathlib import Path

from openfill import catalog as cat
from openfill import geometry, hardware
from openfill import workflow as w
from openfill.backend import InpaintRequest, ModelSpec, Sampling, Txt2ImgRequest

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

    def test_no_text_to_image(self):
        with self.assertRaises(ValueError):
            w.build_txt2img(Txt2ImgRequest(64, 64, "x", "", KLEIN), 1)


class SamplingTests(unittest.TestCase):
    def test_klein_samples_in_four_steps_without_guidance(self):
        from openfill.app import FLUX2_KLEIN_STEPS, App
        from openfill.config import Settings

        app = App.__new__(App)
        app.settings = Settings(steps=30, cfg=7.0)
        s = app.sampling(KLEIN, 2, 5, 1.0)
        self.assertEqual((s.steps, s.cfg, s.batch, s.seed), (FLUX2_KLEIN_STEPS, 1.0, 2, 5))
        self.assertEqual(FLUX2_KLEIN_STEPS, 4)


if __name__ == "__main__":
    unittest.main()
