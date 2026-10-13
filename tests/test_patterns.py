"""Behaviour of the calibration patterns.

These are measuring instruments, so the properties worth asserting are the ones that make
a reading trustworthy: that a value means the luminance it claims, that the gamma-match
lines land on individual device pixels, and that nothing asks the panel for light it
cannot produce.
"""

from __future__ import annotations

import struct
import unittest

from vhdr_color.gamma_correction import pq_eotf, pq_inverse_eotf
from vhdr_color.patterns import (
    PLACEMENT_NITS,
    placement_frame,
    PATTERNS,
    REFERENCE_WHITE_NITS,
    TONE_TRACKING_DELTA_PQ,
    PatternContext,
    compose,
    measurement_frame,
    pattern_by_key,
    render,
    window_size,
)

HDR = PatternContext(is_hdr=True, sdr_white_nits=240.0, peak_nits=1080.0, max_full_frame_nits=1080.0)
SDR = PatternContext(is_hdr=False, sdr_white_nits=240.0)


def pixel_at(frame: bytes, width: int, x: int, y: int) -> tuple[float, float, float, float]:
    return struct.unpack_from("<4e", frame, (y * width + x) * 8)


def channels(frame: bytes) -> list[float]:
    count = len(frame) // 2
    values = struct.unpack(f"<{count}e", frame)
    return [values[i] for i in range(0, count, 4)]  # red channel of every pixel


class FrameShapeTests(unittest.TestCase):
    def test_every_pattern_fills_the_surface_exactly(self):
        """A frame of the wrong length is rejected by the swapchain, not tolerated."""
        for pattern in PATTERNS:
            for width, height in ((1, 1), (17, 5), (640, 360), (1921, 1081)):
                with self.subTest(pattern=pattern.key, size=(width, height)):
                    frame = render(pattern, width, height, HDR)
                    self.assertEqual(len(frame), width * height * 8)

    def test_odd_sizes_do_not_lose_a_column(self):
        """Integer division across columns must give the remainder to the last one."""
        width, height = 1003, 7
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern.key):
                self.assertEqual(len(render(pattern, width, height, HDR)), width * height * 8)

    def test_keys_are_unique_and_resolvable(self):
        keys = [pattern.key for pattern in PATTERNS]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIsNotNone(pattern_by_key(key))
        self.assertIsNone(pattern_by_key("no-such-pattern"))


class LuminanceEncodingTests(unittest.TestCase):
    def test_hdr_values_are_absolute_luminance(self):
        """scRGB is scene-referred on HDR: 1.0 is 80 nits, so 203 nits is 2.5375."""
        self.assertTrue(HDR.absolute)
        self.assertAlmostEqual(HDR.encode(203.0), 2.5375, places=4)
        self.assertAlmostEqual(HDR.encode(80.0), 1.0, places=6)

    def test_sdr_values_are_relative_to_reference_white(self):
        """scRGB is display-referred on SDR, so absolute nits are not addressable."""
        self.assertFalse(SDR.absolute)
        self.assertAlmostEqual(SDR.encode(240.0), 1.0, places=6)
        self.assertAlmostEqual(SDR.encode(120.0), 0.5, places=6)

    def test_sdr_cannot_exceed_its_own_white(self):
        self.assertEqual(SDR.encode(5000.0), 1.0)

    def test_negative_luminance_never_encodes_negative(self):
        for context in (HDR, SDR):
            with self.subTest(hdr=context.is_hdr):
                self.assertEqual(context.encode(-10.0), 0.0)

    def test_the_ceiling_follows_the_display(self):
        self.assertEqual(HDR.ceiling_nits, 1080.0)
        self.assertEqual(SDR.ceiling_nits, 240.0)


class CeilingTests(unittest.TestCase):
    """Asking for light above the panel's peak invites chasing a difference that the
    display physically cannot show."""

    def test_no_fixed_pattern_exceeds_the_panels_peak(self):
        """Level-driven patterns are excluded: following the probe past peak is how they
        find peak in the first place."""
        dim = PatternContext(is_hdr=True, peak_nits=400.0, max_full_frame_nits=400.0)
        allowed = dim.encode(400.0) + 1e-3
        for pattern in PATTERNS:
            if pattern.level_driven:
                continue
            with self.subTest(pattern=pattern.key):
                brightest = max(channels(render(pattern, 200, 120, dim)))
                self.assertLessEqual(brightest, allowed)

    def test_a_dim_panel_still_gets_usable_shadow_patches(self):
        """Filtering the near-black ladder by the ceiling must not empty it."""
        tiny = PatternContext(is_hdr=True, peak_nits=45.0, max_full_frame_nits=45.0)
        frame = render(pattern_by_key("near-black"), 400, 200, tiny)
        self.assertGreater(len(set(round(v, 5) for v in channels(frame))), 1)


