# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""No-model scene description used to augment prompts."""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gimp-openfill"))

from openfill import caption  # noqa: E402


def buf(w, h, fn):
    data = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * 3
            data[o : o + 3] = bytes(max(0, min(255, int(v))) for v in fn(x, y))
    return bytes(data)


class SampleTest(unittest.TestCase):
    def test_downsample_keeps_channels_and_limits_side(self):
        data = buf(400, 100, lambda x, y: (x % 256, y, 7))
        w, h, out = caption.sample(400, 100, data, 3, side=100)
        self.assertEqual((w, h), (100, 25))
        self.assertEqual(len(out), w * h * 3)
        self.assertEqual(out[3:6], bytes((4, 0, 7)))  # second sample is pixel (4, 0)

    def test_small_input_unchanged(self):
        data = buf(10, 10, lambda x, y: (1, 2, 3))
        self.assertEqual(caption.sample(10, 10, data, 3), (10, 10, data))


class DescribeTest(unittest.TestCase):
    def test_night(self):
        data = buf(64, 64, lambda x, y: (8, 9, 14))
        text = caption.describe(64, 64, data, 3)
        self.assertIn("night", text)
        self.assertTrue(text.endswith("photo"))

    def test_white_product_background_is_studio(self):
        data = buf(64, 64, lambda x, y: (247, 247, 246))
        self.assertIn("studio", caption.describe(64, 64, data, 3))

    def test_sky_and_meadow_is_daylight(self):
        rnd = random.Random(1)

        def scene(x, y):
            if y < 24:
                return 120, 170, 235  # blue sky
            v = rnd.randint(-30, 30)
            return 70 + v, 140 + v, 50 + v  # grass

        text = caption.describe(64, 64, buf(64, 64, scene), 3)
        self.assertIn("daylight", text)

    def test_warm_dim_interior(self):
        text = caption.describe(64, 64, buf(64, 64, lambda x, y: (120, 80, 40)), 3)
        self.assertIn("warm", text)

    def test_mask_excludes_selection(self):
        # the selected half is bright white; only the dark half counts
        data = buf(64, 64, lambda x, y: (250, 250, 250) if x < 32 else (10, 10, 12))
        mask = bytes(255 if x < 32 else 0 for y in range(64) for x in range(64))
        self.assertIn("night", caption.describe(64, 64, data, 3, mask))

    def test_too_few_pixels(self):
        self.assertEqual(caption.describe(3, 3, bytes(27), 3), "")
        self.assertEqual(caption.describe(8, 8, bytes(192), 3, bytes([255]) * 64), "")


class AugmentTest(unittest.TestCase):
    def test_appends_after_prompt(self):
        self.assertEqual(
            caption.augment("a red fox", "soft overcast daylight, muted colours, photo"),
            "a red fox, soft overcast daylight, muted colours, photo",
        )

    def test_prepend(self):
        self.assertEqual(
            caption.augment("a red fox", "dusk light, photo", prepend=True), "dusk light, photo, a red fox"
        )

    def test_no_duplicates_and_empty(self):
        self.assertEqual(caption.augment("a cat, photo", "low light, photo"), "a cat, photo, low light")
        self.assertEqual(caption.augment("", "low light, photo"), "")
        self.assertEqual(caption.augment("a cat", ""), "a cat")


if __name__ == "__main__":
    unittest.main()
