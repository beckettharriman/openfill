# SPDX-License-Identifier: Apache-2.0
"""Live model search (Hugging Face, Civitai) against recorded responses: mapping
rules, exclusions, the 24 hour cache and the offline fallback. No network."""

import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from openfill import catalog as cat
from openfill import hardware, hub

FIXTURES = json.loads((Path(__file__).resolve().parent / "data" / "hub-fixtures.json").read_text(encoding="utf-8"))
RESPONSES = FIXTURES["responses"]
GB = 1024**3
NOW = 1791590400.0  # 2026-10-09
LAPTOP = hardware.Hardware("RTX 4070 Laptop", 8 * GB, 32 * GB, 400 * GB, "Windows 11", "nvidia-smi")


def response(url):
    return copy.deepcopy(RESPONSES[url])


def first(substring):
    return next(u for u in RESPONSES if substring in u)


class FakeOpener:
    """Serves the fixtures by URL; anything else fails like an unreachable host."""

    def __init__(self, fail_all=False):
        self.fail_all = fail_all
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.full_url
        if self.fail_all or url not in RESPONSES:
            raise URLError("getaddrinfo failed")
        return _Resp(json.dumps(RESPONSES[url]).encode("utf-8"))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class HelperTests(unittest.TestCase):
    def test_quant_of(self):
        self.assertEqual(hub.quant_of("flux1-fill-dev-Q4_K_S.gguf"), "Q4_K_S")
        self.assertEqual(hub.quant_of("qwen-image-edit-2511-Q4_K_M.gguf"), "Q4_K_M")
        self.assertEqual(hub.quant_of("flux1-fill-dev-Q8_0.gguf"), "Q8_0")
        self.assertEqual(hub.quant_of("flux1-fill-dev-fp8.safetensors"), "FP8")
        self.assertEqual(hub.quant_of("juggernautXL_ragnarok.safetensors"), "")

    def test_excluded(self):
        for name in (
            "RunDiffusion/Juggernaut-XL-Lightning",
            "sdxl-turbo",
            "foo/sdxl-lcm",
            "ByteDance/Hyper-SD",
            "mit-han-lab/svdq-int4-flux.1-fill-dev",
            "Novice25/Qwen-Image-Edit-Rapid-AIO-GGUF",
            "ChrisColeTech/qwen-image-edit-uncensored-GGUF",
            "camenduru/FLUX.1-Fill-dev-ungated",
            "transformer/diffusion_pytorch_model-00001-of-00003.safetensors",
        ):
            self.assertTrue(hub.excluded(name), name)
        for name in ("RunDiffusion/Juggernaut-XI-v11", "hyperrealism-xl", "YarvixPA/FLUX.1-Fill-dev-GGUF"):
            self.assertFalse(hub.excluded(name), name)

    def test_pick_file_follows_graphics_memory(self):
        files = [
            {"name": f"m-{q}.gguf", "size": s * GB}
            for q, s in (("Q4_K_M", 7), ("Q5_K_M", 8), ("Q8_0", 12), ("Q3_K_S", 5))
        ]
        small = hardware.Hardware(vram_bytes=8 * GB)
        medium = hardware.Hardware(vram_bytes=12 * GB)
        large = hardware.Hardware(vram_bytes=24 * GB)
        self.assertEqual(hub.pick_file(files, small)["name"], "m-Q4_K_M.gguf")
        self.assertEqual(hub.pick_file(files, None)["name"], "m-Q4_K_M.gguf")
        self.assertEqual(hub.pick_file(files, medium)["name"], "m-Q5_K_M.gguf")
        self.assertEqual(hub.pick_file(files, large)["name"], "m-Q8_0.gguf")
        st = [{"name": "a-bf16.safetensors", "size": 23 * GB}, {"name": "a-fp8.safetensors", "size": 12 * GB}]
        self.assertEqual(hub.pick_file(st, small)["name"], "a-fp8.safetensors")
        self.assertEqual(hub.pick_file(st, large)["name"], "a-bf16.safetensors")
        self.assertIsNone(hub.pick_file([], small))

    def test_search_urls(self):
        url = hub.hf_search_url({"search": "FLUX.1-Fill", "sort": "downloads"})
        self.assertTrue(url.startswith("https://huggingface.co/api/models?"))
        self.assertIn("direction=-1", url)
        self.assertIn("expand%5B%5D=gated", url)
        self.assertEqual(
            hub.hf_details_url("YarvixPA/FLUX.1-Fill-dev-GGUF"),
            "https://huggingface.co/api/models/YarvixPA/FLUX.1-Fill-dev-GGUF?blobs=true",
        )
        civ = hub.civitai_url("SDXL 1.0")
        for part in ("types=Checkpoint", "baseModels=SDXL+1.0", "sort=Most+Downloaded", "nsfw=false"):
            self.assertIn(part, civ)


