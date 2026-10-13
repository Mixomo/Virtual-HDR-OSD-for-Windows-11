"""The measured greyscale correction: storage, the curves it builds, and the loop.

The closed-loop tests at the bottom are the ones that matter. Every other test here
checks that a piece behaves; those check that the pieces together make a display less
wrong, which is the only claim the feature actually makes.
"""

import math
import tempfile
import unittest
from pathlib import Path

from vhdr_color import curves, greyscale, icc, measure, model
from vhdr_color.gamma_correction import pq_eotf, pq_inverse_eotf
from vhdr_color.greyscale import MAX_CODE_SHIFT, PanelResponse
from sdr_hdr_profile_creator.meter import Reading

PEAK = 1000.0
D65 = (0.3127, 0.3290)

# BT.709 primaries scaled so equal drive makes D65 at PEAK. The signal this app sends
# is scRGB, which is defined on BT.709, so these are the right primaries for a
# correction that acts on that signal -- see measure.py's module docstring.
PRIMARY_XY = {"red": (0.640, 0.330), "green": (0.300, 0.600), "blue": (0.150, 0.060)}
PRIMARY_LUMA = {"red": 0.2126 * PEAK, "green": 0.7152 * PEAK, "blue": 0.0722 * PEAK}
CHANNELS = ("red", "green", "blue")


def primary_xyz(name):
    x, y = PRIMARY_XY[name]
    luminance = PRIMARY_LUMA[name]
    return (x / y * luminance, luminance, (1.0 - x - y) / y * luminance)


PRIMARIES = {name: primary_xyz(name) for name in PRIMARY_XY}


class FakePanel:
    """A display with a transfer function we chose, so the answer is known in advance.

    ``luminance_error`` scales what a code delivers; ``blue_error`` pushes blue on top
    of that. Both take the code so the fault can vary with level, which is the only
    kind a per-level correction can do anything about -- a flat error is what the
    matrix already handles.
    """

    def __init__(self, luminance_error=lambda code: 1.0, blue_error=lambda code: 1.0):
        self.luminance_error = luminance_error
        self.blue_error = blue_error

    def drive(self, name, code):
        code = max(0.0, min(1.0, code))
        ideal = min(pq_eotf(code), PEAK) / PEAK
        value = ideal * self.luminance_error(code)
        if name == "blue":
            value *= self.blue_error(code)
        return max(0.0, min(1.0, value))

    def read(self, codes):
        X = Y = Z = 0.0
        for name, code in zip(CHANNELS, codes):
            amount = self.drive(name, code)
            px, py, pz = PRIMARIES[name]
            X, Y, Z = X + amount * px, Y + amount * py, Z + amount * pz
        total = X + Y + Z
        if total <= 0.0:
            return Reading(0.0, 0.0, 0.0, *D65)
        return Reading(X, Y, Z, X / total, Y / total)

    def measure(self, lut=None, plan_peak=None):
        """Run the real plan through the panel, returning the plan and the readings.

        ``plan_peak`` above the panel's own ceiling is the normal case on hardware: the
        plan asks for what the EDID claims and the panel stops well short, so the top of
        the ramp is a plateau of clipped readings. Defaulting it to PEAK meant the
        fixture planned exactly to its own ceiling and never produced that plateau --
        the one situation the app deliberately creates every run.
        """
        steps = measure.plan(PEAK if plan_peak is None else plan_peak)
        readings = {}
        for step in steps:
            codes = []
            for name, level in zip(CHANNELS, step.rgb):
                wanted = step.nits * level
                code = pq_inverse_eotf(wanted) if wanted > 0.0 else 0.0
                codes.append(code if lut is None else greyscale.sample(lut[name], code))
            readings[step.key] = self.read(codes)
        return steps, readings

    def response(self, lut=None, plan_peak=None):
        steps, readings = self.measure(lut, plan_peak)
        calibration = measure.derive(readings, steps)

        def sent(nits):
            code = pq_inverse_eotf(nits)
            if lut is None:
                return (code, code, code)
            return tuple(greyscale.sample(lut[name], code) for name in CHANNELS)

        return measure.panel_response(calibration, sent)

    def worst_errors(self, lut=None):
        """Worst luminance miss and worst drift from D65, across the ramp."""
        luminance = 0.0
        drift = 0.0
        for target in measure.greyscale_levels(PEAK):
            code = pq_inverse_eotf(target)
            codes = [code if lut is None else greyscale.sample(lut[name], code) for name in CHANNELS]
            reading = self.read(codes)
            if target < PEAK * 0.98:
                luminance = max(luminance, abs(reading.Y - target) / target)
            drift = max(drift, math.dist((reading.x, reading.y), D65))
        return luminance, drift


