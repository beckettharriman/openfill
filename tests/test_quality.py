# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Variation ranking metrics on synthetic buffers."""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gimp-openfill"))

from openfill import quality  # noqa: E402

W = H = 48
SEL = (16, 16, 16, 16)  # x, y, w, h


def make(fn) -> quality.Buffer:
    data = bytearray(W * H * 3)
    for y in range(H):
        for x in range(W):
            r, g, b = fn(x, y)
            o = (y * W + x) * 3
            data[o : o + 3] = bytes((max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))))
    return quality.Buffer(W, H, bytes(data))


def inside(x, y, sel=SEL):
    sx, sy, sw, sh = sel
    return sx <= x < sx + sw and sy <= y < sy + sh


def mask_bytes(sel=SEL) -> bytes:
    return bytes(255 if inside(x, y, sel) else 0 for y in range(H) for x in range(W))


def texture(x, y, seed=0):
    rnd = random.Random(x * 7919 + y * 104729 + seed)
    v = 120 + 40 * ((x // 2 + y // 2) % 2) + rnd.randint(-15, 15)
    return v, v * 0.8, v * 0.6


ORIGINAL = make(lambda x, y: (200, 0, 0) if inside(x, y) else texture(x, y))
MASK = mask_bytes()


class SignedDistanceTest(unittest.TestCase):
    def test_inside_outside_signs(self):
        d = quality.signed_distance(W, H, MASK)
        self.assertEqual(d[0], 16)  # corner: 16 px from the selection (chessboard)
        self.assertEqual(d[20 * W + 15], 1)  # just left of the selection
        self.assertEqual(d[20 * W + 16], 0)  # first pixel inside
        self.assertLess(d[24 * W + 24], -5)  # centre

    def test_empty_mask_is_all_outside(self):
        d = quality.signed_distance(4, 4, bytes(16))
        self.assertTrue(all(v > 0 for v in d))


class RankTest(unittest.TestCase):
    def test_texture_continuation_beats_flat_smudge_for_removal(self):
        good = make(lambda x, y: texture(x, y, seed=1) if inside(x, y) else texture(x, y))
        smudge = make(lambda x, y: (140, 112, 84) if inside(x, y) else texture(x, y))
        self.assertEqual(quality.rank(ORIGINAL, [smudge, good], MASK, removal=True), [1, 0])
        s_good = quality.score(ORIGINAL, good, MASK, True)
        s_smudge = quality.score(ORIGINAL, smudge, MASK, True)
        self.assertLess(s_smudge.lap_ratio, 0.05)
        self.assertGreater(s_good.lap_ratio, 0.5)

    def test_pale_frame_loses(self):
        clean = make(lambda x, y: texture(x, y, seed=2) if inside(x, y) else texture(x, y))

        def framed(x, y):
            if inside(x, y) and not inside(x, y, (19, 19, 10, 10)):
                return 225, 225, 225  # pale band along the inside of the edge
            return texture(x, y, seed=2) if inside(x, y) else texture(x, y)

        frame = make(framed)
        self.assertEqual(quality.rank(ORIGINAL, [frame, clean], MASK, removal=False), [1, 0])
        self.assertGreater(quality.score(ORIGINAL, frame, MASK, False).frame, 0.2)

    def test_leak_outside_selection_loses(self):
        clean = make(lambda x, y: texture(x, y, seed=3) if inside(x, y) else texture(x, y))
        leaky = make(lambda x, y: texture(x, y, seed=3) if inside(x, y) else tuple(v * 0.6 for v in texture(x, y)))
        self.assertEqual(quality.rank(ORIGINAL, [leaky, clean], MASK, removal=False), [1, 0])
        self.assertGreater(quality.score(ORIGINAL, leaky, MASK, False).leak, 0.05)
        self.assertAlmostEqual(quality.score(ORIGINAL, clean, MASK, False).leak, 0.0)

    def test_hard_step_at_the_edge_loses(self):
        smooth = make(lambda x, y: (150, 120, 90))
        orig = make(lambda x, y: (150, 120, 90))

        def stepped(x, y):
            return (60, 50, 40) if inside(x, y) else (150, 120, 90)

        step = make(stepped)
        self.assertGreater(quality.score(orig, step, MASK, False).seam, 1.5)
        self.assertEqual(quality.rank(orig, [step, smooth], MASK, removal=False), [1, 0])

    def test_prompted_fill_may_differ_inside(self):
        """A prompted object differs from the ring; colour is not held against it."""
        obj = make(
            lambda x, y: (40, 160, 40)
            if inside(x, y, (19, 19, 10, 10))
            else (texture(x, y, 4) if inside(x, y) else texture(x, y))
        )
        s_prompt = quality.score(ORIGINAL, obj, MASK, removal=False)
        s_remove = quality.score(ORIGINAL, obj, MASK, removal=True)
        self.assertLess(s_prompt.total, s_remove.total)

    def test_prompted_fill_that_draws_nothing_ranks_last(self):
        # the selection just continues the texture
        empty = make(lambda x, y: texture(x, y, seed=5) if inside(x, y) else texture(x, y))
        drawn = make(
            lambda x, y: (220, 30, 40)
            if inside(x, y, (21, 21, 6, 6))
            else (texture(x, y, 5) if inside(x, y) else texture(x, y))
        )
        self.assertEqual(quality.rank(ORIGINAL, [empty, drawn], MASK, removal=False), [1, 0])
        # for removal the empty continuation is the better one
        self.assertEqual(quality.rank(ORIGINAL, [empty, drawn], MASK, removal=True), [0, 1])

    def test_single_candidate_and_stable_order(self):
        a = make(lambda x, y: texture(x, y))
        self.assertEqual(quality.rank(ORIGINAL, [a], MASK, True), [0])
        self.assertEqual(quality.rank(ORIGINAL, [a, a, a], MASK, True), [0, 1, 2])

    def test_size_mismatch_raises(self):
        small = quality.Buffer(4, 4, bytes(48))
        with self.assertRaises(ValueError):
            quality.score(ORIGINAL, small, MASK, True)


class GrainTest(unittest.TestCase):
    def test_measures_added_noise_and_ignores_texture(self):
        w = h = 120
        flat = bytes(v for _ in range(w * h) for v in (128, 110, 90))
        self.assertEqual(quality.noise_sigma(w, h, flat, 3), 0.0)
        noisy = quality.add_grain(flat, 3, 6.0, seed=1)
        self.assertAlmostEqual(quality.noise_sigma(w, h, noisy, 3), 6.0, delta=0.6)
        # hard stripes (texture, not grain) are not counted as noise
        stripes = bytes(v for y in range(h) for x in range(w) for v in ((240,) * 3 if (x // 3) % 2 else (20,) * 3))
        self.assertEqual(quality.noise_sigma(w, h, stripes, 3), 0.0)

    def test_mask_selects_ring_or_inside(self):
        w = h = 120
        mask = bytes(255 if 30 <= x < 90 and 30 <= y < 90 else 0 for y in range(h) for x in range(w))
        base = bytes(v for _ in range(w * h) for v in (128, 128, 128, 255))
        noisy = quality.add_grain(base, 4, 8.0, seed=2)
        mixed = bytearray(noisy)
        for y in range(30, 90):  # clean inside, grainy around
            o = (y * w + 30) * 4
            mixed[o : o + 60 * 4] = base[o : o + 60 * 4]
        ring = quality.noise_sigma(w, h, bytes(mixed), 4, mask, inside=False, step=1)
        inside = quality.noise_sigma(w, h, bytes(mixed), 4, mask, inside=True, step=1)
        self.assertAlmostEqual(ring, 8.0, delta=1.0)
        self.assertEqual(inside, 0.0)
        self.assertAlmostEqual(quality.grain_to_add(ring, inside), ring)

    def test_grain_to_add_in_quadrature_and_threshold(self):
        self.assertAlmostEqual(quality.grain_to_add(5.0, 3.0), 4.0)
        self.assertEqual(quality.grain_to_add(3.0, 5.0), 0.0)
        self.assertEqual(quality.grain_to_add(1.2, 1.0), 0.0)  # below one level: invisible

    def test_add_grain_keeps_alpha_and_is_monochrome(self):
        data = bytes((100, 150, 200, 77) * 500)
        out = quality.add_grain(data, 4, 5.0, seed=4)
        self.assertEqual(out[3::4], data[3::4])
        for i in range(0, len(out), 4):
            d = out[i] - 100
            self.assertEqual((out[i + 1] - 150, out[i + 2] - 200), (d, d))
        self.assertEqual(quality.add_grain(data, 4, 0.0), data)


if __name__ == "__main__":
    unittest.main()


def _sky(w, h, tint=(0, 0, 0), box=None, texture=0, seed=0):
    """RGBA gradient 'sky'; inside ``box`` (x0, y0, x1, y1) ``tint`` is added, plus optional noise."""
    rnd = random.Random(seed)
    out = bytearray()
    for y in range(h):
        for x in range(w):
            inside = box and box[0] <= x < box[2] and box[1] <= y < box[3]
            px = [90 + y // 4, 150 + y // 6, 220 - y // 8]
            for c in range(3):
                v = px[c] + (tint[c] if inside else 0) + (rnd.randint(-texture, texture) if inside and texture else 0)
                out.append(max(0, min(255, v)))
            out.append(255)
    return bytes(out)


def _rect_mask(w, h, box):
    return bytes(255 if box[0] <= x < box[2] and box[1] <= y < box[3] else 0 for y in range(h) for x in range(w))


class SeamCorrectionTest(unittest.TestCase):
    W, H = 160, 120
    SEL = (50, 40, 110, 80)  # the selection
    LAYER = (42, 32, 118, 88)  # the generated area the layer shows: the selection grown by 8

    def _px(self, data, x, y):
        o = (y * self.W + x) * 4
        return tuple(data[o : o + 3])

    def test_tinted_fill_is_brought_back_to_the_original(self):
        original = _sky(self.W, self.H)
        layer = _sky(self.W, self.H, tint=(6, -5, 8), box=self.LAYER)
        mask = _rect_mask(self.W, self.H, self.SEL)
        field = quality.seam_field(self.W, self.H, original, layer, mask, 4, band=8, side=self.W)
        self.assertIsNotNone(field)
        fixed = quality.apply_seam_field(layer, self.W, self.H, 4, field)
        for x, y in ((45, 60), (80, 35), (80, 60), (115, 85)):
            for a, b in zip(self._px(fixed, x, y), self._px(original, x, y), strict=True):
                self.assertLessEqual(abs(a - b), 2, (x, y))
        self.assertEqual(fixed[3::4], layer[3::4])  # alpha untouched

    def test_matching_fill_is_left_alone(self):
        original = _sky(self.W, self.H)
        mask = _rect_mask(self.W, self.H, self.SEL)
        self.assertIsNone(quality.seam_field(self.W, self.H, original, original, mask, 4, band=8, side=self.W))

    def test_content_and_texture_are_not_mistaken_for_drift(self):
        original = _sky(self.W, self.H)
        mask = _rect_mask(self.W, self.H, self.SEL)
        object_ = _sky(self.W, self.H, tint=(80, -60, -90), box=self.LAYER)  # a red object, not a tint
        self.assertIsNone(quality.seam_field(self.W, self.H, original, object_, mask, 4, band=8, side=self.W))
        grass = _sky(self.W, self.H, box=self.LAYER, texture=60, seed=3)
        self.assertIsNone(quality.seam_field(self.W, self.H, original, grass, mask, 4, band=8, side=self.W))

    def test_expand_strip_is_matched_to_the_old_image(self):
        # Old image: x < 120; the expand mask covers x >= 112 (an 8 px overlap); the strip came back tinted.
        original = _sky(self.W, self.H)
        strip = (112, 0, self.W, self.H)
        layer = _sky(self.W, self.H, tint=(-7, 4, -9), box=strip)
        mask = _rect_mask(self.W, self.H, strip)
        field = quality.seam_field(
            self.W, self.H, original, layer, mask, 4, band=0, side=self.W, original_rect=(0, 0, 120, self.H)
        )
        self.assertIsNotNone(field)
        fixed = quality.apply_seam_field(layer, self.W, self.H, 4, field)
        for x, y in ((115, 20), (140, 60), (155, 110)):
            for a, b in zip(self._px(fixed, x, y), self._px(original, x, y), strict=True):
                self.assertLessEqual(abs(a - b), 2, (x, y))
        for x, y in ((60, 60), (100, 10)):  # the old image is not touched
            self.assertEqual(self._px(fixed, x, y), self._px(layer, x, y))
