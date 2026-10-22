# SPDX-License-Identifier: Apache-2.0
"""The pure geometry of the Generative Expand editor (no GTK needed)."""

import unittest

from openfill import ui_expand as ue
from openfill.ui_expand import Margins


class MarginTests(unittest.TestCase):
    def test_from_dict_clamps_and_tolerates_junk(self):
        m = Margins.from_dict({"left": 5000, "top": -3, "right": "12", "bottom": None})
        self.assertEqual(m, Margins(4096, -3, 12, 0))
        self.assertEqual(Margins.from_dict({"left": "x"}), Margins())
        self.assertEqual(Margins(1, 2, 3, 4).as_dict(), {"left": 1, "top": 2, "right": 3, "bottom": 4})

    def test_canvas_and_empty(self):
        self.assertEqual(Margins(256, 0, 256, 0).canvas(1024, 768), (1536, 768))
        self.assertTrue(Margins().is_empty())
        self.assertFalse(Margins(bottom=1).is_empty())

    def test_clamp_margin(self):
        self.assertEqual(ue.clamp_margin(-1), -1)
        self.assertEqual(ue.clamp_margin(-99999), -ue.MAX_MARGIN)
        self.assertEqual(ue.clamp_margin(10.6), 11)
        self.assertEqual(ue.clamp_margin(99999), ue.MAX_MARGIN)
        self.assertEqual(ue.clamp_margin(float("nan")), 0)
        self.assertEqual(ue.clamp_margin(300, limit=256), 256)


class PresetTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(ue.split(100, 0), (0, 100))
        self.assertEqual(ue.split(101, 1), (50, 51))
        self.assertEqual(ue.split(100, 2), (100, 0))
        self.assertEqual(ue.split(-5, 1), (-3, -2))
        self.assertEqual(ue.split(-5, 0), (0, -5))
        self.assertEqual(ue.split(-5, 2), (-5, 0))

    def test_wide_preset_on_landscape_adds_width(self):
        # 1024 x 768 (4:3) to 16:9: the height stays, the width becomes 1366
        m = ue.preset_margins(1024, 768, "16:9", "center")
        self.assertEqual(m.canvas(1024, 768), (1366, 768))
        self.assertEqual((m.left, m.right, m.top, m.bottom), (171, 171, 0, 0))

    def test_tall_preset_adds_height_with_anchor(self):
        m = ue.preset_margins(1024, 768, "9:16", "top")
        w, h = m.canvas(1024, 768)
        self.assertEqual(w, 1024)
        self.assertAlmostEqual(w / h, 9 / 16, places=2)
        self.assertEqual((m.top, m.left, m.right), (0, 0, 0))
        self.assertEqual(m.bottom, h - 768)

    def test_every_ratio_contains_the_image_and_matches(self):
        for pid, _label, ratio in ue.PRESETS:
            if ratio is None:
                continue
            for size in ((1024, 768), (768, 1024), (512, 512), (1920, 1080), (300, 2000)):
                m = ue.preset_margins(*size, pid)
                w, h = m.canvas(*size)
                self.assertGreaterEqual(w, size[0])
                self.assertGreaterEqual(h, size[1])
                self.assertFalse(m.is_empty(), (pid, size))
                self.assertLess(abs(w / h - ratio[0] / ratio[1]), 0.01, (pid, size, w, h))

    def test_original_and_equal_ratio_grow(self):
        m = ue.preset_margins(1000, 500, "original")
        self.assertEqual(m.canvas(1000, 500), (1250, 625))
        self.assertEqual(m, Margins(125, 62, 125, 63))
        square = ue.preset_margins(512, 512, "1:1", "bottom-right")
        self.assertEqual(square, Margins(128, 128, 0, 0))

    def test_custom_and_unknown(self):
        self.assertIsNone(ue.preset_margins(100, 100, "custom"))
        self.assertIsNone(ue.preset_margins(100, 100, "nope"))

    def test_limit_is_respected(self):
        m = ue.preset_margins(4096, 512, "9:16", "top")
        self.assertEqual(m.bottom, ue.MAX_MARGIN)

    def test_reanchor_keeps_totals(self):
        m = Margins(10, 20, 90, 0)
        self.assertEqual(ue.reanchor(m, "top-left"), Margins(0, 0, 100, 20))
        self.assertEqual(ue.reanchor(m, "center"), Margins(50, 10, 50, 10))
        self.assertEqual(ue.reanchor(m, "bottom-right"), Margins(100, 20, 0, 0))

    def test_anchor_of(self):
        self.assertEqual(ue.anchor_of(Margins(0, 0, 256, 0)), "left")
        self.assertEqual(ue.anchor_of(Margins(256, 0, 0, 256)), "top-right")
        self.assertEqual(ue.anchor_of(Margins(5, 5, 7, 7)), "center")
        self.assertEqual(ue.anchor_of(Margins(), "bottom-left"), "bottom-left")
        self.assertEqual(ue.anchor_of(Margins(0, 0, 10, 0), "bottom-right"), "bottom-left")

    def test_matching_preset(self):
        m = ue.preset_margins(1024, 768, "16:9", "left")
        self.assertEqual(ue.matching_preset(1024, 768, m, "left"), "16:9")
        self.assertEqual(ue.matching_preset(1024, 768, Margins(1, 0, 0, 0), "center"), "custom")
        # the position of the frame does not matter, only its size
        self.assertEqual(ue.matching_preset(1024, 768, Margins(-100, 0, 100 + 342, 0)), "16:9")


