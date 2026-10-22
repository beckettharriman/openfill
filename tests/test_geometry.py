# SPDX-License-Identifier: Apache-2.0
import unittest

from openfill import geometry as g


class RectTests(unittest.TestCase):
    def test_intersect_and_contains(self):
        a, b = g.Rect(0, 0, 100, 100), g.Rect(50, 50, 100, 100)
        self.assertEqual(a.intersect(b), g.Rect(50, 50, 50, 50))
        self.assertTrue(a.contains(g.Rect(10, 10, 20, 20)))
        self.assertFalse(a.contains(b))
        self.assertTrue(g.Rect(0, 0, 0, 5).is_empty())

    def test_relative_to(self):
        self.assertEqual(g.Rect(30, 40, 5, 5).relative_to(g.Rect(10, 10, 100, 100)), g.Rect(20, 30, 5, 5))


class ContextRectTests(unittest.TestCase):
    image = g.Rect(0, 0, 2000, 1500)

    def test_small_selection_gets_tight_context_at_least_minimum(self):
        ctx = g.context_rect(g.Rect(900, 700, 40, 40), self.image, g.SD15)
        self.assertEqual(ctx.size, (192, 192))  # 3x selection = 120 < min_context 192
        self.assertTrue(ctx.contains(g.Rect(900, 700, 40, 40)))
        self.assertTrue(self.image.contains(ctx))

    def test_context_is_about_three_times_the_selection(self):
        sel = g.Rect(500, 500, 300, 200)
        ctx = g.context_rect(sel, self.image, g.SD15)
        self.assertEqual(ctx.size, (900, 800))  # pad = 300 on each side
        self.assertTrue(ctx.contains(sel))

    def test_clamped_to_image_edges(self):
        ctx = g.context_rect(g.Rect(0, 0, 30, 30), self.image, g.SDXL)
        self.assertEqual(ctx, g.Rect(0, 0, 256, 256))
        ctx = g.context_rect(g.Rect(1990, 1490, 10, 10), self.image, g.SD15)
        self.assertEqual((ctx.right, ctx.bottom), (2000, 1500))

    def test_image_smaller_than_minimum(self):
        small = g.Rect(0, 0, 150, 100)
        ctx = g.context_rect(g.Rect(10, 10, 50, 50), small, g.SD15)
        self.assertEqual(ctx, small)

    def test_empty_selection_rejected(self):
        with self.assertRaises(ValueError):
            g.context_rect(g.Rect(0, 0, 0, 0), self.image, g.SD15)


class WorkingSizeTests(unittest.TestCase):
    def test_identity_when_in_range(self):
        ws = g.working_size(640, 512, g.SD15)
        self.assertEqual((ws.width, ws.height, ws.scale), (640, 512, 1.0))

    def test_downscale_large_region(self):
        ws = g.working_size(2000, 1500, g.SD15)
        self.assertEqual(ws.width, 768)
        self.assertEqual(ws.height % 8, 0)
        self.assertLessEqual(ws.height, 768)

    def test_small_region_is_upscaled_to_min_side(self):
        ws = g.working_size(192, 192, g.SD15)
        self.assertEqual((ws.width, ws.height), (512, 512))
        self.assertAlmostEqual(ws.scale, 512 / 192, places=6)
        ws = g.working_size(256, 256, g.SDXL)
        self.assertEqual((ws.width, ws.height), (768, 768))

    def test_upscale_never_exceeds_max_side(self):
        ws = g.working_size(700, 300, g.SD15)
        self.assertLessEqual(max(ws.width, ws.height), 768)

    def test_sdxl_profile(self):
        ws = g.working_size(3000, 2000, g.SDXL)
        self.assertEqual(ws.width, 1024)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            g.working_size(0, 10, g.SD15)


