# SPDX-License-Identifier: Apache-2.0
"""Static batch-size check of every workflow graph (issue #16).

ComfyUI only finds a batch mismatch when the graph runs (comfyui-inpaint-nodes'
Fill Masked Area asserts "Image and mask batch size does not match"). This walks
the API-format graph and works out the batch size of every IMAGE, MASK, LATENT
and conditioning output with the rules of the nodes OpenFill uses, so a graph
with more than one variation is checked without a server.
"""

import unittest

from openfill import geometry as g
from openfill import workflow as w
from openfill.backend import ModelSpec, Sampling

# Nodes that output no tensor (loaders, text encoders, model patches without a
# latent of their own). A model or conditioning input passes its batch through.
_NO_TENSOR = {
    "CheckpointLoaderSimple",
    "CLIPLoader",
    "CLIPLoaderGGUF",
    "CLIPTextEncode",
    "DualCLIPLoader",
    "DualCLIPLoaderGGUF",
    "INPAINT_LoadFooocusInpaint",
    "INPAINT_LoadInpaintModel",
    "LoadBackgroundRemovalModel",
    "UNETLoader",
    "UnetLoaderGGUF",
    "VAELoader",
}
_PASS_MODEL = {n: "model" for n in ("DifferentialDiffusion", "CFGNorm", "ModelSamplingAuraFlow", "LoraLoaderModelOnly")}
_PASS_COND = {"FluxGuidance": "conditioning"}
# Same batch out as in: (input name).
_SAME = {
    "CropMask": "mask",
    "GrowMask": "mask",
    "INPAINT_ExpandMask": "mask",
    "INPAINT_StabilizeMask": "mask",
    "ImageBlur": "image",
    "ImageCrop": "image",
    "ImageScale": "image",
    "ImageToMask": "image",
    "InvertMask": "mask",
    "MaskToImage": "mask",
    "RemoveBackground": "image",
    "VAEDecode": "samples",
    "VAEEncode": "pixels",
}


class BatchError(AssertionError):
    pass


def graph_batches(graph: dict) -> tuple[dict, list[str]]:
    """Batch size of every tensor output, ``{(node, slot): n}`` (None: no tensor),
    and the list of rule violations."""
    memo: dict[str, dict] = {}
    errors: list[str] = []

    def link(node_id: str, name: str):
        value = graph[node_id]["inputs"].get(name)
        if not isinstance(value, list):
            return None
        return outputs(value[0]).get(value[1])

    def fits(node_id: str, what: str, n, target) -> None:
        # ComfyUI broadcasts a batch of 1; anything else must match.
        if n is not None and target is not None and n not in (1, target):
            cls = graph[node_id]["class_type"]
            errors.append(f"{node_id} ({cls}): {what} batch {n} does not match {target}")

    def outputs(node_id: str) -> dict:
        if node_id in memo:
            return memo[node_id]
        node = graph[node_id]
        cls, inputs = node["class_type"], node["inputs"]
        if cls in _NO_TENSOR:
            out = {}
        elif cls in _PASS_MODEL:
            out = {0: link(node_id, _PASS_MODEL[cls])}
        elif cls in _PASS_COND:
            out = {0: link(node_id, _PASS_COND[cls])}
        elif cls in _SAME:
            out = {0: link(node_id, _SAME[cls])}
        elif cls == "LoadImage":
            out = {0: 1, 1: 1}
        elif cls == "SolidMask":
            out = {0: 1}
        elif cls == "EmptyLatentImage":
            out = {0: int(inputs["batch_size"])}
        elif cls == "RepeatImageBatch":
            out = {0: link(node_id, "image") * int(inputs["amount"])}
        elif cls == "RepeatLatentBatch":
            out = {0: link(node_id, "samples") * int(inputs["amount"])}
        elif cls == "ImageFromBatch":
            n = link(node_id, "image")
            if int(inputs["batch_index"]) + int(inputs["length"]) > n:
                errors.append(f"{node_id} (ImageFromBatch): takes past the end of a batch of {n}")
            out = {0: int(inputs["length"])}
        elif cls == "MaskComposite":
            dest = link(node_id, "destination")
            fits(node_id, "source", link(node_id, "source"), dest)
            out = {0: dest}
        elif cls == "INPAINT_MaskedFill":
            image, mask = link(node_id, "image"), link(node_id, "mask")
            if image != mask:  # asserts equal sizes, no broadcast
                errors.append(f"{node_id} (INPAINT_MaskedFill): image batch {image} but mask batch {mask}")
            out = {0: image}
        elif cls in ("INPAINT_MaskedBlur", "INPAINT_InpaintWithModel", w.NATIVE_LAMA_NODE):
            image = link(node_id, "image")
            fits(node_id, "mask", link(node_id, "mask"), image)
            out = {0: image}
        elif cls == "INPAINT_ColorMatch":
            target = link(node_id, "target")
            fits(node_id, "reference", link(node_id, "reference"), target)
            fits(node_id, "exclude_mask", link(node_id, "exclude_mask"), target)
            out = {0: target}
        elif cls == "ImageCompositeMasked":
            dest = link(node_id, "destination")
            # ComfyUI repeats or CUTS the source to the destination's batch: a
            # larger source would silently lose variations.
            fits(node_id, "source", link(node_id, "source"), dest)
            fits(node_id, "mask", link(node_id, "mask"), dest)
            out = {0: dest}
        elif cls == "JoinImageWithAlpha":
            image = link(node_id, "image")
            fits(node_id, "alpha", link(node_id, "alpha"), image)
            out = {0: image}
        elif cls == "SetLatentNoiseMask":
            samples = link(node_id, "samples")
            fits(node_id, "mask", link(node_id, "mask"), samples)
            out = {0: samples}
        elif cls in ("InpaintModelConditioning", "INPAINT_VAEEncodeInpaintConditioning"):
            pixels = link(node_id, "pixels")
            fits(node_id, "mask", link(node_id, "mask"), pixels)
            out = {0: pixels, 1: pixels, 2: pixels, 3: pixels}
        elif cls == "TextEncodeQwenImageEditPlus":
            out = {0: link(node_id, "image1")}
        elif cls == "INPAINT_ApplyFooocusInpaint":
            out = {0: link(node_id, "latent")}  # the inpaint head feature's batch
        elif cls == "KSampler":
            latent = link(node_id, "latent_image")
            for name in ("model", "positive", "negative"):
                fits(node_id, name, link(node_id, name), latent)
            out = {0: latent}
        elif cls == "SaveImage":
            out = {}
        else:
            raise BatchError(f"{node_id}: no batch rule for {cls}; add one to this test")
        memo[node_id] = out
        return out

    for node_id in graph:
        outputs(node_id)
    return {(k, s): n for k, out in memo.items() for s, n in out.items()}, errors