class LayoutTests(unittest.TestCase):
    def test_image_fits_thumb_and_canvas_is_centred(self):
        v = ue.fit_view(600, 380, 1024, 768, Margins())
        self.assertAlmostEqual(v.iw, 360)
        self.assertAlmostEqual(v.ih, 270)
        self.assertAlmostEqual(v.fx + v.fw / 2, 300)
        self.assertAlmostEqual(v.fy + v.fh / 2, 190)
        self.assertEqual((v.fx, v.fy, v.fw, v.fh), (v.ix, v.iy, v.iw, v.ih))

    def test_large_margins_shrink_the_view(self):
        v = ue.fit_view(600, 380, 1024, 768, Margins(2048, 0, 2048, 0))
        self.assertLessEqual(v.fw, 600 - 2 * ue.PAD + 1e-6)
        self.assertGreaterEqual(v.fx, ue.PAD - 1e-6)
        self.assertAlmostEqual(v.ix - v.fx, 2048 * v.scale)

    def test_view_at_keeps_image_still(self):
        v = ue.view_at(0.5, 100, 50, 400, 200, Margins(40, 0, 0, 20))
        self.assertEqual((v.ix, v.iy, v.iw, v.ih), (100, 50, 200, 100))
        self.assertEqual((v.fx, v.fy, v.fw, v.fh), (80, 50, 220, 110))


class HitTests(unittest.TestCase):
    def setUp(self):
        # frame from (100, 50) to (400, 250)
        self.v = ue.view_at(1.0, 150, 50, 250, 200, Margins(50, 0, 0, 0))

    def test_corners_and_edges(self):
        self.assertEqual(ue.hit_test(self.v, 101, 52), "top-left")
        self.assertEqual(ue.hit_test(self.v, 398, 249), "bottom-right")
        self.assertEqual(ue.hit_test(self.v, 405, 150), "right")
        self.assertEqual(ue.hit_test(self.v, 104, 120), "left")
        self.assertEqual(ue.hit_test(self.v, 250, 46), "top")
        self.assertEqual(ue.hit_test(self.v, 250, 255), "bottom")

    def test_misses(self):
        self.assertIsNone(ue.hit_test(self.v, 250, 150))  # inside
        self.assertIsNone(ue.hit_test(self.v, 20, 150))  # far left
        self.assertIsNone(ue.hit_test(self.v, 400, 300))  # below the right edge

    def test_zero_size_frame_still_has_handles(self):
        v = ue.view_at(1.0, 100, 100, 50, 50, Margins())
        self.assertEqual(ue.hit_test(v, 151, 125), "right")
        self.assertEqual(set(ue.handle_points(v)), set(ue.HANDLES))
        self.assertEqual(set(ue.CURSORS), set(ue.HANDLES))