class GammaMatchTests(unittest.TestCase):
    """The pattern is only valid if its lines land on individual device pixels."""

    WIDTH, HEIGHT = 400, 200

    def setUp(self):
        self.frame = render(pattern_by_key("gamma-match"), self.WIDTH, self.HEIGHT, HDR)

    def test_lines_alternate_every_single_row(self):
        """One resampled line and the pattern reports a gamma that was never on screen."""
        edge = 2  # sample away from the centre patches
        lit = [pixel_at(self.frame, self.WIDTH, edge, y)[0] for y in range(0, 40, 2)]
        dark = [pixel_at(self.frame, self.WIDTH, edge, y)[0] for y in range(1, 41, 2)]
        self.assertTrue(all(value > 0.0 for value in lit), "even rows should be lit")
        self.assertTrue(all(value == 0.0 for value in dark), "odd rows should be black")

    def test_the_lit_line_is_at_reference_white(self):
        value = pixel_at(self.frame, self.WIDTH, 2, 0)[0]
        self.assertAlmostEqual(value, HDR.encode(REFERENCE_WHITE_NITS), places=3)

    def test_a_candidate_patch_sits_at_true_half_luminance(self):
        """scRGB is linear, so interleaved lines average to exactly half. One patch must
        be at that value or the pattern has no correct answer."""
        expected = HDR.encode(REFERENCE_WHITE_NITS / 2.0)
        centre = self.WIDTH // 2
        column = [pixel_at(self.frame, self.WIDTH, centre, y)[0] for y in range(self.HEIGHT)]
        self.assertTrue(
            any(abs(value - expected) < 1e-3 for value in column),
            f"no patch at {expected:.4f}; column held {sorted(set(round(v, 4) for v in column))}",
        )

    def test_patches_bracket_the_ideal_in_both_directions(self):
        """A mismatch should read as a direction, so candidates must straddle the answer."""
        ideal = HDR.encode(REFERENCE_WHITE_NITS / 2.0)
        centre = self.WIDTH // 2
        values = {round(pixel_at(self.frame, self.WIDTH, centre, y)[0], 5)
                  for y in range(self.HEIGHT)}
        patches = {v for v in values if v > 0.0}
        self.assertTrue(any(v < ideal - 1e-4 for v in patches), "no darker candidate")
        self.assertTrue(any(v > ideal + 1e-4 for v in patches), "no brighter candidate")


class StaircaseTests(unittest.TestCase):
    def test_steps_are_perceptually_spaced_not_linear_in_nits(self):
        """Evenly spaced nits would spend nearly every step on highlights.

        PQ spacing is the point: the gaps in luminance must grow towards the top.
        """
        frame = render(pattern_by_key("grey-staircase"), 1600, 4, HDR)
        levels = sorted({round(v, 6) for v in channels(frame)})
        nits = [value * 80.0 for value in levels]
        gaps = [b - a for a, b in zip(nits, nits[1:])]
        self.assertGreater(len(gaps), 4)
        self.assertGreater(gaps[-1], gaps[0] * 5,
                           "steps look linear in luminance, so PQ spacing was lost")

    def test_the_top_step_reaches_the_panel_ceiling(self):
        frame = render(pattern_by_key("grey-staircase"), 1600, 4, HDR)
        self.assertAlmostEqual(max(channels(frame)), HDR.encode(1080.0), places=3)

    def test_the_bottom_step_is_black(self):
        frame = render(pattern_by_key("grey-staircase"), 1600, 4, HDR)
        self.assertEqual(min(channels(frame)), 0.0)


class NeutralRampTests(unittest.TestCase):
    def test_the_ramp_rises_monotonically(self):
        frame = render(pattern_by_key("neutral-ramp"), 512, 2, HDR)
        row = [pixel_at(frame, 512, x, 0)[0] for x in range(512)]
        self.assertEqual(row, sorted(row))

    def test_the_ramp_is_neutral_at_every_point(self):
        """Any channel imbalance here would be mistaken for display tint.

        Previously written as ``assertEqual((red, green), (red, blue))``, which
        by tuple equality reduces to ``red == red and green == blue`` -- red was
        compared only against itself, so a red cast at every point, the most
        likely imbalance given the ramp is built from one scalar per pixel, went
        unchecked. Emitting ``_colour_pixel(value * 2.0, value, value)`` passed.
        """
        frame = render(pattern_by_key("neutral-ramp"), 256, 2, HDR)
        for x in range(0, 256, 16):
            red, green, blue, _ = pixel_at(frame, 256, x, 0)
            with self.subTest(x=x):
                self.assertEqual(red, green)
                self.assertEqual(green, blue)

    def test_the_ramp_is_evenly_spaced_in_pq(self):
        frame = render(pattern_by_key("neutral-ramp"), 256, 1, HDR)
        row = [pixel_at(frame, 256, x, 0)[0] * 80.0 for x in range(256)]
        codes = [pq_inverse_eotf(value) for value in row]
        gaps = [b - a for a, b in zip(codes, codes[1:])]
        self.assertLess(max(gaps) - min(gaps), 0.01, "ramp is not uniform in PQ")


