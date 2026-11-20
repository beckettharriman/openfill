# SPDX-License-Identifier: Apache-2.0
"""Custom ComfyUI workflows (issue #18, Advanced): checking the file, finding where the
picture, mask, prompt and seed go, the overrides, the graph sent, and App's model choice."""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from openfill import custom_workflow as cw
from openfill import workflow as w
from openfill.backend import InpaintRequest, ModelSpec, Sampling

# A ComfyUI "Export (API)" of a plain SD 1.5 inpaint workflow: the picture's alpha is the mask.
ALPHA_WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 1234,
            "steps": 20,
            "cfg": 7,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["11", 0],
        },
        "_meta": {"title": "KSampler"},
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15-inpainting.safetensors"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["4", 1]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "example.png", "upload": "image"}},
    "11": {
        "class_type": "VAEEncodeForInpaint",
        "inputs": {"pixels": ["10", 0], "vae": ["4", 2], "mask": ["10", 1], "grow_mask_by": 6},
    },
}

# FLUX style: a guidance node between the text and the sampler, a mask loader, RandomNoise.
MASK_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "photo.png"}, "_meta": {"title": "Load Image"}},
    "2": {"class_type": "LoadImageMask", "inputs": {"image": "mask.png", "channel": "alpha"}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "x", "clip": ["9", 0]}},
    "6": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["5", 0], "guidance": 30}},
    "7": {
        "class_type": "CFGGuider",
        "inputs": {"model": ["9", 0], "positive": ["6", 0], "negative": ["6", 0], "cfg": 1},
    },
    "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
    "9": {"class_type": "UNETLoader", "inputs": {"unet_name": "m.safetensors", "weight_dtype": "default"}},
    "12": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["13", 0], "mask": ["2", 0]}},
    "13": {"class_type": "VAEEncode", "inputs": {"pixels": ["1", 0], "vae": ["9", 0]}},
    "14": {"class_type": "PreviewImage", "inputs": {"images": ["13", 0]}},
    "15": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
}


def write(tmp: str, data, name="wf.json") -> Path:
    path = Path(tmp) / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


class ParseTests(unittest.TestCase):
    def test_a_valid_export_is_kept_as_it_is(self):
        graph = cw.parse(json.dumps(ALPHA_WORKFLOW))
        self.assertEqual(graph["3"]["inputs"]["positive"], ["6", 0])
        self.assertEqual(graph["3"]["_meta"], {"title": "KSampler"})

    def test_the_editor_format_is_explained(self):
        with self.assertRaisesRegex(cw.CustomWorkflowError, "Export \\(API\\)"):
            cw.parse(json.dumps({"nodes": [], "links": [], "version": 0.4}))

    def test_bad_files_are_refused(self):
        cases = {
            "not json": "not valid JSON",
            "[]": "holds no nodes",
            "{}": "holds no nodes",
            '{"1": {"inputs": {}}}': "no class_type",
            '{"1": {"class_type": "X", "inputs": []}}': "inputs must be an object",
            '{"1": {"class_type": "X", "inputs": {"a": ["9", 0]}}}': "not in the workflow",
            '{"1": {"class_type": "X", "inputs": {"a": [1, 2, 3]}}}': "must be a link",
            '{"1": {"class_type": "X", "inputs": {"a": NaN}}}': "not finite",
            '{"../x": {"class_type": "X", "inputs": {}}}': "Bad node id",
            '{"1": {"class_type": "X\\u0000", "inputs": {}}}': "bad class_type",
        }
        for text, why in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(cw.CustomWorkflowError, why):
                cw.parse(text)

    def test_size_and_depth_limits(self):
        with self.assertRaisesRegex(cw.CustomWorkflowError, "too large"):
            cw.parse(b" " * (cw.MAX_FILE_BYTES + 1))
        deep = {"1": {"class_type": "X", "inputs": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}}}
        with self.assertRaisesRegex(cw.CustomWorkflowError, "nested too deeply"):
            cw.parse(json.dumps(deep))
        with self.assertRaises(cw.CustomWorkflowError):
            cw.parse("[" * 100000)  # RecursionError in the decoder is caught

    def test_a_saved_prompt_request_body_is_accepted(self):
        graph = cw.parse(json.dumps({"prompt": ALPHA_WORKFLOW, "client_id": "x"}))
        self.assertIn("10", graph)

    def test_only_the_api_keys_are_kept(self):
        data = {"1": {"class_type": "LoadImage", "inputs": {"image": "a"}, "is_changed": "x", "_meta": {"title": "T"}}}
        self.assertEqual(
            cw.parse(json.dumps(data))["1"],
            {"class_type": "LoadImage", "inputs": {"image": "a"}, "_meta": {"title": "T"}},
        )