def build(response):
    """A state carrying ``response``, round-tripped through storage, and its curves."""
    state = model.ModeState.neutral("HDR")
    state.panel_response = greyscale.to_values(response)
    state.panel_response_weights = tuple(response.weights)
    state = model.ModeState.from_dict(state.to_dict(), "HDR")
    transform = curves.build_transform(state, hdr=True)
    return state, {"red": transform.red, "green": transform.green, "blue": transform.blue}


def flat_response(points=20, weights=(0.2126, 0.7152, 0.0722), scale=1.0):
    """A response from a panel that tracks PQ exactly, or ``scale`` times off it."""
    samples = {name: [] for name in CHANNELS}
    for index in range(points):
        code = 0.05 + 0.9 * index / (points - 1)
        for name, weight in zip(CHANNELS, weights):
            samples[name].append((code, pq_eotf(code) * weight * scale))
    return PanelResponse(
        tuple(samples["red"]), tuple(samples["green"]), tuple(samples["blue"]), weights
    )


class ResponseStorageTests(unittest.TestCase):
    def test_a_response_survives_the_round_trip(self):
        original = flat_response()
        restored = greyscale.from_values(
            greyscale.to_values(original), original.weights
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.red, original.red)
        self.assertEqual(restored.blue, original.blue)

    def test_weights_are_normalised_on_the_way_in(self):
        """Stored weights are floats that have been through JSON; they need not sum to 1."""
        response = greyscale.from_values(greyscale.to_values(flat_response()), (2.0, 7.0, 1.0))
        self.assertIsNotNone(response)
        self.assertAlmostEqual(sum(response.weights), 1.0, places=9)

    def test_a_response_without_weights_is_refused(self):
        """Half a pair corrects grey towards nothing, which is worse than not correcting."""
        self.assertIsNone(greyscale.from_values(greyscale.to_values(flat_response()), ()))

    def test_weights_without_a_response_are_refused(self):
        self.assertIsNone(greyscale.from_values((), (0.2126, 0.7152, 0.0722)))

    def test_a_ramp_too_short_to_be_a_curve_is_refused(self):
        short = flat_response(points=greyscale.MIN_RESPONSE_POINTS - 1)
        self.assertIsNone(greyscale.from_values(greyscale.to_values(short), short.weights))

    def test_a_negative_weight_is_refused(self):
        """A channel cannot contribute negative luminance, and dividing by it later
        would turn a bad measurement into a curve that inverts."""
        response = flat_response()
        self.assertIsNone(
            greyscale.from_values(greyscale.to_values(response), (0.5, 0.7, -0.2))
        )

    def test_values_that_are_not_numbers_are_refused(self):
        response = flat_response()
        self.assertIsNone(greyscale.from_values(("wrong",) * 60, response.weights))
        self.assertIsNone(greyscale.from_values(greyscale.to_values(response), ("x", 1, 1)))

    def test_a_non_finite_value_is_refused(self):
        values = list(greyscale.to_values(flat_response()))
        values[7] = float("inf")
        self.assertIsNone(greyscale.from_values(values, (0.2126, 0.7152, 0.0722)))


