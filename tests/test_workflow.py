# SPDX-License-Identifier: Apache-2.0
import unittest

from openfill import geometry as g
from openfill import png
from openfill import workflow as w
from openfill.backend import InpaintRequest, ModelSpec, Sampling, Txt2ImgRequest


def _req(**overrides) -> InpaintRequest:
    base = dict(
        image_png=b"",
        mask_png=b"",
        width=600,
        height=400,
        work_width=768,
        work_height=512,
        prompt="a red fox",
        negative="blurry",
        model=ModelSpec("sd15.ckpt"),
        sampling=Sampling(steps=20, batch=1, seed=5),
    )
    base.update(overrides)
    return InpaintRequest(**base)


def _links(graph: dict, node_id: str) -> dict:
    return {k: v for k, v in graph[node_id]["inputs"].items() if isinstance(v, list)}


class InpaintGraphTests(unittest.TestCase):
    def test_sd15_graph_core_nodes_only(self):
        graph = w.build_inpaint(_req(), "sb/i.png", "sb/m.png", 123, w.Capabilities(False))
        classes = {n["class_type"] for n in graph.values()}
        self.assertNotIn("INPAINT_ExpandMask", classes)
        self.assertIn("GrowMask", classes)
        self.assertIn("InpaintModelConditioning", classes)
        self.assertIn("DifferentialDiffusion", classes)
        self.assertEqual(graph["sampler"]["inputs"]["seed"], 123)
        self.assertEqual(graph["sampler"]["inputs"]["latent_image"], ["encode", 2])
        self.assertEqual(graph["load_image"]["inputs"]["image"], "sb/i.png")
        self.assertEqual(graph[w.OUTPUT_NODE]["class_type"], "SaveImage")
        # scaled in and back out
        self.assertEqual(graph["image_scale"]["inputs"]["width"], 768)
        self.assertEqual(graph["out_scale"]["inputs"]["width"], 600)

    def test_sd15_with_inpaint_nodes(self):
        graph = w.build_inpaint(_req(grow=10, feather=20), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(graph["mask_grow"]["class_type"], "INPAINT_ExpandMask")
        self.assertEqual(graph["mask_grow"]["inputs"]["grow"], 10)
        # an SD 1.5 inpainting fill keeps a hard model mask (a blurred band became a pale frame)
        self.assertEqual(graph["mask_grow"]["inputs"]["blur"], 0)
        sdxl = ModelSpec(checkpoint="x.safetensors", arch="sdxl")
        soft = w.build_inpaint(_req(grow=10, feather=20, model=sdxl), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(soft["mask_grow"]["inputs"]["blur"], 10)  # blur is capped at grow
        expand = w.build_inpaint(_req(grow=10, feather=20, mode="expand"), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(expand["mask_grow"]["inputs"]["blur"], 10)  # expand keeps its blur
        lama = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt")
        hard = w.build_inpaint(_req(grow=10, feather=20, model=lama), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(hard["mask_grow"]["inputs"]["blur"], 0)  # removal models get a hard mask
        self.assertEqual(graph["preblur"]["class_type"], "INPAINT_MaskedBlur")
        self.assertEqual(graph["encode"]["inputs"]["pixels"], ["preblur", 0])
        self.assertEqual(graph["encode"]["inputs"]["mask"], ["mask_stable", 0])

    def test_expand_mode_prefills_with_border_fill(self):
        graph = w.build_inpaint(_req(mode="expand"), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(graph["prefill"]["inputs"]["fill"], "navier-stokes")
        self.assertEqual(graph["preblur"]["inputs"]["image"], ["prefill", 0])

    def test_color_match_uses_untouched_pixels_as_reference(self):
        graph = w.build_inpaint(_req(), "i", "m", 1, w.Capabilities(True))
        cm = graph["color_match"]["inputs"]
        self.assertEqual(cm["target"], ["decode", 0])
        self.assertEqual(cm["reference"], ["image_scale", 0])
        self.assertEqual(cm["exclude_mask"], ["mask_stable", 0])
        self.assertEqual(graph["out_scale"]["inputs"]["image"], ["color_match", 0])
        off = w.build_inpaint(_req(color_match=0.0), "i", "m", 1, w.Capabilities(True))
        self.assertNotIn("color_match", off)
        core = w.build_inpaint(_req(), "i", "m", 1, w.Capabilities(False))
        self.assertNotIn("color_match", core)

    def test_removal_intent_prefills_from_the_border(self):
        graph = w.build_inpaint(_req(prefill_style="border"), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(graph["prefill"]["class_type"], "INPAINT_MaskedFill")
        self.assertEqual(graph["prefill"]["inputs"]["fill"], "navier-stokes")
        self.assertEqual(graph["preblur"]["inputs"]["image"], ["prefill", 0])
        default = w.build_inpaint(_req(), "i", "m", 1, w.Capabilities(True))
        self.assertNotIn("prefill", default)  # plain fill keeps the blur-only prefill

    def test_prefill_disabled(self):
        graph = w.build_inpaint(_req(prefill=False), "i", "m", 1, w.Capabilities(True))
        self.assertNotIn("preblur", graph)
        self.assertEqual(graph["encode"]["inputs"]["pixels"], ["image_scale", 0])

    def test_no_scaling_when_sizes_match(self):
        graph = w.build_inpaint(_req(work_width=600, work_height=400), "i", "m", 1, w.Capabilities(False))
        self.assertNotIn("image_scale", graph)
        self.assertNotIn("out_scale", graph)
        self.assertEqual(graph["decode"]["inputs"]["samples"], ["sampler", 0])
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], ["decode", 0])

    def test_sdxl_fooocus_path(self):
        model = ModelSpec("sdxl.safetensors", "sdxl", "head.pth", "v26.patch")
        graph = w.build_inpaint(_req(model=model), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(graph["encode"]["class_type"], "INPAINT_VAEEncodeInpaintConditioning")
        self.assertEqual(graph["fooocus"]["inputs"]["latent"], ["encode", 2])
        self.assertEqual(graph["sampler"]["inputs"]["model"], ["fooocus", 0])
        self.assertEqual(graph["sampler"]["inputs"]["latent_image"], ["encode", 3])
        self.assertEqual(graph["fooocus_patch"]["inputs"]["patch"], "v26.patch")

    def test_sdxl_without_nodes_falls_back_to_core(self):
        model = ModelSpec("sdxl.safetensors", "sdxl", "head.pth", "v26.patch")
        graph = w.build_inpaint(_req(model=model), "i", "m", 1, w.Capabilities(False))
        self.assertEqual(graph["encode"]["class_type"], "InpaintModelConditioning")
        self.assertNotIn("fooocus", graph)

    def test_batch_repeats_latent(self):
        graph = w.build_inpaint(_req(sampling=Sampling(batch=3)), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(graph["batch"]["inputs"]["amount"], 3)
        self.assertEqual(graph["sampler"]["inputs"]["latent_image"], ["batch", 0])

    def test_all_links_resolve(self):
        for caps in (w.Capabilities(True), w.Capabilities(False)):
            graph = w.build_inpaint(_req(sampling=Sampling(batch=2)), "i", "m", 1, caps)
            for node_id in graph:
                for link in _links(graph, node_id).values():
                    self.assertIn(link[0], graph, f"{node_id} links to missing {link[0]}")

    def test_total_steps(self):
        graph = w.build_inpaint(_req(sampling=Sampling(steps=33)), "i", "m", 1, w.Capabilities(False))
        self.assertEqual(w.total_steps(graph), 33)


class TemplateSelectionTests(unittest.TestCase):
    def test_select_builder(self):
        self.assertIs(w.select_builder("sd15_inpaint"), w.build_inpaint)
        self.assertIs(w.select_builder("sdxl_fooocus"), w.build_inpaint)
        self.assertIs(w.select_builder("flux_fill"), w.build_flux_fill)
        self.assertIs(w.select_builder("qwen_edit"), w.build_qwen_edit)
        self.assertIs(w.select_builder("inpaint_model"), w.build_removal)
        with self.assertRaises(ValueError):
            w.select_builder("nope")

    def test_effective_template_from_arch(self):
        self.assertEqual(ModelSpec("x.ckpt").effective_template, "sd15_inpaint")
        self.assertEqual(ModelSpec("x", arch="flux").effective_template, "flux_fill")
        self.assertEqual(ModelSpec("x", template="qwen_edit", arch="sd15").effective_template, "qwen_edit")
        self.assertTrue(ModelSpec(arch="inpaint_model").prompt_free)

    def test_build_fill_dispatches_on_active_model(self):
        sd = w.build_fill(_req(), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(sd["ckpt"]["class_type"], "CheckpointLoaderSimple")
        flux = ModelSpec(
            arch="flux",
            unet="flux1-fill-dev-Q4_0.gguf",
            clip1="t5.gguf",
            clip2="clip_l.safetensors",
            vae="ae.safetensors",
        )
        fx = w.build_fill(_req(model=flux, sampling=Sampling(cfg=30.0, steps=20)), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(fx["unet"]["class_type"], "UnetLoaderGGUF")
        self.assertEqual(fx["clip"]["class_type"], "DualCLIPLoaderGGUF")
        self.assertEqual(fx["clip"]["inputs"]["type"], "flux")
        self.assertEqual(fx["guidance"]["inputs"]["guidance"], 30.0)
        self.assertEqual(fx["sampler"]["inputs"]["cfg"], 1.0)
        self.assertEqual(fx["encode"]["class_type"], "InpaintModelConditioning")
        self.assertEqual(fx["sampler"]["inputs"]["model"], ["diff", 0])

    def test_flux_safetensors_uses_core_loaders(self):
        flux = ModelSpec(
            arch="flux",
            unet="flux1-fill-dev.safetensors",
            clip1="t5xxl_fp8_e4m3fn.safetensors",
            clip2="clip_l.safetensors",
            vae="ae.safetensors",
        )
        fx = w.build_fill(_req(model=flux), "i", "m", 1, w.Capabilities(False))
        self.assertEqual(fx["unet"]["class_type"], "UNETLoader")
        self.assertEqual(fx["unet"]["inputs"]["weight_dtype"], "fp8_e4m3fn")
        self.assertEqual(fx["clip"]["class_type"], "DualCLIPLoader")

    def test_qwen_edit_graph(self):
        qwen = ModelSpec(
            arch="qwen_image",
            unet="qwen-image-edit-2511-Q4_K_M.gguf",
            clip1="qwen_2.5_vl_7b_fp8_scaled.safetensors",
            vae="qwen_image_vae.safetensors",
        )
        g = w.build_fill(_req(model=qwen, prompt="replace the box with a bench"), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(g["clip"]["class_type"], "CLIPLoader")
        self.assertEqual(g["clip"]["inputs"]["type"], "qwen_image")
        self.assertEqual(g["pos"]["class_type"], "TextEncodeQwenImageEditPlus")
        self.assertEqual(g["pos"]["inputs"]["image1"], ["image_scale", 0])
        self.assertEqual(g["noise_mask"]["inputs"]["mask"], ["mask_stable", 0])
        self.assertEqual(g["sampler"]["inputs"]["cfg"], 2.5)
        self.assertEqual(g["sampler"]["inputs"]["model"], ["cfgnorm", 0])

    def test_removal_graph_has_no_prompt_nodes(self):
        lama = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt")
        g = w.build_fill(_req(model=lama), "i", "m", 7, w.Capabilities(True))
        classes = {n["class_type"] for n in g.values()}
        self.assertNotIn("CLIPTextEncode", classes)
        self.assertNotIn("KSampler", classes)
        self.assertEqual(g["inpaint_model"]["inputs"]["model_name"], "big-lama.pt")
        self.assertEqual(g["remove"]["inputs"]["seed"], 7)
        self.assertEqual(g[w.OUTPUT_NODE]["inputs"]["images"], ["out_scale", 0])
        with self.assertRaises(ValueError):
            w.build_fill(_req(model=lama), "i", "m", 7, w.Capabilities(False))

    def test_sdxl_checkpoint_swap(self):
        jug = ModelSpec(
            "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
            "sdxl",
            "fooocus_inpaint_head.pth",
            "inpaint_v26.fooocus.patch",
            template="sdxl_fooocus",
        )
        g = w.build_fill(_req(model=jug), "i", "m", 1, w.Capabilities(True))
        self.assertEqual(g["ckpt"]["inputs"]["ckpt_name"], "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors")
        self.assertEqual(g["fooocus"]["class_type"], "INPAINT_ApplyFooocusInpaint")

    def test_txt2img_rejects_non_txt2img_models(self):
        for spec in (ModelSpec(arch="flux", unet="u"), ModelSpec(arch="inpaint_model", inpaint_model="l")):
            with self.assertRaises(ValueError):
                w.build_txt2img(Txt2ImgRequest(512, 512, "p", "n", spec), 1)

    def test_all_links_resolve_in_new_templates(self):
        specs = [
            ModelSpec(arch="flux", unet="u.gguf", clip1="t.gguf", clip2="c", vae="v"),
            ModelSpec(arch="qwen_image", unet="u.gguf", clip1="c", vae="v"),
            ModelSpec(arch="inpaint_model", inpaint_model="l"),
        ]
        for spec in specs:
            graph = w.build_fill(_req(model=spec, sampling=Sampling(batch=2)), "i", "m", 1, w.Capabilities(True))
            for node_id in graph:
                for link in _links(graph, node_id).values():
                    self.assertIn(link[0], graph, f"{spec.arch}: {node_id} -> {link[0]}")


class Txt2ImgGraphTests(unittest.TestCase):
    def test_graph(self):
        req = Txt2ImgRequest(768, 512, "p", "n", ModelSpec("x.ckpt"), Sampling(batch=2, steps=12))
        graph = w.build_txt2img(req, 9)
        self.assertEqual(graph["latent"]["inputs"], {"width": 768, "height": 512, "batch_size": 2})
        self.assertEqual(graph["sampler"]["inputs"]["denoise"], 1.0)
        self.assertEqual(graph["sampler"]["inputs"]["seed"], 9)
        self.assertEqual(w.total_steps(graph), 12)


class ErrorDescriptionTests(unittest.TestCase):
    def test_describe_node_errors(self):
        graph = {"ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {}}}
        errors = {
            "ckpt": {
                "errors": [
                    {
                        "message": "Value not in list",
                        "details": "ckpt_name: 'x' not in []",
                        "extra_info": {"input_name": "ckpt_name"},
                    }
                ]
            }
        }
        text = w.describe_node_errors(errors, graph)
        self.assertIn("CheckpointLoaderSimple", text)
        self.assertIn("ckpt_name", text)
        self.assertEqual(w.describe_node_errors({}, graph), "")


class ModelDisplayNameTests(unittest.TestCase):
    def test_packaging_details_are_dropped(self):
        self.assertEqual(
            w.model_display_name("FLUX.1 Fill [dev] (first-party safetensors, fp8 at load time)"), "FLUX.1 Fill [dev]"
        )
        self.assertEqual(w.model_display_name("MAT Places512 (fp16) - prompt-free object removal"), "MAT Places512")
        self.assertEqual(w.model_display_name("LaMa (big-lama) - prompt-free object removal"), "LaMa")
        self.assertEqual(
            w.model_display_name("Juggernaut XL v9 (RunDiffusion Photo v2, with Fooocus inpaint patch)"),
            "Juggernaut XL v9",
        )

    def test_plain_names_are_kept(self):
        self.assertEqual(w.model_display_name("Stable Diffusion 1.5 Inpainting"), "Stable Diffusion 1.5 Inpainting")
        self.assertEqual(w.model_display_name("  FLUX.1 Fill [dev] GGUF Q4_0  "), "FLUX.1 Fill [dev] GGUF Q4_0")

    def test_never_empty(self):
        self.assertEqual(w.model_display_name("(fp16)"), "(fp16)")
        self.assertEqual(w.model_display_name(""), "")


class ExpandPromptTests(unittest.TestCase):
    def test_empty_prompt_continues_the_scene(self):
        positive, negative = w.expand_prompts("", "")
        self.assertEqual(positive, "")
        self.assertEqual(negative, w.EXPAND_NEGATIVE)
        for word in ("frame", "border", "vignette", "new objects", "people", "text"):
            self.assertIn(word, negative)

    def test_whitespace_prompt_counts_as_empty_and_user_negative_is_kept(self):
        positive, negative = w.expand_prompts("   ", " lowres ")
        self.assertEqual(positive, "")
        self.assertEqual(negative, "lowres, " + w.EXPAND_NEGATIVE)

    def test_user_prompt_kept_and_only_frame_terms_added(self):
        positive, negative = w.expand_prompts("people walking on a beach", "blurry")
        self.assertEqual(positive, "people walking on a beach")
        self.assertEqual(negative, "blurry, " + w.EXPAND_FRAME_NEGATIVE)
        self.assertNotIn("people", w.EXPAND_FRAME_NEGATIVE)
        self.assertIn("frame", negative)


class ExpandCfgTests(unittest.TestCase):
    def test_sdxl_capped_others_unchanged(self):
        self.assertEqual(w.expand_cfg("sdxl", 7.0), w.EXPAND_SDXL_CFG)
        self.assertEqual(w.expand_cfg("sdxl", 3.0), 3.0)
        self.assertEqual(w.expand_cfg("sd15", 7.0), 7.0)
        self.assertEqual(w.expand_cfg("flux", 30.0), 30.0)


class ProbeSafePngTests(unittest.TestCase):
    def _strip_mask(self):
        # the outpaint mask that PyAV's format probe took for MPEG-TS
        plan = g.plan_expand(1920, 1440, 0, 0, 256, 0, g.SDXL)
        return png.encode(plan.context.width, plan.context.height, g.mask_for_expand(plan), 1)

    def test_padding_fills_probe_window_and_keeps_header(self):
        data = self._strip_mask()
        safe = w.probe_safe_png(data)
        self.assertEqual(png.read_header(safe), png.read_header(data))
        self.assertEqual(safe[37:41], b"tEXt")
        self.assertEqual(len(safe), len(data) + 12 + len(b"Comment") + 1 + w.PROBE_PADDING)
        # nothing but the signature and IHDR is recognisable in the first 2048 bytes
        self.assertNotIn(0x47, safe[41 + 8 : 2048])
        self.assertTrue(safe.endswith(data[33:]))

    def test_idempotent_and_ignores_non_png(self):
        safe = w.probe_safe_png(self._strip_mask())
        self.assertEqual(w.probe_safe_png(safe), safe)
        self.assertEqual(w.probe_safe_png(b"not a png"), b"not a png")


if __name__ == "__main__":
    unittest.main()


class TimeoutEstimateTests(unittest.TestCase):
    """Automatic request timeout of the ComfyUI backend (cold model loads)."""

    def _backend(self, last, root=None):
        from openfill.backends import comfy

        class Client:
            def last_model_files(self):
                if isinstance(last, Exception):
                    raise last
                return set(last)

        return comfy.ComfyBackend(client=Client(), models_root=root)

    def test_cold_when_the_server_last_ran_another_model(self):
        model = ModelSpec(checkpoint="a.safetensors", arch="sd15")
        warm = self._backend({"a.safetensors"})._estimate(25, 1, 512 * 512, model)
        cold = self._backend({"b.safetensors"})._estimate(25, 1, 512 * 512, model)
        fresh = self._backend(set())._estimate(25, 1, 512 * 512, model)
        self.assertLess(warm, 600)
        self.assertGreaterEqual(cold, 600)
        self.assertGreaterEqual(fresh, 600)

    def test_unknown_history_counts_as_cold(self):
        from openfill.comfy_api import ComfyApiError

        model = ModelSpec(checkpoint="a.safetensors", arch="sd15")
        self.assertGreaterEqual(self._backend(ComfyApiError("down"))._estimate(25, 1, 1, model), 600)

    def test_model_size_on_disk_counts(self):
        import tempfile
        from pathlib import Path

        from openfill.backends import comfy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "checkpoints").mkdir()
            (root / "checkpoints" / "a.safetensors").write_bytes(b"x" * 1000)
            (root / "inpaint").mkdir()
            (root / "inpaint" / "p.patch").write_bytes(b"x" * 24)
            spec = ModelSpec(checkpoint="a.safetensors", arch="sdxl", fooocus_head="h.pth", fooocus_patch="p.patch")
            self.assertEqual(comfy.model_files(spec), {"a.safetensors", "h.pth", "p.patch"})
            self.assertEqual(comfy.model_bytes(root, comfy.model_files(spec)), 1024)
            self.assertEqual(comfy.model_bytes(None, {"a.safetensors"}), 0)

    def test_last_model_files_reads_the_latest_prompt(self):
        from openfill.comfy_api import ComfyClient

        client = ComfyClient("http://127.0.0.1:1")
        graph = {
            "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.safetensors"}},
            "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
            "p": {"class_type": "INPAINT_LoadFooocusInpaint", "inputs": {"head": "h.pth", "patch": "p.patch"}},
        }
        client._json = lambda method, path, payload=None: {"id": {"prompt": [0, "id", graph, {}, []]}}
        self.assertEqual(client.last_model_files(), {"a.safetensors", "h.pth", "p.patch"})
        client._json = lambda method, path, payload=None: {}
        self.assertEqual(client.last_model_files(), set())


def _planned(plan: g.FillPlan, model=None, batch=1, **extra) -> w.PlannedInpaintRequest:
    base = dict(
        image_png=b"",
        mask_png=b"",
        width=plan.context.width,
        height=plan.context.height,
        work_width=plan.work.width,
        work_height=plan.work.height,
        prompt="red clay roof tiles",
        negative="blurry",
        model=model or ModelSpec("sd15.ckpt", template="sd15_inpaint"),
        sampling=Sampling(steps=20, batch=batch, seed=5),
        prefill_style="border",
        tiles=plan.tiles,
        refine_tiles=plan.refine_tiles,
        refine_denoise=0.4,
        full_grow=16,
        full_blur=12,
    )
    base.update(extra)
    return w.PlannedInpaintRequest(**base)


ROOF = g.plan_fill(g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273), g.SD15, "sd15", False, 0.5, refine=True)
STRAWBERRY = g.plan_fill(g.Rect(730, 1267, 557, 490), g.Rect(0, 0, 1920, 2880), g.SD15, "sd15", False, 0.5, refine=True)
FOOOCUS = ModelSpec("jugg.safetensors", arch="sdxl", fooocus_head="h.pth", fooocus_patch="p.patch")


class TiledGraphTests(unittest.TestCase):
    def test_tiles_chain_through_the_canvas(self):
        graph = w.build_inpaint(_planned(ROOF), "i", "m", 9, w.Capabilities(True))
        n = len(ROOF.tiles)
        self.assertGreater(n, 1)
        self.assertEqual(graph["t0_crop"]["inputs"]["image"], ["load_image", 0])
        for i in range(1, n):
            self.assertEqual(graph[f"t{i}_crop"]["inputs"]["image"], [f"t{i - 1}_paste", 0])
            self.assertEqual(graph[f"t{i}_mask"]["class_type"], "MaskComposite")  # earlier work faded out
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], [f"t{n - 1}_paste", 0])
        for i in range(n):
            self.assertEqual(graph[f"t{i}_sampler"]["inputs"]["seed"], 9)  # same seed on every tile
            self.assertEqual(graph[f"t{i}_sampler"]["inputs"]["denoise"], 1.0)
            self.assertEqual(graph[f"t{i}_prefill"]["inputs"]["fill"], "navier-stokes")
        self.assertNotIn("sampler", graph)  # no single pass
        self.assertEqual(graph["fmask_grow"]["inputs"]["blur"], 0)  # SD 1.5 fill: hard mask

    def test_single_pass_then_refine(self):
        graph = w.build_inpaint(_planned(STRAWBERRY), "i", "m", 9, w.Capabilities(True))
        self.assertIn("sampler", graph)
        self.assertEqual(graph["main_paste"]["inputs"]["source"], ["out_scale", 0])
        last = len(STRAWBERRY.refine_tiles) - 1
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], [f"r{last}_paste", 0])
        self.assertEqual(graph["r0_sampler"]["inputs"]["denoise"], 0.4)
        self.assertNotIn("r0_prefill", graph)  # refine starts from the generated pixels
        self.assertNotIn("r0_scale", graph)  # full resolution
        self.assertEqual(graph["r0_enc"]["class_type"], "InpaintModelConditioning")

    def test_expand_with_detail_windows(self):
        plan = g.plan_expand(2896, 2172, 0, 400, 0, 400, g.SDXL)
        tiles = g.expand_detail_tiles(plan, g.SDXL, 72)
        req = _planned(
            g.FillPlan(plan.context, g.WorkingSize(1000, 1024, 0.34)),
            model=FOOOCUS,
            mode="expand",
            refine_tiles=tiles,
            refine_denoise=0.4,
        )
        graph = w.build_inpaint(req, "i", "m", 9, w.Capabilities(True))
        self.assertIn("sampler", graph)  # the one pass at the working size first
        last = len(tiles) - 1
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], [f"r{last}_paste", 0])
        self.assertNotIn("r0_scale", graph)  # the windows work at 1:1
        self.assertEqual(graph["r0_sampler"]["inputs"]["denoise"], 0.4)
        self.assertGreater(graph["fmask_grow"]["inputs"]["blur"], 0)  # an expand keeps its soft mask

    def test_variations_repeat_the_canvas(self):
        graph = w.build_inpaint(_planned(ROOF, batch=3), "i", "m", 9, w.Capabilities(True))
        self.assertEqual(graph["dest_batch"]["inputs"]["amount"], 3)
        self.assertNotIn("batch", graph)

    def test_fooocus_tiles_patch_from_the_first_variation(self):
        plan = g.plan_fill(g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273), g.SDXL, "sdxl", False, 0.5, refine=True)
        graph = w.build_inpaint(_planned(plan, model=FOOOCUS, batch=2), "i", "m", 9, w.Capabilities(True))
        self.assertEqual(graph["t0_first"]["class_type"], "ImageFromBatch")
        self.assertEqual(graph["t0_penc"]["inputs"]["pixels"], ["t0_first", 0])
        self.assertEqual(graph["t0_sampler"]["inputs"]["latent_image"], ["t0_lat", 0])
        self.assertEqual(graph["t0_patch"]["class_type"], "INPAINT_ApplyFooocusInpaint")
        one = w.build_inpaint(_planned(plan, model=FOOOCUS), "i", "m", 9, w.Capabilities(True))
        self.assertEqual(one["t0_sampler"]["inputs"]["latent_image"], ["t0_penc", 3])

    def test_sdxl_refine_uses_plain_img2img(self):
        plan = g.plan_fill(
            g.Rect(1488, 1200, 792, 1350), g.Rect(0, 0, 2400, 3000), g.SDXL, "sdxl", False, 0.5, refine=True
        )
        self.assertTrue(plan.refine_tiles)
        graph = w.build_inpaint(_planned(plan, model=FOOOCUS), "i", "m", 9, w.Capabilities(True))
        self.assertEqual(graph["r0_venc"]["class_type"], "VAEEncode")
        self.assertEqual(graph["r0_sampler"]["inputs"]["model"], ["tdiff", 0])  # no Fooocus patch

    def test_layout_pass_then_detail_tiles(self):
        plan = g.plan_fill(
            g.Rect(96, 289, 1690, 866), g.Rect(0, 0, 1920, 1443), g.SD15, "sd15", False, 0.5, layout=True
        )
        graph = w.build_inpaint(_planned(plan, tile_denoise=0.5), "i", "m", 9, w.Capabilities(True))
        self.assertIn("sampler", graph)  # one pass over the whole context first
        self.assertEqual(graph["sampler"]["inputs"]["denoise"], 1.0)
        self.assertEqual(graph["t0_crop"]["inputs"]["image"], ["main_paste", 0])
        n = len(plan.tiles)
        for i in range(n):
            self.assertEqual(graph[f"t{i}_sampler"]["inputs"]["denoise"], 0.5)
            self.assertNotIn(f"t{i}_prefill", graph)  # tiles start from the layout
            self.assertNotIn(f"t{i}_preblur", graph)
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], [f"t{n - 1}_paste", 0])
        no_layout = w.build_inpaint(_planned(plan), "i", "m", 9, w.Capabilities(True))
        self.assertNotIn("sampler", no_layout)

    def test_removal_tiles_chain_with_hard_masks(self):
        lama = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt")
        plan = g.plan_removal(g.Rect(192, 913, 806, 355), g.Rect(0, 0, 1920, 1268))
        req = _planned(plan, model=lama, prompt="", negative="")
        graph = w.build_fill(req, "i", "m", 3, w.Capabilities(True))
        n = len(plan.tiles)
        self.assertEqual(sum(1 for v in graph.values() if v["class_type"] == "INPAINT_InpaintWithModel"), n)
        self.assertEqual(graph["fmask_grow"]["inputs"]["blur"], 0)
        self.assertEqual(graph["rm0_remove"]["inputs"]["mask"], ["rm0_mcrop", 0])
        for i in range(1, n):
            self.assertEqual(graph[f"rm{i}_crop"]["inputs"]["image"], [f"rm{i - 1}_paste", 0])
            self.assertEqual(graph[f"rm{i}_remove"]["inputs"]["mask"], [f"rm{i}_hole", 0])  # hard
            self.assertEqual(graph[f"rm{i}_paste"]["inputs"]["mask"], [f"rm{i}_blend", 0])  # ramp
        self.assertEqual(graph[w.OUTPUT_NODE]["inputs"]["images"], [f"rm{n - 1}_paste", 0])
        self.assertNotIn("image_scale", graph)

    def test_without_inpaint_nodes_falls_back_to_one_pass(self):
        graph = w.build_inpaint(_planned(ROOF), "i", "m", 9, w.Capabilities(False))
        self.assertIn("sampler", graph)
        self.assertFalse(any(k.startswith(("t0_", "r0_")) for k in graph))

    def test_work_pixels_counts_every_pass(self):
        req = _planned(STRAWBERRY)
        self.assertGreater(req.work_pixels, req.work_width * req.work_height)

    def test_removal_refine_with_a_generation_checkpoint(self):
        lama = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt")
        req = _planned(
            STRAWBERRY,
            model=lama,
            refine_model=ModelSpec("sd15.ckpt", template="sd15_inpaint"),
            refine_prompt="background",
        )
        graph = w.build_removal(req, "i", "m", 3, w.Capabilities(True))
        self.assertEqual(graph["ckpt"]["inputs"]["ckpt_name"], "sd15.ckpt")
        self.assertEqual(graph["r0_crop"]["inputs"]["image"], ["out_scale", 0])
        self.assertEqual(graph["pos"]["inputs"]["text"], "background")
        plain = w.build_removal(_planned(STRAWBERRY, model=lama), "i", "m", 3, w.Capabilities(True))
        self.assertNotIn("ckpt", plain)