def saved_batch(graph: dict) -> int:
    batches, _ = graph_batches(graph)
    src = graph[w.OUTPUT_NODE]["inputs"]["images"]
    return batches[(src[0], src[1])]


SD15 = ModelSpec("sd15.ckpt", template="sd15_inpaint")
SDXL = ModelSpec("realvisxl.safetensors", arch="sdxl")
FOOOCUS = ModelSpec("realvisxl.safetensors", arch="sdxl", fooocus_head="h.pth", fooocus_patch="p.patch")
FLUX = ModelSpec(arch="flux", unet="flux1-fill.gguf", clip1="t5.gguf", clip2="clip_l.safetensors", vae="ae.sft")
FLUX_SAFE = ModelSpec(arch="flux", unet="flux1-fill.safetensors", clip1="t5.sft", clip2="clip_l.sft", vae="ae.sft")
QWEN = ModelSpec(arch="qwen_image", unet="qwen-edit.gguf", clip1="qwen2.5-vl.gguf", vae="qwen_vae.sft")
RORem = ModelSpec(arch="sdxl", template="sdxl_eraser", unet="rorem.sft", clip1="l.sft", clip2="g.sft", vae="v.sft")
FLUX_ERASER = ModelSpec(
    arch="flux",
    template="flux_eraser",
    unet="flux1-fill.gguf",
    clip1="t5.gguf",
    clip2="clip_l.sft",
    vae="ae.sft",
    lora="removal.sft",
)
LAMA = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt")
BIREFNET = ModelSpec(arch="bg_removal", template="bgremove", bg_removal_model="birefnet.safetensors")

# Plans: the issue's photo (1848 x 4000, 1612 x 1853 selection), a roof strip, a
# refine-only fill and a layout pass.
ISSUE_SEL, ISSUE_IMG = g.Rect(191, 325, 1612, 1853), g.Rect(0, 0, 1848, 4000)
STRIP_SEL, STRIP_IMG = g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273)
SMALL_SEL, SMALL_IMG = g.Rect(730, 1267, 557, 490), g.Rect(0, 0, 1920, 2880)
PROFILE = {"sd15": g.SD15, "sdxl": g.SDXL, "flux": g.FLUX, "qwen_image": g.QWEN, "inpaint_model": g.INPAINT_MODEL}