class ColourPatchTests(unittest.TestCase):
    def test_patches_are_held_at_diffuse_white_not_peak(self):
        """At peak this would mostly measure highlight rolloff, not colour."""
        frame = render(pattern_by_key("colour-patches"), 500, 200, HDR)
        self.assertAlmostEqual(max(channels(frame)), HDR.encode(REFERENCE_WHITE_NITS), places=3)

    def test_the_first_patch_is_pure_red(self):
        frame = render(pattern_by_key("colour-patches"), 500, 200, HDR)
        red, green, blue, _ = pixel_at(frame, 500, 2, 2)
        self.assertGreater(red, 0.0)
        self.assertEqual((green, blue), (0.0, 0.0))


class CompositionTests(unittest.TestCase):
    """Every pattern is shown in one centred window on black, as display patches always
    have been. On an emissive panel it is also the only way readings stay comparable: a
    full-screen pattern engages the brightness limiter differently depending on how bright
    the pattern is, so two measurements are not measuring the same thing."""

    WIDTH, HEIGHT = 800, 600

    def frame(self, key="solid-patch", **kwargs):
        return compose(self.WIDTH, self.HEIGHT, pattern_by_key(key), HDR, **kwargs)

    def test_the_window_is_a_tenth_of_the_screen_area(self):
        width, height = window_size(3840, 2160)
        self.assertAlmostEqual((width * height) / (3840 * 2160), 0.10, places=3)

    def test_everything_outside_the_window_is_true_black(self):
        """A lit surround is itself part of what engages the limiter."""
        frame = self.frame()
        for x, y in ((0, 0), (self.WIDTH - 1, 0), (0, self.HEIGHT - 1),
                     (self.WIDTH - 1, self.HEIGHT - 1), (5, self.HEIGHT // 2)):
            with self.subTest(point=(x, y)):
                self.assertEqual(pixel_at(frame, self.WIDTH, x, y)[0], 0.0)

    def test_the_window_is_centred(self):
        frame = self.frame()
        row = [pixel_at(frame, self.WIDTH, x, self.HEIGHT // 2)[0] for x in range(self.WIDTH)]
        lit = [x for x, value in enumerate(row) if value > 0.0]
        self.assertTrue(lit)
        self.assertAlmostEqual((lit[0] + lit[-1]) / 2, (self.WIDTH - 1) / 2, delta=1.5)

    def test_every_pattern_composes_to_the_exact_surface_size(self):
        for pattern in PATTERNS:
            for width, height in ((1, 1), (37, 11), (800, 600)):
                with self.subTest(pattern=pattern.key, size=(width, height)):
                    frame = compose(width, height, pattern, HDR)
                    self.assertEqual(len(frame), width * height * 8)

    def test_the_solid_patch_sits_at_the_probe_level(self):
        """It is the patch a meter reads, so it must be exactly where it was put."""
        from dataclasses import replace

        context = replace(HDR, probe_nits=137.0)
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("solid-patch"), context)
        centre = pixel_at(frame, self.WIDTH, self.WIDTH // 2, self.HEIGHT // 2)[0]
        self.assertAlmostEqual(centre, context.encode(137.0), places=3)


class OverlayPlacementTests(unittest.TestCase):
    """Controls go hard against one edge. Anything near the patch contaminates both the
    reading and the viewer's dark adaptation."""

    WIDTH, HEIGHT = 800, 600
    OVERLAY = (bytes([255, 255, 255, 255]) * (60 * 200), 60, 200)

    def test_the_overlay_lands_on_the_right_when_asked(self):
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=self.OVERLAY, overlay_side="right")
        self.assertGreater(pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0], 0.0)
        self.assertEqual(pixel_at(frame, self.WIDTH, 2, self.HEIGHT // 2)[0], 0.0)

    def test_the_overlay_lands_on_the_left_when_asked(self):
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=self.OVERLAY, overlay_side="left")
        self.assertGreater(pixel_at(frame, self.WIDTH, 2, self.HEIGHT // 2)[0], 0.0)
        self.assertEqual(pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0], 0.0)

    def test_the_overlay_is_held_far_below_diffuse_white(self):
        """It shares the screen with the patch, so its light counts too."""
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=self.OVERLAY, overlay_side="right")
        nits = pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0] * 80.0
        self.assertLess(nits, 30.0, "an overlay this bright would engage the limiter")
        self.assertGreater(nits, 1.0, "an overlay this dim would be unreadable")

    def test_a_transparent_overlay_contributes_no_light(self):
        clear = (bytes([255, 255, 255, 0]) * (60 * 200), 60, 200)
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR,
                        overlay=clear, overlay_side="right")
        self.assertEqual(pixel_at(frame, self.WIDTH, self.WIDTH - 2, self.HEIGHT // 2)[0], 0.0)

    def test_an_oversized_overlay_does_not_corrupt_the_frame(self):
        huge = (bytes([255, 255, 255, 255]) * (4000 * 4000), 4000, 4000)
        frame = compose(self.WIDTH, self.HEIGHT, pattern_by_key("near-black"), HDR, overlay=huge)
        self.assertEqual(len(frame), self.WIDTH * self.HEIGHT * 8)



class GuidanceTests(unittest.TestCase):
    """A pattern nobody can act on is decoration."""

    def test_every_pattern_says_what_it_is_for_and_what_to_do(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern.key):
                self.assertTrue(pattern.title.strip())
                self.assertGreater(len(pattern.purpose), 20)
                self.assertGreater(len(pattern.instructions), 40)
                self.assertGreater(len(pattern.criterion), 20,
                                   "a pattern with no stated target cannot be aimed at")


class PqHelperSanityTests(unittest.TestCase):
    def test_pq_round_trips(self):
        for nits in (0.1, 1.0, 100.0, 203.0, 1000.0, 4000.0):
            with self.subTest(nits=nits):
                self.assertAlmostEqual(pq_eotf(pq_inverse_eotf(nits)), nits, places=2)


class ShapeSensitivityTests(unittest.TestCase):
    """How much brighter the shape is than its surround IS the sensitivity of the
    measurement: the two merge only once the display can no longer keep them apart, so a
    large gap merges long after the real limit and reports a threshold that is too high."""

    def test_the_gap_is_small_enough_to_find_a_real_limit(self):
        from vhdr_color.patterns import SHAPE_CONTRAST

        self.assertLessEqual(SHAPE_CONTRAST, 1.10,
                             "a coarse gap puts the threshold well above where a panel clips")
        self.assertGreater(SHAPE_CONTRAST, 1.02,
                           "too fine to see means no threshold can be found at all")

    def test_both_clipping_patterns_use_the_same_gap(self):
        """They ask the same question, so they must ask it with the same sensitivity."""
        from dataclasses import replace

        from vhdr_color.patterns import SHAPE_CONTRAST

        context = replace(HDR, probe_nits=300.0)
        for key in ("peak-white", "full-frame-white"):
            with self.subTest(pattern=key):
                frame = render(pattern_by_key(key), 200, 200, context)
                surround = pixel_at(frame, 200, 2, 2)[0]
                shape = pixel_at(frame, 200, 100, 100)[0]
                self.assertAlmostEqual(shape / surround, SHAPE_CONTRAST, places=2)


class ToneTrackingTests(unittest.TestCase):
    """One level at a time. Adaptation follows the brightest thing in view, so a bright
    field anywhere on screen makes a near-threshold patch at the dark end unjudgeable no
    matter what the display is doing -- the reading then describes the viewer, not the
    panel. The first version showed all seven at once and was unusable for exactly that."""

    WIDTH, HEIGHT = 400, 300

    def sample(self, level):
        from dataclasses import replace

        context = replace(HDR, probe_nits=level)
        frame = render(pattern_by_key("tone-tracking"), self.WIDTH, self.HEIGHT, context)
        field = pixel_at(frame, self.WIDTH, self.WIDTH // 2, 8)[0] * 80.0
        bar = pixel_at(frame, self.WIDTH, self.WIDTH // 2, self.HEIGHT // 2)[0] * 80.0
        return field, bar

    def levels(self):
        from vhdr_color.patterns import tone_tracking_levels

        return tone_tracking_levels(HDR)

    def test_only_one_level_is_ever_on_screen(self):
        """The whole point: nothing else in view to drag the eye's adaptation."""
        from dataclasses import replace

        context = replace(HDR, probe_nits=self.levels()[0])
        frame = render(pattern_by_key("tone-tracking"), self.WIDTH, self.HEIGHT, context)
        distinct = {round(value, 5) for value in channels(frame)}
        self.assertLessEqual(len(distinct), 2, f"more than a field and a bar: {distinct}")

    def test_the_darkest_level_has_no_bright_field_beside_it(self):
        field, bar = self.sample(self.levels()[0])
        self.assertLess(max(field, bar), 1.0, "something bright is sharing the screen")

    def test_the_bar_is_a_near_threshold_lift_at_every_level(self):
        for level in self.levels():
            field, bar = self.sample(level)
            with self.subTest(level=round(level, 3)):
                self.assertGreater(bar, field)
                self.assertLess(bar / field, 1.20, "too obvious to register a change")

    def test_the_step_is_the_same_perceptual_size_at_every_level(self):
        for level in self.levels():
            field, bar = self.sample(level)
            with self.subTest(level=round(level, 3)):
                self.assertAlmostEqual(
                    pq_inverse_eotf(bar) - pq_inverse_eotf(field),
                    TONE_TRACKING_DELTA_PQ, places=3)

    def test_the_levels_span_shadows_to_highlights(self):
        levels = self.levels()
        self.assertEqual(list(levels), sorted(levels))
        self.assertLess(levels[0], 1.0)
        self.assertGreater(levels[-1], 100.0)

    def test_nothing_clips_against_the_ceiling(self):
        for level in self.levels():
            _field, bar = self.sample(level)
            self.assertLess(bar, HDR.ceiling_nits)

    def test_the_pattern_declares_its_levels_so_the_view_can_walk_them(self):
        self.assertIsNotNone(pattern_by_key("tone-tracking").levels)

    def test_the_guidance_names_all_three_controls(self):
        """A pattern that says something is wrong without saying what to move is a riddle."""
        text = pattern_by_key("tone-tracking").instructions
        for control in ("Midtone Brightness", "Contrast", "Gamma"):
            with self.subTest(control=control):
                self.assertIn(control, text)

    def test_the_guidance_says_to_walk_the_levels(self):
        text = pattern_by_key("tone-tracking").instructions
        self.assertIn("Up and Down", text)


class TrackingSensitivityTests(unittest.TestCase):
    """The step has to sit near the threshold of visibility or the pattern cannot report a
    change. What the eye sees is the transfer function's slope -- roughly f'(c) times the
    step -- so an obvious block stays obvious when the curve moves, and reports nothing."""

    def test_the_step_is_within_a_few_just_noticeable_differences(self):
        from vhdr_color.patterns import TONE_TRACKING_DELTA_PQ

        # One JND is about one to two ten-bit PQ codes.
        codes = TONE_TRACKING_DELTA_PQ * 1023
        self.assertLess(codes, 8, "far above threshold: an obvious block, insensitive to the curve")
        self.assertGreater(codes, 1.5, "below threshold: nothing to see at all")

    def test_no_patch_is_more_than_a_slight_lift_on_its_background(self):
        """At 0.03 PQ the darkest patch was 2.35x its surround. That is not a faint patch."""
        from dataclasses import replace

        from vhdr_color.patterns import tone_tracking_levels

        for level in tone_tracking_levels(HDR):
            context = replace(HDR, probe_nits=level)
            frame = render(pattern_by_key("tone-tracking"), 400, 300, context)
            field = pixel_at(frame, 400, 200, 8)[0]
            bar = pixel_at(frame, 400, 200, 150)[0]
            with self.subTest(level=round(level, 3)):
                self.assertLess(bar / field, 1.20,
                                "too obvious to register a change in the curve")
                self.assertGreater(bar / field, 1.01, "too faint to find at all")

    def test_moving_a_control_measurably_changes_the_steps(self):
        """If the controls did not move these, the pattern could not be used to set them."""
        from dataclasses import replace

        from vhdr_color.curves import build_transform
        from vhdr_color.model import ModeState
        from vhdr_color.patterns import tone_tracking_levels

        def lifts(contrast):
            state = ModeState.neutral("HDR")
            state.contrast = contrast
            transform = build_transform(state, hdr=True)

            def through(nits):
                position = pq_inverse_eotf(nits) * (len(transform.red) - 1)
                low = int(position)
                high = min(len(transform.red) - 1, low + 1)
                return pq_eotf(transform.red[low]
                               + (transform.red[high] - transform.red[low]) * (position - low))

            found = []
            for level in tone_tracking_levels(HDR):
                context = replace(HDR, probe_nits=level)
                frame = render(pattern_by_key("tone-tracking"), 400, 300, context)
                field = pixel_at(frame, 400, 200, 8)[0] * 80.0
                bar = pixel_at(frame, 400, 200, 150)[0] * 80.0
                found.append(through(bar) / through(field))
            return found

        neutral, altered = lifts(0.0), lifts(-10.0)
        changed = [abs(a - b) / (a - 1.0) for a, b in zip(neutral, altered)]
        self.assertGreater(max(changed), 0.05,
                           "the controls barely move the steps, so nothing can be judged")


class ToneTrackingGuidanceTests(unittest.TestCase):
    """The guidance has to describe the pattern that is actually on screen. Written for the
    all-at-once layout, it asked the viewer to compare levels side by side -- the one thing
    the eye cannot do here, and the reason that layout was abandoned."""

    def instructions(self):
        return pattern_by_key("tone-tracking").instructions

    def test_it_asks_about_the_level_on_screen_first(self):
        self.assertIn("Assess the bar here", self.instructions())

    def test_it_says_how_to_reach_the_other_levels(self):
        self.assertIn("Up and Down", self.instructions())

    def test_it_never_asks_for_a_side_by_side_comparison(self):
        """Only one level is visible, so nothing can be compared against anything."""
        text = self.instructions().lower()
        for phrase in ("squares stand out most", "than the middle", "all seven equally"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, text)

    def test_each_outcome_names_one_control_and_a_direction(self):
        for line in self.instructions().split("\n"):
            if "->" not in line:
                continue
            with self.subTest(line=line.strip()):
                symptom, action = line.split("->")
                self.assertTrue(symptom.strip(), "an outcome with no symptom")
                self.assertTrue(
                    any(word in action for word in ("raise", "lower", "adjust")),
                    "an outcome with no direction to move in",
                )

    def test_it_says_the_overall_pattern_is_what_matters(self):
        """A single level says nothing on its own; that has to be stated or it will be
        read as a per-level verdict."""
        self.assertIn("overall pattern", self.instructions())

    def test_the_criterion_describes_this_level_not_a_row_of_them(self):
        criterion = pattern_by_key("tone-tracking").criterion
        self.assertIn("this level", criterion)


class InstructionVoiceTests(unittest.TestCase):
    """This text ships to every user of the tool, not to whoever it was drafted beside.

    It drifted into second-person commentary during development -- reassurance, asides,
    and references to a previous pattern as though the reader had just been looking at it.
    """

    CONVERSATIONAL = (
        "this one", "you can", "you see", "your eyes", "costs you", "that is normal",
        "same test as before", "come back", "go slowly", "doing its job", "what you saw",
    )

    def all_copy(self):
        for pattern in PATTERNS:
            yield pattern.key, " ".join(
                (pattern.purpose, pattern.criterion, pattern.instructions)).lower()

    def test_no_pattern_addresses_the_reader_conversationally(self):
        for key, text in self.all_copy():
            for phrase in self.CONVERSATIONAL:
                with self.subTest(pattern=key, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_no_pattern_refers_to_another_by_position(self):
        """"The one before" means nothing to someone who arrived by number key."""
        for key, text in self.all_copy():
            for phrase in ("as before", "previous pattern", "the one before"):
                with self.subTest(pattern=key, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_every_criterion_is_a_statement_of_the_correct_state(self):
        """Not an instruction and not a description of the pattern: the thing to check."""
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern.key):
                self.assertGreater(len(pattern.criterion), 20)
                self.assertFalse(pattern.criterion.lower().startswith(("press ", "hold ", "tap ")))

    def test_numbered_steps_are_used_wherever_there_is_a_procedure(self):
        for key in ("black-level", "peak-white", "full-frame-white", "solid-patch"):
            with self.subTest(pattern=key):
                self.assertIn("1.", pattern_by_key(key).instructions)


class MeasurementFrameTests(unittest.TestCase):
    """The patch a colorimeter reads.

    Plainer than the by-eye patterns on purpose: anything else on screen adds light
    the instrument integrates along with the patch.
    """

    def setUp(self):
        self.context = PatternContext(is_hdr=True, peak_nits=1015.0)
        self.width, self.height = 320, 200

    def frame(self, rgb=(1.0, 1.0, 1.0), nits=800.0, **kwargs):
        return measurement_frame(
            self.width, self.height, rgb, nits, self.context, **kwargs
        )

    def pixel(self, frame, x, y):
        return struct.unpack_from("<4e", frame, (y * self.width + x) * 8)

    def centre(self, frame):
        return self.pixel(frame, self.width // 2, self.height // 2)

    def test_frame_is_exactly_the_size_the_swapchain_expects(self):
        """HdrSurface.present rejects any other length; stride is width * 8."""
        self.assertEqual(len(self.frame()), self.width * self.height * 8)

    def test_the_patch_carries_the_luminance_it_was_asked_for(self):
        """scRGB 1.0 is 80 nits, so this is the whole measurement contract."""
        self.assertAlmostEqual(self.centre(self.frame(nits=800.0))[0] * 80.0, 800.0, places=1)

    def test_everything_outside_the_patch_is_black(self):
        frame = self.frame()
        for x, y in ((0, 0), (self.width - 1, 0), (0, self.height - 1), (self.width - 1, self.height - 1)):
            self.assertEqual(self.pixel(frame, x, y)[:3], (0.0, 0.0, 0.0))

    def test_the_patch_covers_the_requested_fraction_of_screen_area(self):
        """An emissive panel's brightness limiter responds to total output, so a
        patch of changing size is not measuring one thing between readings."""
        frame = self.frame()
        lit = sum(
            1
            for y in range(self.height)
            for x in range(self.width)
            if self.pixel(frame, x, y)[0] > 0.0
        )
        self.assertAlmostEqual(lit / (self.width * self.height), 0.10, delta=0.01)

    def test_a_primary_is_driven_as_hard_as_the_white_it_is_compared_against(self):
        """Measuring red at a different drive would sample a different point on the
        panel's response and make the chromaticities incomparable."""
        white = self.centre(self.frame((1.0, 1.0, 1.0), 800.0))
        red = self.centre(self.frame((1.0, 0.0, 0.0), 800.0))
        self.assertAlmostEqual(red[0], white[0], places=4)
        self.assertEqual((red[1], red[2]), (0.0, 0.0))

    def test_the_black_patch_emits_no_light_at_all(self):
        """The black reading is the panel's floor, so any light this frame adds of
        its own would be measured as part of it."""
        frame = self.frame((0.0, 0.0, 0.0), 0.0)
        for y in (0, self.height // 2, self.height - 1):
            for x in (0, self.width // 2, self.width - 1):
                self.assertEqual(self.pixel(frame, x, y)[:3], (0.0, 0.0, 0.0))

    def test_the_patch_is_centred(self):
        """A meter is placed in the middle of the screen; an off-centre patch would
        be read partly off its edge."""
        frame = self.frame()
        rows = [
            y
            for y in range(self.height)
            if self.pixel(frame, self.width // 2, y)[0] > 0.0
        ]
        self.assertAlmostEqual((rows[0] + rows[-1]) / 2.0, (self.height - 1) / 2.0, delta=1.0)

    def test_survives_a_surface_smaller_than_the_patch_maths(self):
        self.assertEqual(len(measurement_frame(1, 1, (1.0, 1.0, 1.0), 100.0, self.context)), 8)


class PlacementTargetTests(unittest.TestCase):
    """The green square the meter is placed on, and that it detects itself.

    Shown before any reading is taken, which is the one moment anything may be lit on
    this surface. Once the run starts the screen carries a single patch on black,
    because every other lit pixel is light the instrument integrates.
    """

    def context(self):
        return PatternContext(is_hdr=True, sdr_white_nits=240.0, peak_nits=1000.0)

    def pixel(self, raw, width, x, y):
        return struct.unpack_from("<4e", raw, (y * width + x) * 8)[:3]

    def test_the_target_is_green_and_nothing_else(self):
        """Green is what makes it self-verifying: it is the primary furthest from both
        a dark screen and from room light, so "the meter sees the target" cannot be
        confused with "the meter is picking up the room"."""
        width, height = 400, 300
        raw = placement_frame(width, height, self.context())
        red, green, blue = self.pixel(raw, width, width // 2, height // 2)
        self.assertEqual(0.0, red)
        self.assertEqual(0.0, blue)
        self.assertGreater(green, 0.0)

    def test_it_is_drawn_at_the_luminance_it_declares(self):
        width, height = 400, 300
        raw = placement_frame(width, height, self.context())
        _r, green, _b = self.pixel(raw, width, width // 2, height // 2)
        self.assertAlmostEqual(PLACEMENT_NITS, green * 80.0, places=1)

    def test_everything_outside_the_target_stays_black(self):
        """A lit surround would be light under the meter's rim, and would also make the
        detection threshold meaningless."""
        width, height = 400, 300
        raw = placement_frame(width, height, self.context())
        for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
            with self.subTest(corner=(x, y)):
                self.assertEqual((0.0, 0.0, 0.0), self.pixel(raw, width, x, y))

    def test_it_matches_the_geometry_the_patches_will_use(self):
        """A meter aligned to this target has to be aligned to every patch that
        follows: a patch edge under the aperture reads part black and returns a
        luminance nothing downstream can tell is wrong."""
        width, height = 400, 300
        target = placement_frame(width, height, self.context())
        patch = measurement_frame(width, height, (1.0, 1.0, 1.0), 100.0, self.context())

        def lit_columns(raw, row):
            return [
                x for x in range(width)
                if any(v > 0.0 for v in self.pixel(raw, width, x, row))
            ]

        row = height // 2
        self.assertEqual(lit_columns(patch, row), lit_columns(target, row))

    def test_a_reading_of_the_target_is_what_detection_accepts(self):
        """The two halves have to agree, or the meter watches for something the screen
        never shows."""
        from vhdr_color.measure import sees_placement_target
        from sdr_hdr_profile_creator.meter import Reading

        # A plausible instrument reading of a green patch at PLACEMENT_NITS.
        seen = Reading(X=0.34 * PLACEMENT_NITS, Y=PLACEMENT_NITS, Z=0.05 * PLACEMENT_NITS,
                       x=0.24, y=0.71)
        self.assertTrue(sees_placement_target(seen))


class GammaTranslationRegressionTests(unittest.TestCase):
    def test_checkerboard_has_an_exact_half_light_reference_in_both_axes(self):
        frame=render(pattern_by_key("gamma-match"),400,200,HDR)
        values=[pixel_at(frame,400,x,y)[0] for y in range(2) for x in range(2)]
        self.assertEqual(values[0],values[3]);self.assertEqual(values[1],values[2])
        self.assertAlmostEqual(sum(values)/4,HDR.encode(REFERENCE_WHITE_NITS)/2,places=3)
        self.assertNotEqual(values[0],values[1])

    def test_actual_profile_gamma_moves_target_relative_to_reference_in_the_documented_direction(self):
        from vhdr_color.curves import build_transform
        from vhdr_color.greyscale import sample
        from vhdr_color.model import ModeState
        for correction in ("Off", "Auto (240 nits)"):
            ratios=[]
            for gamma in (1.6,2.0,2.2,2.4,3.0):
                state=ModeState.neutral("HDR");state.gamma=gamma;state.sdr_gamma_correction=correction
                lut=build_transform(state,True,240).red
                reference=pq_eotf(sample(lut,pq_inverse_eotf(203)))
                target=pq_eotf(sample(lut,pq_inverse_eotf(101.5)))
                ratios.append(target/reference)
            with self.subTest(correction=correction):
                self.assertEqual(ratios,sorted(ratios,reverse=True))
                self.assertGreater(ratios[0],.5);self.assertLess(ratios[-1],.5)
        guidance=pattern_by_key("gamma-match").instructions
        self.assertIn("Left lowers Gamma and brightens",guidance)
        self.assertIn("Right raises Gamma and darkens",guidance)

    def test_target_arrow_is_outside_the_patch_and_points_toward_it(self):
        marker=next(m for m in pattern_by_key("gamma-match").markers(HDR) if m.target)
        self.assertGreater(marker.x,.625)

    def test_guided_run_has_a_dedicated_gamma_step_before_tone_tracking(self):
        from vhdr_color.patterns import GUIDED_SEQUENCE
        self.assertEqual(GUIDED_SEQUENCE,("black-level","peak-white","full-frame-white","gamma-match"))


class GammaCorrectionRangeTests(unittest.TestCase):
    def test_low_white_correction_no_longer_leaves_target_outside_the_gamma_range(self):
        from dataclasses import replace
        from vhdr_color.curves import build_transform
        from vhdr_color.greyscale import sample
        from vhdr_color.model import ModeState
        from vhdr_color.patterns import gamma_reference_nits, gamma_target_nits
        for white in (80,100,240):
            context=replace(HDR,gamma_white_nits=white)
            reference=gamma_reference_nits(context)
            self.assertLessEqual(reference,white)
            ratios=[]
            for gamma in (1.6,2.2,3.0):
                state=ModeState.neutral("HDR");state.gamma=gamma;state.sdr_gamma_correction="Auto (Recommended)"
                lut=build_transform(state,True,white).red
                output=lambda n:pq_eotf(sample(lut,pq_inverse_eotf(n)))
                ratios.append(output(gamma_target_nits(context))/output(reference))
            self.assertAlmostEqual(ratios[1], .5, delta=.002)
            self.assertGreater(ratios[0],.5);self.assertLess(ratios[-1],.5)
            self.assertGreater(ratios[0]-ratios[-1],.1)


class BrowserGammaReferenceTests(unittest.TestCase):
    def test_match_encodes_browser_half_output_signal_once(self):
        from dataclasses import replace
        from vhdr_color.patterns import gamma_target_nits, gamma_reference_nits
        from vhdr_color.gamma_correction import srgb_inverse_eotf
        for gamma in (1.0, 1.8, 2.2, 3.0):
            for base in (HDR, PatternContext(is_hdr=False)): 
                context=replace(base,gamma_view="match",reference_gamma=gamma)
                encoded=gamma_target_nits(context)/gamma_reference_nits(context)
                signal=srgb_inverse_eotf(encoded)
                expected=(0.25**gamma+0.75**gamma)/2
                self.assertAlmostEqual(signal,expected**(1/gamma),places=7)
                self.assertAlmostEqual(signal**gamma,expected,places=7)

    def test_reference_slider_changes_target_but_not_checkerboard(self):
        from dataclasses import replace
        frames=[render(pattern_by_key("gamma-match"),400,200,
                       replace(HDR,gamma_view="match",reference_gamma=g)) for g in (1,2.2,3)]
        backgrounds=[pixel_at(f,400,0,0) for f in frames]
        targets=[pixel_at(f,400,200,70)[0] for f in frames]
        self.assertEqual(backgrounds,[backgrounds[0]]*3)
        self.assertEqual(targets,sorted(targets))
        self.assertGreater(targets[-1]-targets[0],0.1)

    def test_matching_reference_is_neutral_midgrey_and_averages_in_both_axes(self):
        from dataclasses import replace
        context=replace(HDR,gamma_view="match")
        frame=render(pattern_by_key("gamma-match"),400,200,context)
        pixels=[pixel_at(frame,400,x,y) for y in range(2) for x in range(2)]
        for px in pixels:self.assertEqual(px[:3],(px[0],)*3)
        self.assertEqual(pixels[0],pixels[3]);self.assertEqual(pixels[1],pixels[2])
        self.assertGreater(min(px[0] for px in pixels),0)
        self.assertLess(max(px[0] for px in pixels),context.encode(context.sdr_white_nits))
        preview=render(pattern_by_key("gamma-match"),400,200,replace(context,gamma_average_preview=True))
        self.assertAlmostEqual(pixel_at(preview,400,0,0)[0],sum(px[0] for px in pixels)/4,delta=.001)
        self.assertEqual(pixel_at(preview,400,0,0),pixel_at(preview,400,1,0))

    def test_custom_slider_moves_only_custom_row(self):
        from dataclasses import replace
        context=replace(HDR,gamma_view="rows")
        a=render(pattern_by_key("gamma-match"),600,500,replace(context,reference_gamma=1.75))
        b=render(pattern_by_key("gamma-match"),600,500,replace(context,reference_gamma=2.65))
        self.assertEqual(a[:600*400*8],b[:600*400*8])
        self.assertNotEqual(a[600*400*8:],b[600*400*8:])

    def test_rows_have_correct_signals_and_identical_endpoints(self):
        from dataclasses import replace
        from vhdr_color.patterns import gamma_reference_nits
        from vhdr_color.gamma_correction import srgb_inverse_eotf
        context=replace(HDR,gamma_view="rows")
        frame=render(pattern_by_key("gamma-match"),600,500,context)
        white=context.encode(gamma_reference_nits(context))
        for row,gamma in enumerate((1.8,2,2.2,2.4,2.2)):
            for col in range(6):
                value=pixel_at(frame,600,col*100+50,row*100+50)[0]/white
                self.assertAlmostEqual(srgb_inverse_eotf(value)**gamma,col/5,delta=.001)


if __name__ == "__main__":
    unittest.main()
