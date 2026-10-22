# SPDX-License-Identifier: Apache-2.0
"""Per-operation default models and their fallbacks."""

import unittest
from pathlib import Path

from openfill import catalog as cat

CATALOG = Path(__file__).resolve().parent.parent / "models" / "catalog.json"


class DefaultsTests(unittest.TestCase):
    def setUp(self):
        self.entries = cat.load_catalog(CATALOG)
        self.by = {e.id: e for e in self.entries}

    def pick(self, ids, tier, operation="fill"):
        best = cat.tier_default([self.by[i] for i in ids], tier, operation)
        return best.id if best else None

    def test_preference_lists_name_shipped_models(self):
        for ids in (*cat.TIER_PREFERENCE.values(), *cat.EXPAND_PREFERENCE.values(), cat.AUTO_ACTIVE_ORDER):
            for mid in ids:
                self.assertIn(mid, self.by)

    def test_fast_order(self):
        self.assertEqual(self.pick(["sd15-inpainting", "dreamshaper-8-inpainting"], "fast"), "dreamshaper-8-inpainting")
        self.assertEqual(self.pick(["sd15-inpainting", "realvisxl-v5"], "fast"), "sd15-inpainting")
        self.assertIsNone(self.pick(["realvisxl-v5", "realistic-vision-v6-inpainting"], "fast"))

    def test_quality_order(self):
        allq = ["sdxl-base", "juggernaut-xl-v9", "realvisxl-v5"]
        self.assertEqual(self.pick(allq, "quality"), "realvisxl-v5")
        self.assertEqual(self.pick(allq[:2], "quality"), "juggernaut-xl-v9")
        self.assertEqual(self.pick(allq[:1], "quality"), "sdxl-base")

    def test_expand_suggestion(self):
        both = [self.by[i] for i in ("dreamshaper-8-inpainting", "sd15-inpainting", "realvisxl-v5")]
        sug = cat.expand_suggestion(both, self.by["realvisxl-v5"])
        self.assertEqual(sug.id, "sd15-inpainting")
        self.assertEqual(cat.expand_suggestion(both, self.by["dreamshaper-8-inpainting"]).id, "sd15-inpainting")
        self.assertIsNone(cat.expand_suggestion(both, self.by["sd15-inpainting"]))
        no_default = [self.by["realvisxl-v5"]]
        self.assertIsNone(cat.expand_suggestion(no_default, self.by["realvisxl-v5"]))
        self.assertIsNone(cat.expand_suggestion(both, None))

    def test_expand_defaults(self):
        both = ["dreamshaper-8-inpainting", "sd15-inpainting", "realvisxl-v5"]
        for tier in ("fast", "quality"):
            self.assertEqual(self.pick(both, tier, "expand"), "sd15-inpainting")
            self.assertEqual(self.pick(both[:1] + both[2:], tier, "expand"), "dreamshaper-8-inpainting")
        self.assertIsNone(self.pick(["realvisxl-v5", "juggernaut-xl-v9"], "quality", "expand"))

    def test_remove_order(self):
        self.assertEqual(self.pick(["mat-places512", "lama-big"], "remove"), "lama-big")
        self.assertEqual(self.pick(["mat-places512"], "remove"), "mat-places512")

    def test_flux_and_qwen_never_automatic(self):
        opt = ["flux1-fill-dev-gguf-q4", "flux1-fill-dev", "qwen-image-edit-2511-gguf-q4"]
        for mid in opt:
            self.assertTrue(cat.is_opt_in(self.by[mid]))
        for tier in ("fast", "quality", "remove"):
            self.assertIsNone(self.pick(opt, tier))
        self.assertIsNone(cat.auto_active([self.by[m] for m in opt]))
        both = [self.by[m] for m in (*opt, "sd15-inpainting")]
        self.assertEqual(cat.auto_active(both).id, "sd15-inpainting")

    def test_auto_active_and_manual_choice(self):
        inst = [self.by[m] for m in ("sd15-inpainting", "realvisxl-v5", "lama-big")]
        self.assertEqual(cat.auto_active(inst).id, "realvisxl-v5")
        self.assertEqual(cat.auto_active([self.by["lama-big"]]).id, "lama-big")
        self.assertIsNone(cat.suggest_active(inst, ""))  # nothing set by hand: automatic
        self.assertEqual(cat.suggest_active(inst, "flux1-fill-dev"), "realvisxl-v5")

    def test_qwen_is_flagged_as_needing_more_than_8gb(self):
        from openfill import hardware as hw

        self.assertGreater(hw.OFFLOAD_FLOOR_GB["qwen_image"], 8.0)

    def test_first_run_recommends_starter_set_then_upgrade(self):
        rows = [
            {
                "id": e.id,
                "name": e.id,
                "installed": False,
                "partial": [],
                "runnable": True,
                "origin": "bundled",
                "family": e.family or e.arch,
                "fit": "comfortable",
                "downloads": 0,
                "updated": "",
                "checksum_known": True,
            }
            for e in self.entries
        ]
        rec = [r["id"] for r in cat.recommended(rows, 5)]
        self.assertEqual(rec[:3], ["lama-big", "dreamshaper-8-inpainting", "realvisxl-v5"])
        self.assertFalse({"flux1-fill-dev", "flux1-fill-dev-gguf-q4", "qwen-image-edit-2511-gguf-q4"} & set(rec))
        self.assertIn("RealVisXL", cat.FIRST_RUN_HINT)
        self.assertIn("LaMa", cat.FIRST_RUN_HINT)
        self.assertIn("DreamShaper", cat.FIRST_RUN_HINT)


class RemovalRoutingTests(unittest.TestCase):
    """Empty prompt or Remove object never reaches FLUX (or Qwen)."""

    def sub(self, installed_ids, choice):
        by = {e.id: e for e in cat.load_catalog(CATALOG)}
        best = cat.removal_substitute([by[i] for i in installed_ids], by[choice])
        return best.id if best else None

    def test_flux_replaced_by_fast_then_quality_default(self):
        flux = "flux1-fill-dev-gguf-q4"
        self.assertEqual(self.sub([flux, "sd15-inpainting", "realvisxl-v5"], flux), "sd15-inpainting")
        self.assertEqual(
            self.sub([flux, "dreamshaper-8-inpainting", "sd15-inpainting"], flux), "dreamshaper-8-inpainting"
        )
        self.assertEqual(self.sub([flux, "realvisxl-v5"], flux), "realvisxl-v5")

    def test_only_opt_in_installed_has_no_substitute(self):
        self.assertIsNone(self.sub(["flux1-fill-dev-gguf-q4"], "flux1-fill-dev-gguf-q4"))
        self.assertIsNone(self.sub(["qwen-image-edit-2511-gguf-q4"], "qwen-image-edit-2511-gguf-q4"))

    def test_other_models_are_kept(self):
        self.assertEqual(self.sub(["sd15-inpainting", "flux1-fill-dev-gguf-q4"], "sd15-inpainting"), "sd15-inpainting")


if __name__ == "__main__":
    unittest.main()