class SmallSelectionPipelineTests(unittest.TestCase):
    """The bug report: a 38x33 px button in a 3024x4032 photo with SDXL."""

    def test_button_on_jacket(self):
        image = g.Rect(0, 0, 3024, 4032)
        sel = g.Rect(474, 1010, 38, 33)
        ctx = g.context_rect(sel, image, g.SDXL)
        self.assertEqual(ctx.size, (256, 256))  # tight crop, not 768
        self.assertTrue(ctx.contains(sel))
        ws = g.working_size(ctx.width, ctx.height, g.SDXL)
        self.assertEqual((ws.width, ws.height), (768, 768))  # upscaled 3x so the button is ~114 px
        mp = g.mask_params(sel, ws.scale)
        self.assertEqual(mp.grow_image, 6)  # 15 % of 38 -> 6 px (min 6)
        self.assertEqual(mp.feather_image, 2)  # 6 % of 38 -> 2 px, not 24
        self.assertEqual(mp.grow_work, 18)
        self.assertLessEqual(mp.blur_work, mp.grow_work)

    def test_fixed_feather_bug_is_gone(self):
        # Before the fix a fixed 24 px feather exceeded the selection itself.
        sel = g.Rect(0, 0, 38, 33)
        mp = g.mask_params(sel, 1.0)
        self.assertLess(mp.feather_image * 2, sel.height)

    def test_large_selection_caps(self):
        mp = g.mask_params(g.Rect(0, 0, 1200, 900), 0.5)
        self.assertEqual(mp.grow_image, 48)  # capped
        self.assertEqual(mp.feather_image, 24)  # capped
        self.assertEqual(mp.grow_work, 24)
        self.assertEqual(mp.blur_work, 12)

    def test_blur_never_exceeds_grow(self):
        for side in (10, 40, 100, 400, 2000):
            for scale in (0.25, 1.0, 4.0):
                mp = g.mask_params(g.Rect(0, 0, side, side), scale, feather_percent=30.0, grow_percent=5.0)
                self.assertLessEqual(mp.blur_work, mp.grow_work)
                self.assertGreaterEqual(mp.grow_work, 1)


class ExpandTests(unittest.TestCase):
    def test_single_side(self):
        plan = g.plan_expand(800, 600, 256, 0, 0, 0, g.SD15)
        self.assertEqual((plan.new_width, plan.new_height), (1056, 600))
        self.assertEqual((plan.offset_x, plan.offset_y), (256, 0))
        self.assertEqual(plan.grows, (True, False, False, False))
        self.assertEqual(plan.keep_rect, g.Rect(288, 0, 768, 600))
        self.assertEqual(plan.fill_rect, g.Rect(0, 0, 288, 600))
        self.assertTrue(plan.context.contains(plan.fill_rect))
        self.assertTrue(g.Rect(0, 0, 1056, 600).contains(plan.context))

    def test_strip_context_is_local_to_the_new_edge(self):
        # a 256 px strip along a 1440 px edge: not the whole 2176 px canvas
        plan = g.plan_expand(1920, 1440, 0, 0, 256, 0, g.SDXL)
        self.assertEqual(plan.context, g.Rect(1312, 0, 864, 1440))
        self.assertTrue(plan.context.contains(plan.fill_rect))
        ws = g.working_size(plan.context.width, plan.context.height, g.SDXL)
        self.assertGreater(ws.scale, 0.7)
        # top strip: full width, anchored at the top
        plan = g.plan_expand(1920, 1440, 0, 256, 0, 0, g.SDXL)
        self.assertEqual((plan.context.x, plan.context.y, plan.context.width), (0, 0, 1920))
        self.assertTrue(plan.context.contains(plan.fill_rect))

    def test_strip_context_not_narrower_than_half_the_edge(self):
        plan = g.plan_expand(2400, 3000, 0, 0, 256, 0, g.SDXL)
        self.assertEqual(plan.context.width, 1500)
        self.assertEqual(plan.context.right, plan.new_width)

    def test_all_sides_fill_is_canvas(self):
        plan = g.plan_expand(400, 400, 50, 50, 50, 50, g.SD15)
        self.assertEqual(plan.fill_rect, g.Rect(0, 0, 500, 500))
        self.assertEqual(plan.keep_rect, g.Rect(82, 82, 336, 336))

    def test_mask_marks_new_area_and_overlap(self):
        plan = g.plan_expand(800, 600, 256, 0, 0, 0, g.SD15)
        mask = g.mask_for_expand(plan)
        ctx = plan.context
        self.assertEqual(len(mask), ctx.area)
        # new border column -> 255; overlap band -> 255; untouched old pixels -> 0
        self.assertEqual(mask[0], 255)
        self.assertEqual(mask[(256 + 10) - ctx.x], 255)
        self.assertEqual(mask[(256 + 40) - ctx.x], 0)

    def test_rejects_bad_margins(self):
        with self.assertRaises(ValueError):
            g.plan_expand(100, 100, 0, 0, 0, 0, g.SD15)
        with self.assertRaises(ValueError):
            g.plan_expand(100, 100, -1, 0, 0, 0, g.SD15)