def _request(plan, model, batch, **extra) -> w.PlannedInpaintRequest:
    base = dict(
        image_png=b"",
        mask_png=b"",
        width=plan.context.width,
        height=plan.context.height,
        work_width=plan.work.width,
        work_height=plan.work.height,
        prompt="a stone wall",
        negative="blurry",
        model=model,
        sampling=Sampling(steps=20, batch=batch, seed=5),
        prefill_style="border",
        tiles=plan.tiles,
        refine_tiles=plan.refine_tiles,
        full_grow=16,
        full_blur=12,
    )
    base.update(extra)
    return w.PlannedInpaintRequest(**base)


def _fill_plans(model):
    """(name, plan, extra request fields) of every fill path the planner gives this model."""
    profile = PROFILE.get(model.arch, g.SD15)
    eraser = model.effective_template in ("sdxl_eraser", "flux_eraser")
    min_scale = 1.0 if eraser else None
    refine = model.effective_template in w.REFINE_TEMPLATES
    out = []
    for sel_name, sel, img in (("issue", ISSUE_SEL, ISSUE_IMG), ("strip", STRIP_SEL, STRIP_IMG)):
        plan = g.plan_fill(sel, img, profile, model.arch, eraser, 0.5, refine=refine, min_scale=min_scale)
        out.append((f"{sel_name}-direct", plan, {}))
        layout = g.plan_fill(sel, img, profile, model.arch, False, 0.5, refine=refine, layout=True)
        if layout.layout:
            out.append((f"{sel_name}-layout", layout, {"tile_denoise": 0.5}))
    small = g.plan_fill(SMALL_SEL, SMALL_IMG, profile, model.arch, False, 0.5, refine=refine)
    out.append(("small", small, {}))
    return out


GENERATIVE = {
    "sd15": SD15,
    "sdxl": SDXL,
    "sdxl-fooocus": FOOOCUS,
    "flux": FLUX,
    "flux-safetensors": FLUX_SAFE,
    "qwen": QWEN,
    "rorem": RORem,
    "flux-eraser": FLUX_ERASER,
}
CAPS = {"inpaint-nodes": w.Capabilities(True), "core-only": w.Capabilities(False)}


class GraphBatchCheckerTests(unittest.TestCase):
    """The checker itself catches the issue #16 mismatch."""

    def test_masked_fill_with_one_mask_for_four_images_is_an_error(self):
        graph = {
            "load_image": {"class_type": "LoadImage", "inputs": {"image": "i"}},
            "rep": {"class_type": "RepeatImageBatch", "inputs": {"image": ["load_image", 0], "amount": 4}},
            "fill": {
                "class_type": "INPAINT_MaskedFill",
                "inputs": {"image": ["rep", 0], "mask": ["load_image", 1], "fill": "neutral", "falloff": 0},
            },
        }
        _, errors = graph_batches(graph)
        self.assertEqual(len(errors), 1)
        self.assertIn("image batch 4 but mask batch 1", errors[0])

    def test_a_source_larger_than_the_destination_is_an_error(self):
        graph = {
            "load_image": {"class_type": "LoadImage", "inputs": {"image": "i"}},
            "rep": {"class_type": "RepeatImageBatch", "inputs": {"image": ["load_image", 0], "amount": 3}},
            "paste": {
                "class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["load_image", 0], "source": ["rep", 0], "x": 0, "y": 0},
            },
        }
        self.assertEqual(len(graph_batches(graph)[1]), 1)

    def test_unknown_nodes_need_a_rule(self):
        with self.assertRaises(BatchError):
            graph_batches({"x": {"class_type": "SomethingNew", "inputs": {}}})