class DetectTests(unittest.TestCase):
    def test_alpha_workflow(self):
        m = cw.detect(cw.parse(json.dumps(ALPHA_WORKFLOW)))
        self.assertEqual(m.image, "10")
        self.assertEqual(m.mask, "")  # the selection goes in the picture's alpha
        self.assertEqual(m.prompt, (("6", "text"),))
        self.assertEqual(m.negative, (("7", "text"),))
        self.assertEqual(m.seed, (("3", "seed"),))
        self.assertEqual(m.output, "9")

    def test_mask_loader_guidance_and_random_noise(self):
        m = cw.detect(cw.parse(json.dumps(MASK_WORKFLOW)))
        self.assertEqual((m.image, m.mask), ("1", "2"))
        self.assertEqual(m.prompt, (("5", "text"),))  # through FluxGuidance
        self.assertEqual(m.negative, ())  # the same text node: not set twice
        self.assertEqual(m.seed, (("8", "noise_seed"),))
        self.assertEqual(m.output, "15")  # a SaveImage before a PreviewImage

    def test_a_second_load_image_used_as_a_mask(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
            "3": {"class_type": "ImageScale", "inputs": {"image": ["2", 0], "width": 8, "height": 8}},
            "4": {"class_type": "ImageToMask", "inputs": {"image": ["3", 0], "channel": "red"}},
            "5": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        self.assertEqual(cw.detect(graph).mask, "2")

    def test_a_text_from_a_string_node_is_followed(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "hello"}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ["2", 0], "clip": ["9", 0]}},
            "4": {"class_type": "KSampler", "inputs": {"positive": ["3", 0], "seed": 1}},
            "9": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c"}},
            "5": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        self.assertEqual(cw.detect(graph).prompt, (("2", "value"),))

    def test_without_a_loader_or_an_output(self):
        with self.assertRaisesRegex(cw.CustomWorkflowError, "Load Image"):
            cw.detect({"1": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}})
        with self.assertRaisesRegex(cw.CustomWorkflowError, "Save Image"):
            cw.detect({"1": {"class_type": "LoadImage", "inputs": {"image": "a"}}})


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.graph = cw.parse(json.dumps(MASK_WORKFLOW))
        self.mapping = cw.detect(self.graph)

    def test_overrides(self):
        m = cw.apply_inputs(self.graph, self.mapping, "image=1, mask=alpha; prompt=5.text\nseed=none")
        self.assertEqual((m.image, m.mask, m.prompt, m.seed), ("1", "", (("5", "text"),), ()))
        m = cw.apply_inputs(self.graph, self.mapping, "seed=8, output=14, negative=5")
        self.assertEqual((m.seed, m.output, m.negative), ((("8", "noise_seed"),), "14", (("5", "text"),)))

    def test_bad_overrides_say_what_is_wrong(self):
        for text, why in {
            "image": "name=node",
            "colour=1": "name=node",
            "image=99": "no node 99",
            "image=5": "not an image loader",
            "output=5": "no images input",
            "prompt=5.nope": "no such input",
            "prompt=7.positive": "connected to another node",
        }.items():
            with self.subTest(text=text), self.assertRaisesRegex(cw.CustomWorkflowError, why):
                cw.apply_inputs(self.graph, self.mapping, text)