class TolerantLoadingTests(unittest.TestCase):
    def test_one_bad_cache_entry_does_not_drop_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            good = {"t": NOW, "data": {"a": 1}}
            path.write_text(
                json.dumps(
                    {"version": 1, "responses": {"u1": good, "u2": {"t": "abc", "data": 1}, "u3": 5, "u4": {"t": 1}}}
                ),
                encoding="utf-8",
            )
            cache = hub.Cache(path, now=lambda: NOW)
            self.assertEqual(cache.get("u1"), {"a": 1})
            self.assertIsNone(cache.get("u2"))
            self.assertEqual(set(cache.data), {"u1"})
            path.write_text(json.dumps({"version": 1, "responses": [1, 2]}), encoding="utf-8")
            self.assertEqual(hub.Cache(path).data, {})
            path.write_text("[" * 100000, encoding="utf-8")
            self.assertEqual(hub.Cache(path).data, {})

    def test_odd_repository_shapes_do_not_abort_the_search(self):
        bundled = cat.load_catalog()
        flux = hub.FAMILY_SPECS["flux-fill"]
        base = cat.by_id(bundled, flux.base_id)
        details = response(first("YarvixPA/FLUX.1-Fill-dev-GGUF?"))
        details["siblings"] = ["junk", *details["siblings"]]
        for s in details["siblings"]:
            if isinstance(s, dict):
                s["lfs"] = "oops"
        self.assertIsNotNone(hub.hf_candidate(flux, details, base, LAPTOP) or True)
        trimmed = hub._trim("u", {"siblings": [], "cardData": ["x"], "id": "a/b"})
        self.assertEqual(trimmed["cardData"]["license"], None)

    def test_repository_without_a_commit_is_not_offered(self):
        bundled = cat.load_catalog()
        flux = hub.FAMILY_SPECS["flux-fill"]
        base = cat.by_id(bundled, flux.base_id)
        details = response(first("YarvixPA/FLUX.1-Fill-dev-GGUF?"))
        self.assertIsNotNone(hub.hf_candidate(flux, details, base, LAPTOP))
        details["sha"] = None
        self.assertIsNone(hub.hf_candidate(flux, details, base, LAPTOP))