class FillGraphBatchTests(unittest.TestCase):
    def _check(self, name: str, req, caps, expected: int, builder=w.build_fill) -> dict:
        graph = builder(req, "i", "m", 9, caps)
        _, errors = graph_batches(graph)
        self.assertEqual(errors, [], name)
        self.assertEqual(saved_batch(graph), expected, f"{name}: variations saved")
        return graph

    def test_every_family_and_path_with_one_and_four_variations(self):
        checked = 0
        for family, model in GENERATIVE.items():
            for plan_name, plan, extra in _fill_plans(model):
                for style in ("border", "blur"):
                    for caps_name, caps in CAPS.items():
                        for batch in (1, 4):
                            name = f"{family}/{plan_name}/{style}/{caps_name}/batch {batch}"
                            with self.subTest(name):
                                req = _request(plan, model, batch, prefill_style=style, **extra)
                                self._check(name, req, caps, batch)
                                checked += 1
        self.assertGreater(checked, 150)

    def test_the_issue_16_fill_repeats_the_tile_mask_for_the_pre_fill(self):
        plan = g.plan_fill(ISSUE_SEL, ISSUE_IMG, g.SDXL, "sdxl", False, 0.5, refine=True)
        self.assertGreater(len(plan.tiles), 1)
        self.assertFalse(plan.layout)
        graph = self._check("issue", _request(plan, FOOOCUS, 4), w.Capabilities(True), 4)
        for i in range(len(plan.tiles)):
            self.assertEqual(graph[f"t{i}_prefill"]["inputs"]["mask"], [f"t{i}_bmask", 0])
            self.assertEqual(graph[f"t{i}_bmrep"]["inputs"]["amount"], 4)
            self.assertEqual(graph[f"t{i}_preblur"]["inputs"]["mask"], [f"t{i}_bmask", 0])
        one = self._check("issue", _request(plan, FOOOCUS, 1), w.Capabilities(True), 1)
        self.assertNotIn("t0_bmask", one)  # one variation: the graph stays as it was
        self.assertEqual(one["t0_prefill"]["inputs"]["mask"], ["t0_mstable", 0])

    def test_neutral_and_telea_pre_fill(self):
        plan = g.plan_fill(STRIP_SEL, STRIP_IMG, g.SD15, "sd15", False, 0.5, refine=True)
        for mode in ("neutral", "telea", "navier-stokes"):
            with self.subTest(mode):
                self._check(mode, _request(plan, SD15, 4, prefill_mode=mode), w.Capabilities(True), 4)

    def test_untiled_fill_and_remove_with_a_generation_model(self):
        plan = g.plan_fill(SMALL_SEL, SMALL_IMG, g.SDXL, "sdxl", True, 0.5)
        for model in (SD15, FOOOCUS, FLUX, QWEN):
            for batch in (1, 4):
                with self.subTest(f"{model.effective_template} {batch}"):
                    req = _request(plan, model, batch, tiles=(), refine_tiles=(), prompt="background")
                    self._check("untiled", req, w.Capabilities(True), batch)

    def test_expand_with_and_without_detail_windows(self):
        plan = g.plan_expand(2896, 2172, 0, 400, 0, 400, g.SDXL)
        tiles = g.expand_detail_tiles(plan, g.SDXL, 72)
        work = g.FillPlan(plan.context, g.WorkingSize(1000, 1024, 0.34))
        for model in (SD15, FOOOCUS, FLUX, QWEN):
            for detail in ((), tiles):
                for batch in (1, 4):
                    with self.subTest(f"{model.effective_template} detail={bool(detail)} {batch}"):
                        req = _request(work, model, batch, mode="expand", prefill=True, refine_tiles=detail)
                        self._check("expand", req, w.Capabilities(True), batch)

    def test_removal_models_save_one_image(self):
        removal = g.plan_removal(g.Rect(192, 913, 806, 355), g.Rect(0, 0, 1920, 1268))
        native = g.plan_native_removal(SMALL_SEL, SMALL_IMG)
        small = g.plan_fill(SMALL_SEL, SMALL_IMG, g.INPAINT_MODEL, "inpaint_model", True, 0.5, refine=True)
        caps_all = w.Capabilities(True, native_lama=True)
        native_lama = ModelSpec(arch="inpaint_model", inpaint_model="big-lama.pt", template="")
        cases = [
            ("tiles", _request(removal, LAMA, 1, prompt="", negative=""), w.Capabilities(True)),
            ("native", _request(native, native_lama, 1, prompt="", negative=""), caps_all),
            ("plain", _request(small, LAMA, 1, prompt="", negative="", refine_tiles=()), w.Capabilities(True)),
            (
                "refine",
                _request(SMALL_PLAN, LAMA, 1, prompt="", negative="", refine_model=SD15, refine_prompt="background"),
                w.Capabilities(True),
            ),
        ]
        for name, req, caps in cases:
            with self.subTest(name):
                self._check(name, req, caps, 1)

    def test_background_removal(self):
        plan = g.FillPlan(g.Rect(0, 0, 800, 600), g.WorkingSize(800, 600, 1.0))
        req = _request(plan, BIREFNET, 1, prompt="", negative="")
        self._check("bgremove", req, w.Capabilities(True, bg_removal=True), 1)

    def test_text_to_image(self):
        from openfill.backend import Txt2ImgRequest

        for batch in (1, 4):
            req = Txt2ImgRequest(
                prompt="a fox", negative="", width=512, height=512, model=SD15, sampling=Sampling(batch=batch)
            )
            graph = w.build_txt2img(req, 3)
            self.assertEqual(graph_batches(graph)[1], [])
            self.assertEqual(saved_batch(graph), batch)


SMALL_PLAN = g.plan_fill(SMALL_SEL, SMALL_IMG, g.SD15, "sd15", False, 0.5, refine=True)


if __name__ == "__main__":
    unittest.main()