class DragTests(unittest.TestCase):
    def test_outward_adds_and_snaps(self):
        start = Margins(0, 0, 0, 0)
        self.assertEqual(ue.drag_margins(start, "right", 50, 0, 0.5), Margins(0, 0, 104, 0))
        self.assertEqual(ue.drag_margins(start, "left", -50, 7, 0.5), Margins(104, 0, 0, 0))
        self.assertEqual(ue.drag_margins(start, "top", 0, -10, 0.25), Margins(0, 40, 0, 0))
        self.assertEqual(ue.drag_margins(start, "bottom-right", 32, 16, 0.5), Margins(0, 0, 64, 32))
        self.assertEqual(ue.drag_margins(start, "top-left", -32, -16, 0.5), Margins(64, 32, 0, 0))

    def test_inward_and_limits(self):
        start = Margins(100, 0, 100, 0)
        self.assertEqual(ue.drag_margins(start, "right", -500, 0, 1.0), Margins(100, 0, -400, 0))
        self.assertEqual(ue.drag_margins(start, "left", -99999, 0, 1.0).left, ue.MAX_MARGIN)
        self.assertEqual(ue.drag_margins(start, "left", -500, 0, 1.0, limit=256).left, 256)

    def test_other_sides_keep_exact_values(self):
        start = Margins(13, 7, 0, 0)
        self.assertEqual(ue.drag_margins(start, "right", 40, 0, 1.0), Margins(13, 7, 40, 0))
        self.assertEqual(ue.drag_margins(start, "nonsense", 40, 0, 1.0), start)
        self.assertEqual(ue.drag_margins(start, "right", 40, 0, 0), start)


class MagnetTests(unittest.TestCase):
    def test_edge_snaps_to_the_opposite_margin(self):
        # 47 screen px at scale 0.25 = 188 image px, within 12 screen px of 200
        self.assertEqual(ue.drag_margins(Margins(0, 0, 0, 200), "top", 0, -47, 0.25).top, 200)

    def test_no_magnet_when_the_opposite_side_is_zero(self):
        self.assertEqual(ue.drag_margins(Margins(), "top", 0, -10, 0.25).top, 40)


class StateTests(unittest.TestCase):
    def test_initial_values(self):
        s = ue.ExpandState(1024, 768, Margins(256, 0, 256, 0))
        self.assertEqual(s.anchor, "center")
        self.assertEqual(s.preset, "custom")
        self.assertEqual(s.canvas, (1536, 768))

    def test_preset_then_anchor_then_drag(self):
        calls = []
        s = ue.ExpandState(1024, 768, Margins(), listener=lambda: calls.append(1))
        s.apply_preset("16:9")
        self.assertEqual(s.preset, "16:9")
        self.assertEqual(s.canvas, (1366, 768))
        s.set_anchor("left")
        self.assertEqual(s.margins, Margins(0, 0, 342, 0))
        self.assertEqual(s.preset, "16:9")
        s.set_side("top", 64)
        self.assertEqual(s.preset, "custom")
        self.assertEqual(s.anchor, "bottom-left")
        self.assertEqual(len(calls), 3)

    def test_custom_keeps_margins_and_unknowns_are_ignored(self):
        s = ue.ExpandState(800, 600, Margins(10, 0, 10, 0))
        s.apply_preset("custom")
        self.assertEqual(s.margins, Margins(10, 0, 10, 0))
        s.apply_preset("bogus")
        s.set_anchor("bogus")
        s.set_side("middle", 5)
        self.assertEqual(s.margins, Margins(10, 0, 10, 0))

    def test_setting_the_preset_margins_by_hand_selects_it(self):
        s = ue.ExpandState(1024, 768, Margins())
        s.set_margins(ue.preset_margins(1024, 768, "1:1", "center"))
        self.assertEqual(s.preset, "1:1")

    def test_no_listener_call_without_change(self):
        calls = []
        s = ue.ExpandState(100, 100, Margins(8, 0, 0, 0), listener=lambda: calls.append(1))
        s.set_side("left", 8)
        self.assertEqual(calls, [])