class HuggingFaceMappingTests(unittest.TestCase):
    def setUp(self):
        self.bundled = cat.load_catalog()
        self.flux = hub.FAMILY_SPECS["flux-fill"]
        self.flux_base = cat.by_id(self.bundled, self.flux.base_id)

    def test_gguf_repository(self):
        details = response(first("YarvixPA/FLUX.1-Fill-dev-GGUF?"))
        raw = hub.hf_candidate(self.flux, details, self.flux_base, LAPTOP)
        main = raw["files"][0]
        self.assertTrue(raw["id"].startswith("hf-YarvixPA--FLUX.1-Fill-dev-GGUF--"))
        self.assertEqual(main["role"], "unet")
        self.assertTrue(
            main["dest"].startswith("unet/hf-") and main["dest"].endswith("/flux1-fill-dev-Q4_K_S.gguf")
        )  # Q4_K_M is not offered; next preference
        self.assertRegex(main["url"], r"^https://huggingface\.co/YarvixPA/FLUX\.1-Fill-dev-GGUF/resolve/[0-9a-f]{40}/")
        lfs = next(s for s in details["siblings"] if s["rfilename"] == "flux1-fill-dev-Q4_K_S.gguf")["lfs"]
        self.assertEqual(main["sha256"], lfs["sha256"])
        self.assertEqual(main["size"], lfs["size"])
        # companions are the bundled FLUX text encoders and VAE, optional, with their own gating
        companions = {f["role"]: f for f in raw["files"][1:]}
        self.assertEqual(set(companions), {"clip1", "clip2", "vae", "preview"})
        self.assertTrue(all(f["optional"] for f in companions.values()))
        self.assertTrue(companions["vae"]["gated"])
        self.assertEqual([n["folder"] for n in raw["requires_nodes"]], ["ComfyUI-GGUF"])
        self.assertEqual(raw["license"]["name"], "flux-1-dev-non-commercial-license")
        self.assertTrue(raw["license"]["url"].endswith("/blob/main/LICENSE.md"))
        (entry,) = cat.parse_live_entries([raw], self.bundled)
        self.assertEqual(entry.source, "huggingface")
        self.assertTrue(entry.gated)  # through the gated FLUX VAE
        self.assertEqual(cat.login_pages(entry), ["https://huggingface.co/black-forest-labs/FLUX.1-schnell"])

    def test_gated_repository_is_marked(self):
        details = response(first("black-forest-labs/FLUX.1-Fill-dev?"))
        self.assertEqual(details["gated"], "auto")
        raw = hub.hf_candidate(self.flux, details, self.flux_base, hardware.Hardware(vram_bytes=24 * GB))
        self.assertTrue(raw["gated"])
        self.assertTrue(raw["files"][0]["gated"])
        self.assertEqual(
            raw["files"][0]["dest"], f"diffusion_models/{cat.live_folder(raw['id'])}/flux1-fill-dev.safetensors"
        )
        # the same file is on the verified list, so the live copy is dropped
        self.assertEqual(cat.parse_live_entries([raw], self.bundled), [])

    def test_ungated_mirror_is_refused(self):
        details = response(first("camenduru/FLUX.1-Fill-dev-ungated?"))
        self.assertIsNone(hub.hf_candidate(self.flux, details, self.flux_base, LAPTOP))

    def test_no_license_no_candidate(self):
        details = response(first("YarvixPA/FLUX.1-Fill-dev-GGUF?"))
        details["cardData"] = {}
        details["tags"] = [t for t in details["tags"] if not t.startswith("license:")]
        self.assertIsNone(hub.hf_candidate(self.flux, details, self.flux_base, LAPTOP))

    def test_adult_tag_and_pickles_and_subfolders_are_refused(self):
        details = response(first("YarvixPA/FLUX.1-Fill-dev-GGUF?"))
        tagged = dict(details, tags=[*details["tags"], "not-for-all-audiences"])
        self.assertIsNone(hub.hf_candidate(self.flux, tagged, self.flux_base, LAPTOP))
        for path in ("flux1-fill-dev.ckpt", "transformer/flux1-fill-dev-Q4_K_M.gguf", "x/../flux1-fill-Q4_K_M.gguf"):
            sib = {"rfilename": path, "size": 7 * GB, "lfs": {"sha256": "a" * 64, "size": 7 * GB}}
            only = dict(details, siblings=[sib])
            self.assertIsNone(hub.hf_candidate(self.flux, only, self.flux_base, LAPTOP), path)
        nested = dict(
            details,
            siblings=[
                {
                    "rfilename": "split_files/diffusion_models/flux1-fill-dev-Q4_K_M.gguf",
                    "size": 7 * GB,
                    "lfs": {"sha256": "b" * 64, "size": 7 * GB},
                }
            ],
        )
        raw = hub.hf_candidate(self.flux, nested, self.flux_base, LAPTOP)
        self.assertEqual(raw["files"][0]["dest"], f"unet/{cat.live_folder(raw['id'])}/flux1-fill-dev-Q4_K_M.gguf")
        self.assertIn("/split_files/diffusion_models/", raw["files"][0]["url"])
        self.assertEqual(len(cat.parse_live_entries([raw], cat.load_catalog())), 1)

    def test_size_window(self):
        details = response(first("YarvixPA/FLUX.1-Fill-dev-GGUF?"))
        for s in details["siblings"]:
            if s.get("lfs"):
                s["lfs"]["size"] = 100  # a LoRA-sized file is not a FLUX Fill model
        self.assertIsNone(hub.hf_candidate(self.flux, details, self.flux_base, LAPTOP))

    def test_sdxl_checkpoint(self):
        fam = hub.FAMILY_SPECS["sdxl"]
        base = cat.by_id(self.bundled, fam.base_id)
        raw = hub.hf_candidate(fam, response(first("RunDiffusion/Juggernaut-XI-v11?")), base, LAPTOP)
        self.assertEqual(raw["files"][0]["role"], "checkpoint")
        self.assertTrue(raw["files"][0]["dest"].startswith("checkpoints/"))
        self.assertEqual(raw["requires_models"], ["fooocus-inpaint-patch"])
        self.assertEqual(raw["template"], "sdxl_fooocus")
        self.assertEqual(raw["license"]["name"], "cc-by-nc-nd-4.0")