class SampleTests(unittest.TestCase):
    def test_it_interpolates_between_entries(self):
        """Quantising to the nearest entry is invisible in a gradient and very visible
        in a correction solved from it."""
        curve = [0.0, 0.5, 1.0]
        self.assertAlmostEqual(greyscale.sample(curve, 0.25), 0.25, places=9)

    def test_the_ends_are_exact(self):
        curve = [0.0, 0.5, 1.0]
        self.assertEqual(greyscale.sample(curve, 0.0), 0.0)
        self.assertEqual(greyscale.sample(curve, 1.0), 1.0)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.shaped = [index / 255 for index in range(256)]

    def test_an_unusable_response_leaves_one_common_curve(self):
        """No measurement means the matrix keeps sole charge of colour, as before."""
        broken = PanelResponse((), (), (), (0.2126, 0.7152, 0.0722))
        red, green, blue = greyscale.correct(self.shaped, broken)
        self.assertEqual(red, self.shaped)
        self.assertEqual(red, green)
        self.assertEqual(green, blue)

    def test_a_panel_that_already_tracks_pq_is_barely_touched(self):
        red, green, blue = greyscale.correct(self.shaped, flat_response())
        for curve in (red, green, blue):
            worst = max(abs(a - b) for a, b in zip(curve, self.shaped))
            self.assertLess(worst, 0.01, "a correct display should be left alone")

    def test_an_imbalanced_panel_gets_three_different_curves(self):
        panel = FakePanel(blue_error=lambda code: 1.0 + 0.3 * (code - 0.45))
        _state, lut = build(panel.response())
        self.assertNotEqual(lut["blue"], lut["green"])
        self.assertNotEqual(lut["red"], lut["blue"])

    def test_every_curve_is_non_decreasing_and_pinned(self):
        """A LUT that goes backwards inverts a gradient, and the ends address the ends."""
        panel = FakePanel(
            luminance_error=lambda code: 0.8 + 0.32 * code,
            blue_error=lambda code: 1.0 + 0.3 * (code - 0.45),
        )
        _state, lut = build(panel.response())
        for name, curve in lut.items():
            self.assertEqual(curve[0], 0.0, name)
            self.assertEqual(curve[-1], 1.0, name)
            for lower, higher in zip(curve, curve[1:]):
                self.assertLessEqual(lower, higher, f"{name} goes backwards")

    def test_no_shift_exceeds_the_clamp(self):
        """The clamp is a backstop against a bad measurement, not a working limit."""
        panel = FakePanel(luminance_error=lambda code: 0.35 + 0.9 * code)
        _state, lut = build(panel.response())
        plain = curves.build_transform(model.ModeState.neutral("HDR"), hdr=True).red
        for name, curve in lut.items():
            worst = max(abs(a - b) for a, b in zip(curve, plain))
            self.assertLessEqual(worst, MAX_CODE_SHIFT + 1e-9, name)

    def test_a_measurement_far_beyond_belief_is_clamped_rather_than_obeyed(self):
        """A meter left capped reads a fraction of the light and asks for enormous
        drive. Obeying that would blow the whole range out; the clamp bounds the damage
        to something a user can see is wrong and measure again.

        A panel eight times too dim needs more correction than the clamp allows, so the
        clamp is what decides the answer here -- which is the only way to know it is
        actually in the path.
        """
        red, _green, _blue = greyscale.correct(self.shaped, flat_response(scale=0.125))
        worst = max(abs(a - b) for a, b in zip(red, self.shaped))
        self.assertAlmostEqual(worst, MAX_CODE_SHIFT, places=3)

    def test_the_top_code_still_addresses_the_top_of_the_range(self):
        """A reading far too bright puts the measured peak past the top of PQ, so there
        is no fade region and the last entry gets corrected like any other. Left alone
        it lands at 0.900, and the brightest code the pipeline can send stops reaching
        the brightest the panel can show."""
        red, green, blue = greyscale.correct(self.shaped, flat_response(scale=40.0))
        for curve in (red, green, blue):
            self.assertEqual(curve[-1], 1.0)
            self.assertEqual(curve[0], 0.0)

    def test_the_correction_never_grows_above_the_measured_peak(self):
        """Past the peak the ramp has nothing to say, so the correction is held at what
        it was there and faded out.

        Solving afresh up here instead asks the inverse for a luminance it never saw,
        and the only answer it has is the top code it knows -- a shift that grows with
        every step past the peak and drags the top of the range down to the panel's
        maximum. That is a hard clip replacing whatever roll-off the display does,
        arrived at from the other side, and it is what this test exists to catch.
        """
        panel = FakePanel(luminance_error=lambda code: 0.8 + 0.32 * code)
        response = panel.response()
        _state, lut = build(response)
        plain = curves.build_transform(model.ModeState.neutral("HDR"), hdr=True).red
        peak_code = pq_inverse_eotf(response.measured_peak_nits)
        entries = len(plain)

        above = [
            abs(lut["green"][index] - plain[index])
            for index in range(entries)
            if index / (entries - 1) > peak_code
        ]
        self.assertTrue(above, "the ramp should not reach the top of the code range")
        for earlier, later in zip(above, above[1:]):
            self.assertLessEqual(later, earlier + 1e-9, "the correction grows past peak")
        self.assertAlmostEqual(above[-1], 0.0, places=6)