class CropTests(unittest.TestCase):
    def test_crop_and_grow_split(self):
        m = Margins(-100, 50, 30, -20)
        self.assertEqual(m.crop(), (100, 0, 0, 20))
        self.assertEqual(m.grow(), (0, 50, 30, 0))
        self.assertEqual(m.canvas(1000, 800), (930, 830))

    def test_fit_margins_keeps_a_minimum_of_the_photo(self):
        fit = ue.fit_margins
        self.assertEqual(fit(Margins(-500, 0, -500, 0), 1000, 800), Margins(-436, 0, -500, 0))
        # the side that changed gives way
        prev = Margins(-900, 0, -50, 0)
        self.assertEqual(fit(Margins(-900, 0, -500, 0), 1000, 800, previous=prev), Margins(-900, 0, -36, 0))
        self.assertEqual(fit(Margins(-100, -5000, 0, 0), 1000, 800), Margins(-100, -736, 0, 0))
        self.assertEqual(fit(Margins(5000, 0, -99999, 0), 1000, 800), Margins(4096, 0, -936, 0))
        # a photo smaller than the minimum is kept whole
        self.assertEqual(fit(Margins(-60, 0, 0, 0), 50, 50), Margins())

    def test_side_range(self):
        m = Margins(0, 0, -500, 0)
        self.assertEqual(ue.side_range(m, "left", 1000, 800), (-436, ue.MAX_MARGIN))
        self.assertEqual(ue.side_range(m, "right", 1000, 800), (-936, ue.MAX_MARGIN))
        self.assertEqual(ue.side_range(m, "top", 1000, 800), (-736, ue.MAX_MARGIN))
        self.assertEqual(ue.side_range(m, "bogus", 1000, 800), (0, 0))

    def test_drag_inward_past_the_photo_edge_crops(self):
        got = ue.drag_margins(Margins(), "left", 100, 0, 1.0)
        self.assertEqual(got, Margins(-96, 0, 0, 0))
        self.assertEqual(ue.drag_margins(Margins(0, 0, 40, 0), "right", -120, 0, 1.0), Margins(0, 0, -80, 0))

    def test_frame_roundtrip(self):
        m = Margins(-100, 30, 50, -20)
        x, y, w, h = ue.frame_rect(m, 1000, 800)
        self.assertEqual((x, y, w, h), (100, -30, 950, 810))
        self.assertEqual(ue.margins_of_frame(x, y, w, h, 1000, 800), m)

    def test_move_keeps_size_and_snaps(self):
        start = ue.preset_margins(1024, 768, "16:9")  # 342 px wider, centred
        moved = ue.move_frame(start, 171, 0, 1024, 768)
        self.assertEqual(moved.canvas(1024, 768), start.canvas(1024, 768))
        self.assertEqual((moved.left, moved.right), (0, 342))
        self.assertEqual(ue.move_frame(Margins(), 0, 70, 1024, 768).top, -72)
        self.assertEqual(ue.move_frame(Margins(), 0, 30, 1024, 768).top, 0)  # centre magnet

    def test_move_stays_over_the_photo(self):
        start = Margins(100, 100, 100, 100)
        far = ue.move_frame(start, 99999, 99999, 1000, 800)
        x, y, w, h = ue.frame_rect(far, 1000, 800)
        self.assertEqual((x, y), (1000 - ue.MIN_FRAME, 800 - ue.MIN_FRAME))
        self.assertEqual((w, h), (1200, 1000))
        gone = ue.move_frame(start, -99999, -99999, 1000, 800)
        x, y, w, h = ue.frame_rect(gone, 1000, 800)
        self.assertEqual((x + w, y + h), (ue.MIN_FRAME, ue.MIN_FRAME))

    def test_move_can_crop_one_side_and_expand_the_other(self):
        moved = ue.move_frame(Margins(), 200, 0, 1000, 800)
        self.assertEqual((moved.left, moved.right), (-200, 200))
        self.assertEqual(moved.canvas(1000, 800), (1000, 800))

    def test_resize_locked_keeps_ratio_and_the_opposite_corner(self):
        start = ue.preset_margins(1024, 768, "16:9", "left")  # frame x 0..1366, y 0..768
        got = ue.resize_locked(start, "bottom-right", 160, 0, (16, 9), 1024, 768)
        w, h = got.canvas(1024, 768)
        self.assertEqual((got.left, got.top), (0, 0))
        self.assertEqual(w, 1528)
        self.assertLess(abs(w / h - 16 / 9), 0.005)
        up = ue.resize_locked(start, "top-left", -80, -80, (16, 9), 1024, 768)
        w, h = up.canvas(1024, 768)
        x, y, fw, fh = ue.frame_rect(up, 1024, 768)
        self.assertEqual((x + fw, y + fh), (1366, 768))  # bottom-right corner stays
        self.assertLess(abs(w / h - 16 / 9), 0.005)
        self.assertGreater(w, 1366)

    def test_resize_locked_shrinks_to_the_minimum_and_stays_valid(self):
        start = ue.preset_margins(1024, 768, "1:1")
        small = ue.resize_locked(start, "bottom-right", -99999, -99999, (1, 1), 1024, 768)
        # the top-left corner stays at y = -128, so the frame must still reach the photo (64 px): 192 px
        self.assertEqual(small.canvas(1024, 768), (192, 192))
        self.assertTrue(ue.frame_valid(small, 1024, 768))
        free = Margins()  # the frame is the photo: it can shrink to the minimum
        tiny = ue.resize_locked(free, "bottom-right", -99999, -99999, (1, 1), 1024, 1024)
        self.assertEqual(tiny.canvas(1024, 1024), (ue.MIN_FRAME, ue.MIN_FRAME))
        self.assertEqual(ue.resize_locked(start, "right", 50, 0, (1, 1), 1024, 768), start)  # edges are not corners

    def test_resize_locked_never_leaves_the_allowed_range(self):
        start = ue.preset_margins(1024, 768, "4:3")
        got = ue.resize_locked(start, "bottom-right", 99999, 99999, (4, 3), 1024, 768)
        self.assertTrue(ue.frame_valid(got, 1024, 768))
        self.assertLessEqual(max(got.as_dict().values()), ue.MAX_MARGIN)

    def test_hit_region_inside_is_move(self):
        v = ue.view_at(1.0, 150, 50, 250, 200, Margins(50, 0, 0, 0))
        self.assertEqual(ue.hit_region(v, 250, 150), "move")
        self.assertEqual(ue.hit_region(v, 101, 52), "top-left")
        self.assertIsNone(ue.hit_region(v, 20, 150))

    def test_crop_strips(self):
        # photo 0..400 x 0..200, the frame cuts 100 off the left and 50 off the bottom, adds 30 on the right
        v = ue.view_at(1.0, 0, 0, 400, 200, Margins(-100, 0, 30, -50))
        strips = ue.crop_strips(v)
        self.assertIn((0, 150, 400, 50), strips)  # bottom strip
        self.assertIn((0, 0, 100, 150), strips)  # left strip beside the frame
        self.assertEqual(len(strips), 2)
        self.assertEqual(ue.crop_strips(ue.view_at(1.0, 50, 50, 400, 200, Margins(50, 50, 50, 50))), [])

    def test_fit_view_with_a_crop_shows_the_whole_photo(self):
        v = ue.fit_view(600, 380, 1024, 768, Margins(-512, 0, 0, 0))
        self.assertGreaterEqual(v.ix, ue.PAD - 1e-6)
        self.assertLessEqual(v.ix + v.iw, 600 - ue.PAD + 1e-6)
        self.assertAlmostEqual(v.fx - v.ix, 512 * v.scale)