class CivitaiMappingTests(unittest.TestCase):
    def setUp(self):
        self.bundled = cat.load_catalog()
        self.sdxl = hub.FAMILY_SPECS["sdxl"]
        self.sd15 = hub.FAMILY_SPECS["sd15-inpaint"]
        self.items = response(first("baseModels=SDXL+1.0"))["items"]
        self.sd15_items = response(first("baseModels=SD+1.5"))["items"]

    def item(self, items, name):
        return copy.deepcopy(next(i for i in items if i["name"] == name))

    def test_sdxl_checkpoint(self):
        it = self.item(self.items, "Juggernaut XL")
        raw = hub.civitai_candidate(self.sdxl, it, cat.by_id(self.bundled, "sdxl-base"), NOW)
        main = raw["files"][0]
        f = it["modelVersions"][0]["files"][0]
        self.assertEqual(main["size"], round(f["sizeKB"] * 1024))
        self.assertEqual(main["sha256"], f["hashes"]["SHA256"].lower())
        self.assertRegex(main["url"], r"^https://civitai\.com/api/download/models/\d+")
        self.assertEqual(raw["source"], "civitai")
        self.assertTrue(raw["page"].startswith("https://civitai.com/models/"))
        self.assertFalse(raw["gated"])
        self.assertIn("Civitai", raw["license"]["name"])
        self.assertEqual(raw["requires_models"], ["fooocus-inpaint-patch"])
        self.assertEqual(len(cat.parse_live_entries([raw], self.bundled)), 1)

    def test_sdxl_skips_distilled_versions(self):
        raw = hub.civitai_candidate(
            self.sdxl, self.item(self.items, "DreamShaper XL"), cat.by_id(self.bundled, "sdxl-base"), NOW
        )
        if raw is not None:
            self.assertFalse(hub.excluded(raw["name"]), raw["name"])

    def test_sd15_takes_the_inpainting_version(self):
        base = cat.by_id(self.bundled, "sd15-inpainting")
        raw = hub.civitai_candidate(self.sd15, self.item(self.sd15_items, "DreamShaper"), base, NOW)
        self.assertIn("inpainting", raw["name"].lower())
        self.assertEqual(raw["template"], "sd15_inpaint")
        self.assertIsNone(hub.civitai_candidate(self.sd15, self.item(self.sd15_items, "Counterfeit-V3.0"), base, NOW))

    def test_refusals(self):
        base = cat.by_id(self.bundled, "sdxl-base")
        it = self.item(self.items, "Juggernaut XL")
        self.assertIsNone(hub.civitai_candidate(self.sdxl, dict(it, nsfw=True), base, NOW))
        self.assertIsNone(hub.civitai_candidate(self.sdxl, dict(it, availability="EarlyAccess"), base, NOW))
        for mutate in (
            lambda f: f["metadata"].update(format="PickleTensor"),
            lambda f: f.update(sizeKB=f["sizeKB"] + 0.0001),  # exact byte count unknown
            lambda f: f["hashes"].pop("SHA256"),
            lambda f: f.update(virusScanResult="Danger"),
            lambda f: f.update(downloadUrl="https://evil.example/x"),
        ):
            bad = copy.deepcopy(it)
            for v in bad["modelVersions"]:
                for f in v["files"]:
                    mutate(f)
            self.assertIsNone(hub.civitai_candidate(self.sdxl, bad, base, NOW))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "hub-cache.json"
        self.bundled = cat.load_catalog()

    def tearDown(self):
        self.tmp.cleanup()

    def run_search(self, opener, now=NOW, families=("flux-fill", "sdxl"), cache=True):
        return hub.search(
            self.bundled, LAPTOP, self.cache if cache else None, opener=opener, families=families, now=now
        )

    def test_search_maps_validates_and_never_sends_a_token(self):
        opener = FakeOpener()
        result = self.run_search(opener)
        ids = [r["id"] for r in result.entries]
        self.assertTrue(any(i.startswith("hf-YarvixPA") for i in ids), ids)
        self.assertTrue(any(i.startswith("hf-RunDiffusion--Juggernaut-XI") for i in ids), ids)
        self.assertTrue(any(i.startswith("civitai-") for i in ids), ids)
        self.assertFalse(any("Lightning" in i or "svdq" in i or "inpainting" in i for i in ids), ids)
        # every candidate passes the strict live rules and has a checksum
        self.assertEqual(len(cat.parse_live_entries(result.entries, self.bundled)), len(ids))
        self.assertTrue(all(r["files"][0].get("sha256") for r in result.entries))
        # some queries are not in the fixtures: reported, not fatal
        self.assertTrue(result.offline)
        self.assertTrue(any("Hugging Face" in p for p in result.problems))
        self.assertGreater(result.counts["flux-fill/huggingface"], 0)
        self.assertIn("YarvixPA/FLUX.1-Fill-dev-GGUF", result.repo_stats)
        for req in opener.requests:
            self.assertIsNone(req.get_header("Authorization"))
            self.assertTrue(req.full_url.startswith(("https://huggingface.co/api/", "https://civitai.com/api/v1/")))

    def test_mirrors_are_deduplicated(self):
        result = self.run_search(FakeOpener(), families=("flux-fill",))
        shas = [r["files"][0]["sha256"] for r in result.entries]
        self.assertEqual(len(shas), len(set(shas)))
        bundled_shas = {f.sha256 for e in self.bundled for f in e.files}
        self.assertFalse(set(shas) & bundled_shas)

    def test_cache_then_offline(self):
        online = self.run_search(FakeOpener())
        self.assertTrue(self.cache.is_file())
        cached = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(cached["version"], 1)
        # within 24 hours: answered from the cache without touching the network
        again = self.run_search(FakeOpener(fail_all=True), now=NOW + 3600)
        self.assertEqual([r["id"] for r in again.entries], [r["id"] for r in online.entries])
        # after 24 hours and offline: the stale cache is used and the result says so
        stale = self.run_search(FakeOpener(fail_all=True), now=NOW + 25 * 3600)
        self.assertEqual([r["id"] for r in stale.entries], [r["id"] for r in online.entries])
        self.assertTrue(stale.offline)
        self.assertTrue(stale.from_cache)

    def test_offline_without_cache_degrades_to_nothing(self):
        result = self.run_search(FakeOpener(fail_all=True), cache=False)
        self.assertEqual(result.entries, [])
        self.assertTrue(result.offline)
        self.assertIn("verified list", result.summary())

    def test_cache_file_is_trimmed(self):
        self.run_search(FakeOpener(), families=("flux-fill",))
        text = self.cache.read_text(encoding="utf-8")
        data = json.loads(text)["responses"]
        details = data[first("YarvixPA/FLUX.1-Fill-dev-GGUF?")]["data"]
        self.assertEqual(
            set(details), {"id", "author", "sha", "gated", "downloads", "lastModified", "tags", "cardData", "siblings"}
        )
        self.assertFalse(re.search(r"hf_[A-Za-z0-9]{10,}", text))


if __name__ == "__main__":
    unittest.main()