class BuildTransformTests(unittest.TestCase):
    def test_sdr_keeps_the_common_curve(self):
        """This app never modifies the SDR path, so there is nothing there to correct."""
        panel = FakePanel(blue_error=lambda code: 1.0 + 0.3 * (code - 0.45))
        state, _lut = build(panel.response())
        transform = curves.build_transform(state, hdr=False)
        self.assertEqual(transform.red, transform.green)
        self.assertEqual(transform.green, transform.blue)

    def test_a_state_with_no_response_behaves_as_it_always_did(self):
        transform = curves.build_transform(model.ModeState.neutral("HDR"), hdr=True)
        self.assertEqual(transform.red, transform.green)
        self.assertEqual(transform.green, transform.blue)

    def test_the_controls_still_reach_the_curve_through_a_correction(self):
        """The measurement decides how much drive an intent needs, not what the intent
        is. A slider that stopped doing anything once a meter had been used would be a
        very quiet way to lose the manual controls."""
        panel = FakePanel(luminance_error=lambda code: 0.8 + 0.32 * code)
        state, _lut = build(panel.response())
        neutral = curves.build_transform(state, hdr=True)
        state.gamma = 2.6
        steeper = curves.build_transform(state, hdr=True)
        self.assertNotEqual(neutral.green, steeper.green)


class ProfileRoundTripTests(unittest.TestCase):
    """A profile this app writes has to carry its own calibration back.

    The response is what the curves are rebuilt from. If importing a profile dropped
    it, the sliders would come back and the measurement would not, and the difference
    would only show up as a display that quietly went back to being wrong.
    """

    def test_a_written_profile_reloads_with_its_response_intact(self):
        panel = FakePanel(
            luminance_error=lambda code: 0.8 + 0.32 * code,
            blue_error=lambda code: 1.0 + 0.3 * (code - 0.45),
        )
        state, _lut = build(panel.response())
        transform = curves.build_transform(state, hdr=True)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "measured.icm"
            path.write_bytes(icc.build_profile("HDR", state, transform))
            imported = icc.import_profile(path, "HDR")

        self.assertTrue(imported.exact_state)
        self.assertEqual(imported.state.panel_response, state.panel_response)
        self.assertEqual(
            imported.state.panel_response_weights, state.panel_response_weights
        )

    def test_the_reloaded_profile_rebuilds_the_same_curves(self):
        """Carrying the numbers back is only half of it; they have to mean the same."""
        panel = FakePanel(blue_error=lambda code: 1.0 + 0.3 * (code - 0.45))
        state, _lut = build(panel.response())
        transform = curves.build_transform(state, hdr=True)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "measured.icm"
            path.write_bytes(icc.build_profile("HDR", state, transform))
            imported = icc.import_profile(path, "HDR")

        rebuilt = curves.build_transform(imported.state, hdr=True)
        self.assertEqual(rebuilt.red, transform.red)
        self.assertEqual(rebuilt.blue, transform.blue)