class RatioStateTests(unittest.TestCase):
    def test_orientation_helpers(self):
        self.assertTrue(ue.has_orientation("4:3"))
        self.assertFalse(ue.has_orientation("1:1"))
        self.assertFalse(ue.has_orientation("custom"))
        self.assertEqual(ue.oriented("4:3", True), "3:4")
        self.assertEqual(ue.oriented("3:4", False), "4:3")
        self.assertEqual(ue.oriented("1:1", True), "1:1")
        self.assertEqual(ue.ratio_button("9:16"), "16:9")
        self.assertIs(ue.is_portrait("3:4"), True)
        self.assertIs(ue.is_portrait("16:9"), False)
        self.assertIsNone(ue.is_portrait("original"))

    def test_apply_ratio_follows_orientation_and_switch_turns_it(self):
        s = ue.ExpandState(1024, 768)
        s.apply_ratio("16:9")
        self.assertEqual((s.preset, s.portrait), ("16:9", False))
        s.set_portrait(True)
        self.assertEqual(s.preset, "9:16")
        w, h = s.canvas
        self.assertLess(w, h)
        s.apply_ratio("3:2")
        self.assertEqual(s.preset, "2:3")
        s.apply_ratio("custom")
        s.set_portrait(False)
        self.assertEqual(s.preset, "custom")
        self.assertFalse(s.portrait)

    def test_move_keeps_the_preset_and_the_size(self):
        s = ue.ExpandState(1024, 768)
        s.apply_preset("16:9")
        start, size = s.margins, s.canvas
        s.move_from(start, 80, 0)
        self.assertEqual(s.preset, "16:9")
        self.assertEqual(s.canvas, size)
        self.assertEqual(s.margins.left + s.margins.right, start.left + start.right)
        self.assertLessEqual(abs(s.margins.left - (start.left - 80)), 4)

    def test_corner_drag_keeps_the_ratio_and_preset(self):
        s = ue.ExpandState(1024, 768)
        s.apply_preset("3:2")
        start = s.margins
        s.resize_from(start, "bottom-right", 100, 0)
        self.assertEqual(s.preset, "3:2")
        w, h = s.canvas
        self.assertLess(abs(w / h - 1.5), 0.005)
        self.assertGreater(w, start.canvas(1024, 768)[0])

    def test_corner_drag_without_a_preset_is_free(self):
        s = ue.ExpandState(1024, 768, Margins(10, 10, 10, 10))
        s.resize_from(s.margins, "bottom-right", 100, 40)
        self.assertEqual(s.preset, "custom")
        self.assertEqual(s.margins, Margins(10, 10, 112, 48))  # snapped to 8 px

    def test_edge_drag_in_a_preset_becomes_custom(self):
        s = ue.ExpandState(1024, 768)
        s.apply_preset("16:9")
        s.set_side("top", 64)
        self.assertEqual(s.preset, "custom")

    def test_wide_photo_to_4_3_can_crop_the_sides_and_add_sky(self):
        s = ue.ExpandState(1920, 1080)
        s.apply_preset("4:3")
        self.assertEqual(s.canvas, (1920, 1440))
        # shrink the locked frame from its top-left corner, then slide it right
        s.resize_from(s.margins, "bottom-right", -240, -180)
        w, h = s.canvas
        self.assertEqual((w, h), (1680, 1260))
        s.move_from(s.margins, 240, 0)
        self.assertEqual(s.preset, "4:3")
        self.assertEqual(s.margins.right, 0)
        self.assertEqual(s.margins.left, -240)  # 240 cropped on the left
        self.assertEqual(s.margins.crop(), (240, 0, 0, 0))
        self.assertEqual(s.margins.grow()[1] + s.margins.grow()[3], 180)  # sky on top and/or below

    def test_anchor_snaps_the_frame_and_keeps_its_size(self):
        s = ue.ExpandState(1000, 800)
        s.apply_preset("1:1")  # 1000 x 1000, 200 more in height
        s.move_from(s.margins, 0, -80)
        size = s.canvas
        s.set_anchor("top")
        self.assertEqual(s.canvas, size)
        self.assertEqual((s.margins.top, s.margins.bottom), (0, 200))
        self.assertEqual(s.preset, "1:1")

    def test_anchor_for_a_cropping_frame(self):
        s = ue.ExpandState(1000, 800, Margins(-100, 0, -100, 0))
        s.set_anchor("left")
        self.assertEqual((s.margins.left, s.margins.right), (0, -200))
        s.set_anchor("right")
        self.assertEqual((s.margins.left, s.margins.right), (-200, 0))

    def test_negative_margins_are_limited_to_keep_the_photo(self):
        s = ue.ExpandState(1000, 800, Margins(-990, 0, 0, 0))
        self.assertEqual(s.margins.left, -(1000 - ue.MIN_FRAME))
        s.set_side("right", -500)
        self.assertEqual(s.margins.right, 0)  # nothing left to crop on that axis
        self.assertEqual(s.side_range("right")[0], 0)

    def test_older_saved_settings_still_load(self):
        s = ue.ExpandState(1024, 768, Margins.from_dict({"left": 256, "top": 0, "right": 256, "bottom": 0}))
        self.assertEqual(s.margins, Margins(256, 0, 256, 0))
        self.assertEqual(s.preset, "custom")


if __name__ == "__main__":
    unittest.main()