class BuildTests(unittest.TestCase):
    def build(self, data, size=(100, 80), work=(100, 80), inputs=""):
        with tempfile.TemporaryDirectory() as tmp:
            wf = cw.load(write(tmp, data), inputs)
        return wf, cw.build(wf, "up-image.png", "up-mask.png", "a dog", "ugly", 77, size, work, "openfill/custom-x")

    def test_inputs_are_filled_in_and_the_result_saved_by_openfill(self):
        wf, graph = self.build(ALPHA_WORKFLOW)
        self.assertEqual(graph["10"]["inputs"]["image"], "up-image.png")
        self.assertEqual(graph["6"]["inputs"]["text"], "a dog")
        self.assertEqual(graph["7"]["inputs"]["text"], "ugly")
        self.assertEqual(graph["3"]["inputs"]["seed"], 77)
        self.assertNotIn("9", graph)  # the user's SaveImage is left out
        save = cw.output_node(graph)
        self.assertEqual(save, cw.OUTPUT_NODE)
        self.assertEqual(graph[save]["inputs"]["filename_prefix"], "openfill/custom-x")
        scale = graph[save]["inputs"]["images"][0]
        self.assertEqual(graph[scale]["inputs"]["image"], ["8", 0])
        self.assertEqual((graph[scale]["inputs"]["width"], graph[scale]["inputs"]["height"]), (100, 80))
        self.assertEqual(wf.graph["10"]["inputs"]["image"], "example.png")  # the loaded workflow is not changed

    def test_inputs_are_resized_to_the_working_size(self):
        _wf, graph = self.build(ALPHA_WORKFLOW, size=(100, 80), work=(512, 400))
        pixels = graph["11"]["inputs"]["pixels"]
        mask = graph["11"]["inputs"]["mask"]
        self.assertEqual(graph[pixels[0]]["class_type"], "ImageScale")
        self.assertEqual(graph[pixels[0]]["inputs"]["image"], ["10", 0])
        self.assertEqual((graph[pixels[0]]["inputs"]["width"], graph[pixels[0]]["inputs"]["height"]), (512, 400))
        self.assertEqual(graph[mask[0]]["class_type"], "ImageToMask")  # the alpha mask, resized too
        out = graph[cw.output_node(graph)]["inputs"]["images"][0]
        self.assertEqual((graph[out]["inputs"]["width"], graph[out]["inputs"]["height"]), (100, 80))

    def test_mask_loader_gets_the_mask(self):
        _wf, graph = self.build(MASK_WORKFLOW, work=(128, 96))
        self.assertEqual(graph["2"]["inputs"]["image"], "up-mask.png")
        self.assertEqual(graph["8"]["inputs"]["noise_seed"], 77)
        self.assertNotIn("14", graph)  # the preview node goes too
        noise_mask = graph["12"]["inputs"]["mask"]
        self.assertNotEqual(noise_mask, ["2", 0])  # resized to the working size
        self.assertTrue(noise_mask[0].startswith(cw.PREFIX))

    def test_no_id_clash_with_the_users_nodes(self):
        data = dict(ALPHA_WORKFLOW)
        data[cw.OUTPUT_NODE] = {"class_type": "Note", "inputs": {}}
        _wf, graph = self.build(data)
        self.assertEqual(graph[cw.OUTPUT_NODE]["class_type"], "Note")
        self.assertEqual(graph[cw.output_node(graph)]["class_type"], "SaveImage")

    def test_workflow_builder_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = cw.load(write(tmp, ALPHA_WORKFLOW))
        spec = ModelSpec(arch="custom", template="custom_workflow", custom=wf)
        self.assertEqual(spec.effective_template, "custom_workflow")
        req = InpaintRequest(b"", b"", 64, 64, 64, 64, "p", "n", spec, Sampling(seed=3))
        graph = w.build_fill(req, "i.png", "m.png", 5, w.Capabilities(), "openfill/fill-x")
        self.assertEqual(graph["3"]["inputs"]["seed"], 5)
        self.assertEqual(w.total_steps(graph), 20)
        with self.assertRaisesRegex(ValueError, "no custom workflow"):
            w.build_fill(
                InpaintRequest(b"", b"", 8, 8, 8, 8, "", "", ModelSpec(arch="custom")), "i", "m", 0, w.Capabilities()
            )

    def test_alpha_and_mask_pictures(self):
        rgba = bytes([1, 2, 3, 255, 4, 5, 6, 255])
        self.assertEqual(cw.rgba_with_mask(rgba, bytes([255, 0])), bytes([1, 2, 3, 0, 4, 5, 6, 255]))
        self.assertEqual(cw.mask_rgba(bytes([255, 10])), bytes([255, 255, 255, 0, 10, 10, 10, 245]))

    def test_describe_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("image 10 (LoadImage)", cw.describe_file(str(write(tmp, ALPHA_WORKFLOW))))
            self.assertIn("Export (API)", cw.describe_file(str(write(tmp, {"nodes": [], "links": []}))))
            self.assertIn("Can't read", cw.describe_file(str(Path(tmp) / "missing.json")))
        self.assertEqual(cw.describe_file(""), "")