class ClippedRampTests(unittest.TestCase):
    """A ramp planned above what the panel can actually reach.

    Which is what every real run looks like: the plan asks for the peak the EDID claims
    and the panel stops well short, so the top of the ramp is a plateau of clipped
    readings -- 1015 asked for and about 460 delivered on the display this was developed
    against. The fixture could not produce that until FakePanel.measure took a plan_peak,
    because it planned exactly to its own ceiling, so the plateau was the one thing on
    the hot path with no coverage at all.
    """

    def worst_channel_spread(self, response, floor=0.4):
        """How far apart the three corrections get, above ``floor``.

        Above the floor only: the bottom of the range has its own, older, behaviour and
        including it would hide the highlight defect inside a larger shadow number.
        """
        _state, lut = build(response)
        plain = curves.build_transform(model.ModeState.neutral("HDR"), hdr=True).red
        entries = len(plain)
        worst, where = 0.0, 0.0
        for index in range(entries):
            code = index / (entries - 1)
            if code < floor:
                continue
            values = [lut[name][index] - plain[index] for name in CHANNELS]
            spread = max(values) - min(values)
            if spread > worst:
                worst, where = spread, code
        return worst, where

    def test_a_clipped_plateau_does_not_pin_a_drifting_channel_to_the_top_ramp_code(self):
        """The plateau gave the inverse table entries whose code climbed against an
        unchanged level, so code_for handed back the top ramp code for every level at or
        above the clip. A channel drifting when it clipped kept that drift as one large
        fixed shift across the whole highlight range: measured at +0.0697 for blue
        against +0.0084 for red and green, a spread of 0.061 where everything below the
        clip agreed to 0.0015. That is a visible tint in highlights, from a panel whose
        only fault was being dimmer than its EDID claims.
        """
        panel = FakePanel(blue_error=lambda code: 1.0 - 0.06 * code)
        worst, where = self.worst_channel_spread(panel.response(plan_peak=PEAK * 2))
        self.assertLess(
            worst, 0.005,
            f"the three channels disagree by {worst:.5f} at code {where:.4f}",
        )

    def test_the_same_panel_measured_inside_its_range_is_unaffected(self):
        """The control. Without it the test above would still pass if the correction
        stopped doing anything at all."""
        panel = FakePanel(blue_error=lambda code: 1.0 - 0.06 * code)
        worst, _where = self.worst_channel_spread(panel.response())
        self.assertLess(worst, 0.005)

    def test_the_trimmed_response_still_has_enough_points_to_be_usable(self):
        """Dropping the plateau costs entries, and below MIN_RESPONSE_POINTS correct()
        silently falls back to one common curve."""
        panel = FakePanel(blue_error=lambda code: 1.0 - 0.06 * code)
        response = panel.response(plan_peak=PEAK * 2)
        self.assertTrue(response.usable)
        _state, lut = build(response)
        # With blue misbehaving, a correction that survived gives blue its own curve.
        # Equal curves would mean correct() bailed out and returned the intent unchanged.
        self.assertNotEqual(
            lut["red"], lut["blue"], "the correction fell back to one common curve"
        )


class ClosedLoopTests(unittest.TestCase):
    """The claim the feature makes: measure a wrong display, and it becomes less wrong."""

    def setUp(self):
        self.panel = FakePanel(
            # Dim in the shadows, slightly hot near the top: a very ordinary EOTF miss.
            luminance_error=lambda code: 0.8 + 0.32 * code,
            blue_error=lambda code: 1.0 + 0.3 * (code - 0.45),
        )

    def test_the_measured_correction_reduces_the_error(self):
        before_luminance, before_drift = self.panel.worst_errors()
        _state, lut = build(self.panel.response())
        after_luminance, after_drift = self.panel.worst_errors(lut)

        self.assertGreater(before_luminance, 0.10, "the fake panel should start wrong")
        self.assertLess(after_luminance, 0.01)
        self.assertLess(after_drift, before_drift)

    def test_grey_is_held_to_the_reference_white_not_to_d65(self):
        """Absolute white balance belongs to the MHC2 matrix. If the LUT chased D65 as
        well the two would fight, and a second pass would undo the first."""
        steps, readings = self.panel.measure()
        reference = readings["balance-white"]
        offset = math.dist((reference.x, reference.y), D65)

        _state, lut = build(self.panel.response())
        _luminance, drift = self.panel.worst_errors(lut)
        self.assertAlmostEqual(drift, offset, places=3)

    def test_a_second_pass_refines_rather_than_compounds(self):
        """Every pass measures the display as currently corrected. A correction derived
        from that and then stacked on the one already in force would double it, and the
        display would walk away from the target instead of settling on it."""
        _state, first = build(self.panel.response())
        first_luminance, first_drift = self.panel.worst_errors(first)

        _state, second = build(self.panel.response(first))
        second_luminance, second_drift = self.panel.worst_errors(second)

        self.assertLessEqual(second_luminance, max(first_luminance, 0.01) + 1e-9)
        self.assertLessEqual(second_drift, first_drift + 1e-4)

    def test_it_settles_rather_than_oscillating(self):
        lut = None
        errors = []
        for _pass in range(4):
            _state, lut = build(self.panel.response(lut))
            errors.append(self.panel.worst_errors(lut))
        for luminance, _drift in errors:
            self.assertLess(luminance, 0.01)
        # The last two passes should be indistinguishable; a loop that keeps moving is
        # not converging, it is hunting.
        self.assertAlmostEqual(errors[-1][1], errors[-2][1], places=4)


if __name__ == "__main__":
    unittest.main()