class ExpandRoundsTests(unittest.TestCase):
    def test_moderate_expansion_is_one_round_with_all_sides(self):
        self.assertEqual(g.expand_rounds(1920, 1440, 192, 192, 192, 192), [(192, 192, 192, 192)])
        self.assertEqual(g.expand_rounds(1280, 853, 0, 192, 0, 192), [(0, 192, 0, 192)])
        self.assertEqual(g.expand_rounds(2400, 3000, 0, 0, 256, 0), [(0, 0, 256, 0)])

    def test_large_expansion_runs_in_rounds_each_with_every_side(self):
        rounds = g.expand_rounds(1000, 800, 400, 400, 400, 400)
        self.assertGreater(len(rounds), 1)
        for step in rounds:
            self.assertTrue(all(m > 0 for m in step))
        # the rounds add up to the request
        self.assertEqual(tuple(sum(r[i] for r in rounds) for i in range(4)), (400, 400, 400, 400))

    def test_every_round_respects_the_fraction_of_the_current_canvas(self):
        width, height = 640, 480
        rounds = g.expand_rounds(width, height, 700, 50, 0, 900)
        for left, top, right, bottom in rounds:
            self.assertLessEqual(max(left, right), int(width * g.EXPAND_ROUND_FRACTION))
            self.assertLessEqual(max(top, bottom), int(height * g.EXPAND_ROUND_FRACTION))
            width, height = width + left + right, height + top + bottom
        self.assertEqual((width, height), (640 + 700, 480 + 50 + 900))

    def test_larger_parts_first_and_zero_margins_stay_zero(self):
        rounds = g.expand_rounds(400, 400, 0, 0, 301, 0)
        self.assertTrue(all(r[0] == r[1] == r[3] == 0 for r in rounds))
        rights = [r[2] for r in rounds]
        self.assertEqual(rights, sorted(rights, reverse=True))
        self.assertLessEqual(max(rights) - min(rights), 1)

    def test_round_limit(self):
        rounds = g.expand_rounds(10, 10, 10000, 0, 0, 0, max_rounds=3)
        self.assertEqual(len(rounds), 3)
        self.assertEqual(sum(r[0] for r in rounds), 10000)

    def test_rejects_bad_margins(self):
        with self.assertRaises(ValueError):
            g.expand_rounds(100, 100, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            g.expand_rounds(100, 100, -5, 10, 0, 0)


class FillProfileTests(unittest.TestCase):
    def test_large_prompted_sd15_fill_is_capped(self):
        ctx = g.Rect(849, 399, 1524, 1282)  # img-3000
        prof = g.fill_profile(g.SD15, ctx, "sd15", removal=False)
        self.assertEqual(prof.max_side, g.SD15_PROMPT_MAX_SIDE)
        ws = g.working_size(ctx.width, ctx.height, prof)
        self.assertEqual((ws.width, ws.height), (640, 536))

    def test_small_context_removal_and_other_archs_unchanged(self):
        small = g.Rect(0, 0, 409, 396)
        self.assertIs(g.fill_profile(g.SD15, small, "sd15", removal=False), g.SD15)
        big = g.Rect(0, 0, 1524, 1282)
        self.assertIs(g.fill_profile(g.SD15, big, "sd15", removal=True), g.SD15)
        self.assertIs(g.fill_profile(g.SDXL, big, "sdxl", removal=False), g.SDXL)

    def test_lower_user_max_side_is_kept(self):
        prof = g.fill_profile(g.ModelProfile("sd15", 512, 576, 192), g.Rect(0, 0, 2000, 1500), "sd15", False)
        self.assertEqual(prof.max_side, 576)


class MiscTests(unittest.TestCase):
    def test_round_to_multiple(self):
        self.assertEqual(g.round_to_multiple(515, 8), 512)
        self.assertEqual(g.round_to_multiple(3, 8), 8)

    def test_seed(self):
        self.assertEqual(g.seed_or_random(7), 7)
        import random

        self.assertEqual(g.seed_or_random(-1, random.Random(1)), random.Random(1).randrange(0, 2**32 - 1))

    def test_timeout_has_floor(self):
        self.assertGreaterEqual(g.estimate_timeout(1, 1, 1000), 120)
        self.assertGreaterEqual(g.estimate_timeout(28, 1, 1024 * 1024, floor=240.0), 240)
        self.assertGreater(g.estimate_timeout(50, 4, 1024 * 1024), g.estimate_timeout(10, 1, 1024 * 1024))

    def test_timeout_covers_a_cold_model_load(self):
        warm = g.estimate_timeout(25, 1, 1024 * 1024)
        # a 7 GB model adds its load time from disk
        self.assertGreater(g.estimate_timeout(25, 1, 1024 * 1024, model_bytes=7_000_000_000), warm + 100)
        # the first request with a model gets at least ten minutes
        self.assertGreaterEqual(g.estimate_timeout(25, 1, 1024 * 1024, cold=True), 600)
        self.assertGreaterEqual(g.estimate_timeout(1, 1, 1000, cold=True), g.COLD_TIMEOUT_FLOOR)
        # FLUX Q4 cold case: 153 s was not enough
        self.assertGreaterEqual(g.estimate_timeout(28, 1, 1024 * 1024, model_bytes=12_000_000_000, cold=True), 600)


class FillPolicyTests(unittest.TestCase):
    image = g.Rect(0, 0, 1024, 768)

    def test_prompt_fill_uses_tighter_context_than_removal(self):
        sel = g.Rect(410, 0, 205, 192)
        tight = g.context_rect(sel, self.image, g.SD15, padding_ratio=g.fill_padding(False))
        wide = g.context_rect(sel, self.image, g.SD15, padding_ratio=g.fill_padding(True))
        self.assertLess(tight.area, wide.area)
        self.assertTrue(tight.contains(sel))

    def test_color_match_skipped_without_reference(self):
        whole = self.image
        self.assertFalse(g.color_match_useful(whole, whole, self.image))
        # touches left, top and bottom edges
        tall_left = g.Rect(0, 0, 200, 768)
        self.assertFalse(g.color_match_useful(tall_left, g.Rect(0, 0, 600, 768), self.image))
        # ordinary selection in the middle keeps colour matching
        mid = g.Rect(420, 300, 260, 200)
        self.assertTrue(g.color_match_useful(mid, g.context_rect(mid, self.image, g.SD15), self.image))
        # a corner selection touches only two edges
        corner = g.Rect(0, 0, 100, 100)
        self.assertTrue(g.color_match_useful(corner, g.Rect(0, 0, 300, 300), self.image))

    def test_removal_model_context_stays_near_native_size(self):
        # MAT runs on a 512 px square: a 400 px selection must not be shrunk 2.4x
        image = g.Rect(0, 0, 2400, 3000)
        sel = g.Rect(852, 1665, 312, 405)
        ctx = g.context_rect(sel, image, g.INPAINT_MODEL, padding_ratio=g.fill_padding(True, True))
        wide = g.context_rect(sel, image, g.INPAINT_MODEL, padding_ratio=g.fill_padding(True))
        self.assertLess(ctx.area, wide.area)
        self.assertTrue(ctx.contains(sel))
        ws = g.working_size(ctx.width, ctx.height, g.INPAINT_MODEL)
        self.assertLessEqual(max(ws.width, ws.height), 512)
        # a small selection gets a 512 px context at 1:1
        small = g.context_rect(g.Rect(100, 100, 80, 60), image, g.INPAINT_MODEL, g.fill_padding(True, True))
        self.assertEqual(small.size, (512, 512))
        self.assertEqual(g.working_size(*small.size, g.INPAINT_MODEL).scale, 1.0)

    def test_prefill_policy(self):
        self.assertEqual(g.prefill_for(True, 1.0, "sd15"), ("border", "navier-stokes"))
        self.assertEqual(g.prefill_for(True, 1.0, "sdxl"), ("border", "navier-stokes"))
        self.assertEqual(g.prefill_for(False, 0.6, "sd15"), ("blur", "navier-stokes"))
        self.assertEqual(g.prefill_for(False, 1.0, "sd15"), ("border", "navier-stokes"))
        # the SDXL tier paints a framed picture into a flat grey hole
        self.assertEqual(g.prefill_for(False, 1.0, "sdxl"), ("border", "navier-stokes"))

    def test_color_match_strength(self):
        mid = g.Rect(420, 300, 260, 200)
        ctx = g.context_rect(mid, self.image, g.SD15)
        self.assertEqual(g.color_match_strength(mid, ctx, self.image, True), 1.0)
        self.assertEqual(g.color_match_strength(mid, ctx, self.image, False), g.PROMPT_COLOR_MATCH)
        self.assertEqual(g.color_match_strength(self.image, self.image, self.image, True), 0.0)

    def test_layer_mask_grow(self):
        mp = g.mask_params(g.Rect(0, 0, 260, 200), 1.0)
        self.assertEqual(g.layer_mask_grow(mp, True), mp.grow_image)
        self.assertEqual(g.layer_mask_grow(mp, False), min(mp.grow_image, mp.feather_image))
        self.assertLessEqual(g.layer_mask_grow(mp, False), mp.grow_image)
        tiny = g.mask_params(g.Rect(0, 0, 10, 10), 1.0)
        self.assertGreaterEqual(g.layer_mask_grow(tiny, False), 1)

    def test_flatten_over_mean(self):
        opaque = bytes([10, 20, 30, 255]) * 4
        self.assertIs(g.flatten_over_mean(opaque), opaque)
        # two opaque red pixels, two fully transparent black ones -> transparent become red
        px = bytes([200, 0, 0, 255, 200, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0])
        out = g.flatten_over_mean(px)
        self.assertEqual(out, bytes([200, 0, 0, 255]) * 4)
        # half transparent pixel blends with the mean
        px = bytes([100, 100, 100, 255, 0, 0, 0, 128])
        out = g.flatten_over_mean(px)
        self.assertEqual(out[3::4], bytes([255, 255]))
        self.assertTrue(25 <= out[4] <= 40, out)  # mean is alpha-weighted (67), half of it shows
        # nothing visible -> mid grey
        self.assertEqual(g.flatten_over_mean(bytes(8)), bytes([128, 128, 128, 255]) * 2)


class ExpandCropTests(unittest.TestCase):
    def test_split_crop_grow(self):
        self.assertEqual(g.split_crop_grow((-10, 20, 0, -5)), ((10, 0, 0, 5), (0, 20, 0, 0)))

    def test_fit_expand_margins_limits_and_keeps_the_photo(self):
        fit = g.fit_expand_margins
        self.assertEqual(fit((256, 0, 256, 0), 1000, 800), (256, 0, 256, 0))
        self.assertEqual(fit((9999, -9999, 0, 0), 1000, 800), (4096, -736, 0, 0))
        self.assertEqual(fit((-600, 0, -600, 0), 1000, 800), (-336, 0, -600, 0))
        self.assertEqual(fit((-600, 0, -600, 0), 1000, 800, previous=(-600, 0, 0, 0)), (-600, 0, -336, 0))
        self.assertEqual(fit((-30, 0, 0, 0), 40, 40), (0, 0, 0, 0))  # a photo under 64 px is kept whole

    def test_batch_margins_are_clamped_not_rejected(self):
        # procedure arguments run through fit_expand_margins: huge crops give way, nothing raises
        out = g.fit_expand_margins((-4096, -4096, -4096, -4096), 1920, 1080)
        self.assertGreaterEqual(1920 + out[0] + out[2], 64)
        self.assertGreaterEqual(1080 + out[1] + out[3], 64)

    def test_round_canvases_follow_each_round(self):
        rounds = g.expand_rounds(1000, 800, 500, 0, 500, 0)
        canvases = g.round_canvases(1000, 800, rounds)
        self.assertEqual(canvases[-1], (2000, 800))
        self.assertEqual([c[0] for c in canvases], sorted(c[0] for c in canvases))

    def test_rounds_use_the_cropped_photo(self):
        # 2000 px photo cropped to 1000, then 400 px added: 40 % of the kept width needs two rounds
        self.assertEqual(len(g.expand_rounds(1000, 800, 400, 0, 0, 0)), 2)
        self.assertEqual(len(g.expand_rounds(2000, 800, 400, 0, 0, 0)), 1)

    def test_clip_rect(self):
        self.assertEqual(g.clip_rect(0, 0, 100, 100, 100, 100), g.Rect(0, 0, 100, 100))
        self.assertEqual(g.clip_rect(-20, -10, 100, 100, 80, 60), g.Rect(0, 0, 80, 60))
        self.assertEqual(g.clip_rect(30, 40, 100, 100, 80, 60), g.Rect(30, 40, 50, 20))
        self.assertIsNone(g.clip_rect(80, 0, 10, 10, 80, 60))
        self.assertIsNone(g.clip_rect(-50, 0, 50, 10, 80, 60))


if __name__ == "__main__":
    unittest.main()


class FillPlanTests(unittest.TestCase):
    """Minimum working scale, thin strips and the refine pass (geometry.plan_fill)."""

    def _covered(self, tiles, sel):
        """Every selection pixel lies in some tile (checked on a grid)."""
        for y in range(sel.y, sel.bottom, max(1, sel.height // 7)):
            for x in range(sel.x, sel.right, max(1, sel.width // 7)):
                self.assertTrue(
                    any(t.rect.x <= x < t.rect.right and t.rect.y <= y < t.rect.bottom for t in tiles), (x, y)
                )

    def test_small_selection_is_one_pass_without_refine(self):
        plan = g.plan_fill(g.Rect(922, 384, 256, 256), g.Rect(0, 0, 1280, 853), g.SD15, "sd15", False, 0.5, refine=True)
        self.assertEqual(plan.tiles, ())
        self.assertEqual(plan.work.scale, 1.0)
        self.assertEqual(plan.refine_tiles, ())  # worked at 1:1: nothing to refine

    def test_scale_never_below_minimum(self):
        for arch, prof in (("sd15", g.SD15), ("sdxl", g.SDXL)):
            for sel, img in (
                (g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273)),
                (g.Rect(96, 72, 1728, 1296), g.Rect(0, 0, 1920, 1440)),
                (g.Rect(1488, 1200, 792, 1350), g.Rect(0, 0, 2400, 3000)),
                (g.Rect(730, 1267, 557, 490), g.Rect(0, 0, 1920, 2880)),
            ):
                plan = g.plan_fill(sel, img, prof, arch, False, 0.5)
                self.assertGreaterEqual(plan.main_scale, g.MIN_WORK_SCALE[arch] - 0.02, (arch, sel))
                self.assertTrue(plan.context.contains(sel))
                if plan.tiles:
                    self._covered([t for t in plan.tiles], sel.relative_to(plan.context))
                    for t in plan.tiles:
                        self.assertLessEqual(max(t.work_width, t.work_height), prof.max_side)
                        self.assertEqual(t.work_width % prof.multiple, 0)

    def test_thin_strip_gets_tiles_with_content_across(self):
        sel = g.Rect(160, 51, 1620, 89)
        plan = g.plan_fill(sel, g.Rect(0, 0, 2000, 1273), g.SD15, "sd15", False, 0.5)
        self.assertGreater(len(plan.tiles), 1)
        for t in plan.tiles:
            self.assertGreaterEqual(min(t.work_width, t.work_height), g.MIN_TILE_CONTENT)
            # the strip is worked with more than 28 px across (the old single pass)
            self.assertGreaterEqual(89 * plan.tile_scale, 80)

    def test_later_tiles_mark_earlier_overlap_done(self):
        sel = g.Rect(160, 51, 1620, 89)
        plan = g.plan_fill(sel, g.Rect(0, 0, 2000, 1273), g.SD15, "sd15", False, 0.5)
        self.assertEqual(plan.tiles[0].done, ())
        for prev, tile in zip(plan.tiles, plan.tiles[1:], strict=False):
            self.assertTrue(tile.done)
            overlap = prev.rect.right - tile.rect.x
            done_width = max(d.right for d in tile.done)
            # half the overlap stays to be repainted as a blend band
            self.assertLess(done_width, overlap)
            self.assertGreater(done_width, 0)

    def test_margin_shrinks_before_tiling(self):
        # 900 px selection: a 0.5 margin context needs scale 0.4; a smaller margin keeps 0.6+ in one pass
        sel = g.Rect(500, 500, 900, 700)
        plan = g.plan_fill(sel, g.Rect(0, 0, 3000, 3000), g.SD15, "sd15", False, 0.5)
        self.assertEqual(plan.tiles, ())
        self.assertGreaterEqual(plan.work.scale, 0.6)
        self.assertTrue(plan.context.contains(sel))

    def test_refine_covers_selection_at_full_resolution(self):
        sel = g.Rect(730, 1267, 557, 490)
        plan = g.plan_fill(sel, g.Rect(0, 0, 1920, 2880), g.SD15, "sd15", False, 0.5, refine=True)
        self.assertLess(plan.main_scale, 1.0)
        self.assertTrue(plan.refine_tiles)
        self._covered(plan.refine_tiles, sel.relative_to(plan.context))
        for t in plan.refine_tiles:
            self.assertEqual((t.work_width, t.work_height), (t.rect.width, t.rect.height))
            self.assertLessEqual(max(t.rect.width, t.rect.height), g.SD15.max_side)
            self.assertEqual(t.rect.width % 8, 0)
        self.assertEqual(len(plan.refine_tiles), 1)

    def test_layout_pass_only_when_tiled(self):
        small = g.plan_fill(
            g.Rect(922, 384, 256, 256), g.Rect(0, 0, 1280, 853), g.SD15, "sd15", False, 0.5, layout=True
        )
        self.assertFalse(small.layout)
        suede = g.plan_fill(
            g.Rect(96, 289, 1690, 866), g.Rect(0, 0, 1920, 1443), g.SD15, "sd15", False, 0.5, layout=True
        )
        self.assertTrue(suede.tiles)
        self.assertTrue(suede.layout)
        off = g.plan_fill(g.Rect(96, 289, 1690, 866), g.Rect(0, 0, 1920, 1443), g.SD15, "sd15", False, 0.5)
        self.assertFalse(off.layout)
        self.assertEqual(off.tiles, suede.tiles)  # the tiles themselves do not change

    def test_layout_pass_is_one_pass_over_the_whole_context(self):
        for arch, prof, sel, img in (
            ("sd15", g.SD15, g.Rect(96, 289, 1690, 866), g.Rect(0, 0, 1920, 1443)),
            ("sdxl", g.SDXL, g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273)),
        ):
            plan = g.plan_fill(sel, img, prof, arch, False, 0.5, layout=True)
            self.assertTrue(plan.layout)
            single = g.plan_fill(sel, img, prof, arch, False, 0.5).work
            w = plan.work
            self.assertGreaterEqual(w.scale, min(single.scale, 1.0))
            self.assertGreaterEqual(w.scale, g.LAYOUT_MIN_SCALE - 1e-9, arch)
            self.assertLessEqual(max(w.width, w.height), g.LAYOUT_MAX_SIDE_FACTOR * prof.max_side + prof.multiple)
            self.assertAlmostEqual(w.width / plan.context.width, w.scale, delta=0.02)
            self.assertEqual(w.width % prof.multiple, 0)

    def test_layout_decision(self):
        def plan(sel, img, arch):
            return g.plan_fill(sel, img, g.PROFILES[arch], arch, False, 0.5, layout=True)

        roof, image = g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273)
        self.assertTrue(plan(roof, image, "sdxl").layout)  # SDXL strip: tiles mostly wall
        self.assertFalse(plan(roof, image, "sd15").layout)  # SD 1.5 strip tiles already work
        self.assertTrue(plan(roof, image, "sd15").tiles)
        brick, image = g.Rect(96, 72, 1728, 1296), g.Rect(0, 0, 1920, 1440)
        self.assertTrue(plan(brick, image, "sd15").layout)  # SD 1.5: always when tiled
        self.assertFalse(plan(brick, image, "sdxl").layout)  # large SDXL area: tiles only
        self.assertTrue(plan(brick, image, "sdxl").tiles)
        leather = plan(g.Rect(1488, 1200, 792, 1350), g.Rect(0, 0, 2400, 3000), "sdxl")
        self.assertTrue(leather.tiles)
        self.assertFalse(leather.layout)
        self.assertFalse(g.wants_layout("sd15", brick, (), 1.0))  # never without tiles

    def test_layout_scale_capped_on_huge_contexts(self):
        ctx = g.Rect(0, 0, 6000, 4000)
        single = g.working_size(ctx.width, ctx.height, g.SD15)
        w = g.layout_work(ctx, g.SD15, single)
        self.assertLess(w.scale, g.LAYOUT_MIN_SCALE)
        self.assertLessEqual(max(w.width, w.height), g.LAYOUT_MAX_SIDE_FACTOR * g.SD15.max_side + 8)
        self.assertGreaterEqual(w.scale, single.scale)

    def test_strip_tiles_centred_on_the_selection_across(self):
        # away from the image edge the window is centred on the strip across it
        sel = g.Rect(160, 600, 1620, 89)
        plan = g.plan_fill(sel, g.Rect(0, 0, 2000, 1400), g.SDXL, "sdxl", False, 0.5)
        self.assertTrue(plan.tiles)
        local = sel.relative_to(plan.context)
        for t in plan.tiles:
            above, below = local.y - t.rect.y, t.rect.bottom - local.bottom
            self.assertLessEqual(abs(above - below), 2)
        # at the edge it is moved inside the image, not cut
        edge = g.plan_fill(g.Rect(160, 51, 1620, 89), g.Rect(0, 0, 2000, 1273), g.SDXL, "sdxl", False, 0.5)
        for t in edge.tiles:
            self.assertEqual(t.rect.y, 0)
            self.assertGreaterEqual(t.work_height, 0.75 * g.SDXL.min_side)

    def test_other_architectures_unchanged(self):
        sel = g.Rect(96, 72, 1728, 1296)
        plan = g.plan_fill(sel, g.Rect(0, 0, 1920, 1440), g.INPAINT_MODEL, "inpaint_model", True, 0.5)
        self.assertEqual(plan.tiles, ())
        ctx = g.context_rect(sel, g.Rect(0, 0, 1920, 1440), g.INPAINT_MODEL, 0.5)
        self.assertEqual(plan.context, ctx)


class RemovalPlanTests(unittest.TestCase):
    """Tiled LaMa/MAT removal (geometry.plan_removal)."""

    image = g.Rect(0, 0, 1920, 1268)

    def test_small_hole_is_not_tiled(self):
        self.assertIsNone(g.plan_removal(g.Rect(893, 512, 163, 256), g.Rect(0, 0, 1920, 1280)))
        self.assertIsNone(g.plan_removal(g.Rect(154, 324, 154, 188), g.Rect(0, 0, 1280, 853)))

    def test_deep_hole_is_not_tiled(self):
        # windows inside a hole this deep would be nearly all hole
        self.assertIsNone(g.plan_removal(g.Rect(115, 119, 794, 614), g.Rect(0, 0, 1280, 853)))
        self.assertIsNone(g.plan_removal(g.Rect(0, 900, 720, 1140), g.Rect(0, 0, 2400, 3000)))

    def test_disabled(self):
        self.assertIsNone(g.plan_removal(g.Rect(192, 913, 806, 355), self.image, tile=0))

    def test_band_is_tiled_at_one_to_one(self):
        sel = g.Rect(192, 913, 806, 355)
        plan = g.plan_removal(sel, self.image)
        self.assertIsNotNone(plan)
        self.assertGreater(len(plan.tiles), 1)
        self.assertTrue(self.image.contains(plan.context))
        self.assertTrue(plan.context.contains(sel))
        self.assertEqual(plan.work.scale, 1.0)
        local = sel.relative_to(plan.context)
        for t in plan.tiles:
            self.assertEqual((t.work_width, t.work_height), (t.rect.width, t.rect.height))
            self.assertLessEqual(max(t.rect.width, t.rect.height), g.REMOVAL_TILE)
        for y in range(local.y, local.bottom, 20):
            for x in range(local.x, local.right, 20):
                self.assertTrue(any(t.rect.x <= x < t.rect.right and t.rect.y <= y < t.rect.bottom for t in plan.tiles))
        self.assertEqual(plan.tiles[0].done, ())
        for prev, tile in zip(plan.tiles, plan.tiles[1:], strict=False):
            self.assertGreaterEqual(prev.rect.right - tile.rect.x, g.REMOVAL_TILE_OVERLAP)
            self.assertTrue(tile.done)

    def test_band_windows_centred_across(self):
        sel = g.Rect(300, 600, 1200, 154)
        plan = g.plan_removal(sel, g.Rect(0, 0, 1920, 1400))
        local = sel.relative_to(plan.context)
        for t in plan.tiles:
            self.assertLessEqual(abs((local.y - t.rect.y) - (t.rect.bottom - local.bottom)), 2)
            self.assertGreaterEqual(t.rect.height, g.REMOVAL_TILE - 1)
            self.assertLessEqual(t.rect.height, g.REMOVAL_TILE)