class AppTests(unittest.TestCase):
    def app(self, **settings):
        from openfill.app import App
        from openfill.config import Settings

        app = App.__new__(App)
        settings.setdefault("backend", "comfyui")
        app.settings = Settings(**settings)
        app.log = logging.getLogger("test")
        app.require_backend_install = lambda: None
        return app

    def test_the_choice_runs_the_users_workflow(self):
        from openfill.app import CUSTOM_WORKFLOW_ID

        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(custom_workflow=str(write(tmp, ALPHA_WORKFLOW, "My Inpaint.json")))
            entry, spec = app.resolve_model(CUSTOM_WORKFLOW_ID)
        self.assertIsNone(entry)
        self.assertEqual((spec.arch, spec.effective_template), ("custom", "custom_workflow"))
        self.assertEqual(spec.custom.mapping.image, "10")
        self.assertEqual(app.custom_workflow_name(), "Custom workflow (My Inpaint)")

    def test_problems_are_plain_messages(self):
        from openfill.app import CUSTOM_WORKFLOW_ID, CustomWorkflowProblem

        with self.assertRaisesRegex(CustomWorkflowProblem, "No custom workflow is set"):
            self.app().custom_workflow_spec()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(write(tmp, ALPHA_WORKFLOW))
            for backend in ("remote", "cloud"):  # never sent to Remote GPU, OpenRouter or Gemini
                with self.assertRaisesRegex(CustomWorkflowProblem, "this computer only"):
                    self.app(custom_workflow=path, backend=backend).custom_workflow_spec()
            gemini = self.app(custom_workflow=path, backend="cloud")
            gemini.settings.cloud_provider = "gemini"
            with self.assertRaisesRegex(CustomWorkflowProblem, "this computer only"):
                gemini.custom_workflow_spec()
            bad = str(write(tmp, {"nodes": []}, "ui.json"))
            with self.assertRaisesRegex(CustomWorkflowProblem, "Export \\(API\\)"):
                self.app(custom_workflow=bad).resolve_model(CUSTOM_WORKFLOW_ID)
            with self.assertRaisesRegex(CustomWorkflowProblem, "no node 99"):
                self.app(custom_workflow=path, custom_workflow_inputs="image=99").custom_workflow_spec()
        self.assertEqual(self.app().custom_workflow_name(), "")

    def test_settings_are_saved(self):
        from openfill.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Settings(custom_workflow="C:/wf.json", custom_workflow_inputs="image=10").save(path)
            loaded = Settings.load(path)
        self.assertEqual((loaded.custom_workflow, loaded.custom_workflow_inputs), ("C:/wf.json", "image=10"))


if __name__ == "__main__":
    unittest.main()
