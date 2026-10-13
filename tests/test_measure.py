"""Turning meter readings into profile values, and refusing when they are wrong.

The rejection rules carry more weight than the arithmetic. A meter that is
unplugged, aimed at the wrong part of the screen, or reading through a closed
diffuser still returns numbers, and those reach the profile with nothing
downstream able to tell them from real measurements.
"""

from __future__ import annotations

import struct
import unittest

from vhdr_color.measure import (
    estimated_seconds,
    greyscale_levels,
    sees_placement_target,
    D65_XY,
    VERIFIED_DELTA_UV,
    compose_gains,
    correlated_colour_temperature,
    delta_uv,
    MAX_CHANNEL_TRIM,
    Aborted,
    Calibration,
    GreyPoint,
    MAX_RAMP_REVERSAL,
    SUSTAINED_MAX_READS,
    SUSTAINED_SETTLE_SECONDS,
    SUSTAINED_INTERVAL_SECONDS,
    sustained,
    PEAK_WINDOW_FRACTION,
    WINDOW_AREA_FRACTION,
    _additivity_error,
    _ramp_reversal,
    balance_problems,
    channel_contributions,
    MeasurementError,
    derive,
    panel_response,
    plan,
    run,
    validate,
    white_balance_gains,
)
from vhdr_color.gamma_correction import pq_inverse_eotf
from sdr_hdr_profile_creator.meter import MeterError, Reading
from vhdr_color.patterns import PatternContext, measurement_frame


def reading(Y: float, x: float, y: float) -> Reading:
    """A reading with a plausible XYZ for the given Y and chromaticity."""
    if y <= 0.0:
        return Reading(X=0.0, Y=Y, Z=0.0, x=x, y=y)
    return Reading(X=(x / y) * Y, Y=Y, Z=((1.0 - x - y) / y) * Y, x=x, y=y)


def xy_of(X: float, Y: float, Z: float) -> tuple[float, float]:
    total = X + Y + Z
    return (X / total, Y / total)


def combine(channels: dict[str, Reading]) -> Reading:
    """The white three channels add up to, so a set is additive by construction."""
    X = sum(channels[c].X for c in ("red", "green", "blue"))
    Y = sum(channels[c].Y for c in ("red", "green", "blue"))
    Z = sum(channels[c].Z for c in ("red", "green", "blue"))
    x, y = xy_of(X, Y, Z)
    return Reading(X=X, Y=Y, Z=Z, x=x, y=y)


PEAK = 454.252093
BALANCE = 100.0

# The balance patches are shown well below peak so the panel is linear there, and
# peak white is dimmed by the limiter relative to the sum of the channels -- which
# is exactly the situation that made an earlier version refuse every real run.
DIMMED_PEAK = reading(PEAK, 0.3270, 0.3295)

# BT.709 channels weighted so they sum to exactly D65 -- a display needing no
# correction at all.
NEUTRAL_CHANNELS = {
    "red": reading(0.2126 * BALANCE, 0.640, 0.330),
    "green": reading(0.7152 * BALANCE, 0.300, 0.600),
    "blue": reading(0.0722 * BALANCE, 0.150, 0.060),
}
NEUTRAL = dict(
    NEUTRAL_CHANNELS,
    black=reading(0.0, 0.3130, 0.3290),
    white=DIMMED_PEAK,
    **{
        "balance-white": combine(NEUTRAL_CHANNELS),
        # The same drive on the window the rest of the run uses. Lower than peak,
        # because a brightness limiter responds to total output.
        "window-white": reading(PEAK * 0.45, 0.3130, 0.3290),
    },
)

# The chromaticities actually measured on the development panel, with red raised
# so white lands warm, as it did there.
WARM_CHANNELS = {
    "red": reading(0.2126 * BALANCE * 1.13, 0.6486, 0.3312),
    "green": reading(0.7152 * BALANCE * 0.99, 0.3141, 0.5892),
    "blue": reading(0.0722 * BALANCE * 0.97, 0.1524, 0.0596),
}
WARM = dict(
    WARM_CHANNELS,
    black=reading(0.0, 0.3130, 0.3290),
    white=DIMMED_PEAK,
    **{"balance-white": combine(WARM_CHANNELS)},
)


def changed(base=None, **replacements) -> dict[str, Reading]:
    updated = dict(NEUTRAL if base is None else base)
    updated.update(replacements)
    return updated


def without(key: str) -> dict[str, Reading]:
    updated = dict(NEUTRAL)
    updated.pop(key, None)
    return updated


class PlanTests(unittest.TestCase):
    CORE = ["black", "white", "window-white", "balance-white", "red", "green", "blue"]

    def test_measures_black_peak_and_a_balance_set(self):
        """The short plan is exactly the six patches the profile is built from."""
        self.assertEqual([step.key for step in plan(1000.0, full=False)], self.CORE)

    def test_black_is_measured_first_while_the_panel_is_still_cool(self):
        """A long bright sequence warms an emissive panel, and the black floor
        is the reading most disturbed by that."""
        self.assertEqual(plan(1000.0)[0].key, "black")

    def test_black_is_given_longer_to_settle(self):
        steps = {step.key: step for step in plan(1000.0)}
        self.assertGreater(steps["black"].settle_seconds, steps["white"].settle_seconds)

    def test_channels_are_driven_at_the_same_level_as_the_reference_white(self):
        """A channel measured at another level samples a different point on the
        display's response and cannot be combined with the others."""
        steps = {step.key: step for step in plan(1000.0)}
        for key in ("red", "green", "blue"):
            self.assertEqual(steps[key].nits, steps["balance-white"].nits)

    def test_the_balance_set_sits_well_below_peak(self):
        """White at peak asks for about three times the power of one channel, so
        the limiter dims it much harder and the channels no longer add up to it.
        An earlier version measured balance at peak and refused every real run."""
        steps = {step.key: step for step in plan(1000.0)}
        self.assertLess(steps["balance-white"].nits, steps["white"].nits / 2.0)

    def test_a_dim_display_is_not_asked_for_balance_above_half_its_peak(self):
        steps = {step.key: step for step in plan(120.0)}
        self.assertLessEqual(steps["balance-white"].nits, steps["white"].nits / 2.0)

    def test_an_absurd_peak_is_clamped_into_range(self):
        self.assertLessEqual(plan(99000.0)[1].nits, 10000.0)
        self.assertGreaterEqual(plan(0.0)[1].nits, 80.0)


class WhiteBalanceTests(unittest.TestCase):
    """The gains that move measured white onto D65."""

    def corrected_white(self, readings, gains):
        axes = []
        for axis in ("X", "Y", "Z"):
            axes.append(sum(
                gain * getattr(readings[channel], axis)
                for gain, channel in zip(gains, ("red", "green", "blue"))
            ))
        return xy_of(*axes), axes[1]

    def test_a_neutral_display_needs_no_correction(self):
        gains = white_balance_gains(NEUTRAL)
        for gain in gains:
            self.assertAlmostEqual(gain, 1.0, places=3)

    def test_a_warm_display_has_its_red_pulled_down(self):
        red, green, blue = white_balance_gains(WARM)
        self.assertLess(red, 0.95)
        self.assertLessEqual(max(red, green, blue), 1.0 + 1e-9)

    def test_the_correction_actually_lands_on_d65(self):
        """The whole point. Anything else is a plausible-looking number."""
        gains = white_balance_gains(WARM)
        (x, y), _ = self.corrected_white(WARM, gains)
        self.assertAlmostEqual(x, D65_XY[0], places=4)
        self.assertAlmostEqual(y, D65_XY[1], places=4)

    def test_nothing_is_ever_boosted_above_unity(self):
        """A display cannot be asked for light it has already run out of, so the
        excess channels come down to meet the weakest."""
        for readings in (NEUTRAL, WARM):
            self.assertLessEqual(max(white_balance_gains(readings)), 1.0 + 1e-9)

    def test_correcting_white_costs_luminance_rather_than_clipping(self):
        gains = white_balance_gains(WARM)
        _, corrected_Y = self.corrected_white(WARM, gains)
        self.assertLess(corrected_Y, WARM["white"].Y)

    def test_degenerate_channels_give_a_neutral_answer_rather_than_raising(self):
        same = reading(150.0, 0.3127, 0.3290)
        readings = changed(red=same, green=same, blue=same)
        self.assertEqual(white_balance_gains(readings), (1.0, 1.0, 1.0))


class DeriveTests(unittest.TestCase):
    def test_reports_peak_and_black(self):
        result = derive(NEUTRAL)
        self.assertIsInstance(result, Calibration)
        self.assertAlmostEqual(result.peak_nits, PEAK, places=2)
        self.assertEqual(result.black_nits, 0.0)

    def test_peak_comes_from_the_full_drive_patch_not_the_balance_one(self):
        """They are deliberately different levels, and mixing them up would
        report the balance level as the display's peak."""
        self.assertAlmostEqual(derive(NEUTRAL).peak_nits, PEAK, places=2)
        self.assertGreater(derive(NEUTRAL).peak_nits, BALANCE * 2)

    def test_the_white_point_comes_from_the_patch_the_channels_sat_beside(self):
        """That is the white the correction is solved against."""
        result = derive(WARM)
        self.assertAlmostEqual(result.white_xy[0], WARM["balance-white"].x, places=6)

    def test_records_the_window_the_peak_was_measured_on(self):
        """Peak luminance is meaningless without it. The development panel is
        rated 1015 nits but reads 454 on a tenth of the screen, because the
        brightness limiter responds to total output."""
        self.assertAlmostEqual(derive(NEUTRAL).window_fraction, 0.10, places=3)

    def test_an_unmeasurably_low_black_gives_infinite_contrast(self):
        """An OLED reads true black as 0.0000 on this class of instrument. That
        is a floor rather than a value, and must not divide by zero."""
        self.assertEqual(derive(NEUTRAL).contrast, float("inf"))

    def test_contrast_is_a_ratio_when_black_is_measurable(self):
        result = derive(changed(black=reading(0.05, 0.313, 0.329)))
        self.assertAlmostEqual(result.contrast, PEAK / 0.05, places=0)

    def test_white_error_is_reported_against_d65(self):
        dx, dy = derive(WARM).white_error
        self.assertGreater(dx, 0.0)
        # Against the reference white the channels sat beside, not peak white,
        # because that is the white the correction is solved against.
        self.assertAlmostEqual(dx, WARM["balance-white"].x - D65_XY[0], places=6)
        self.assertAlmostEqual(dy, WARM["balance-white"].y - D65_XY[1], places=6)

    def test_trims_are_the_gains_as_percentages_and_never_positive(self):
        trims = derive(WARM).channel_trims
        self.assertEqual(len(trims), 3)
        for trim in trims:
            self.assertLessEqual(trim, 0.0)

    def test_a_neutral_display_needs_no_trim(self):
        for trim in derive(NEUTRAL).channel_trims:
            self.assertAlmostEqual(trim, 0.0, places=1)

    def test_an_oversized_correction_is_flagged(self):
        """A profile can only carry so much, and clamping it silently would
        leave white visibly off with nothing said."""
        extreme = dict(WARM_CHANNELS)
        extreme["red"] = reading(0.2126 * BALANCE * 3.0, 0.6486, 0.3312)
        readings = dict(
            extreme,
            black=reading(0.0, 0.313, 0.329),
            white=DIMMED_PEAK,
            **{"balance-white": combine(extreme)},
        )
        self.assertTrue(derive(readings).trims_exceed_range)

    def test_a_modest_correction_is_not_flagged(self):
        self.assertFalse(derive(NEUTRAL).trims_exceed_range)
        self.assertLessEqual(MAX_CHANNEL_TRIM, 1.0)

    def test_refuses_rather_than_returning_something_wrong(self):
        with self.assertRaises(MeasurementError):
            derive(without("white"))


class GamutIsNotMeasuredTests(unittest.TestCase):
    """Why these readings never reach the profile's colorant tags.

    On a QD-OLED whose EDID declares green at (0.2698, 0.6859), the green patch
    read (0.3141, 0.5892) -- 0.0141 from BT.709 green and 0.0967 from the declared
    one. The patch asked for BT.709 green and the display rendered it, so the
    reading describes the request, not the panel.

    That is this app's limit, not scRGB's. scRGB reaches wider colours through
    negative components, and on that panel Windows passed them through: BT.2020
    green sent that way read (0.2524, 0.6983). What keeps every patch inside BT.709
    is measurement_frame clamping each channel at zero.
    """

    def test_every_patch_is_clamped_inside_bt709(self):
        """This replaces a test that compared three recorded chromaticities with one
        another, which passed whatever the code did. This one checks the mechanism."""
        width, height = 40, 20
        frame = measurement_frame(
            width, height, (1.0, -0.1, -0.05), 80.0, PatternContext(is_hdr=True)
        )
        centre = struct.unpack_from("<4e", frame, ((height // 2) * width + width // 2) * 8)
        self.assertEqual((1.0, 0.0, 0.0), centre[:3])

    def test_a_calibration_offers_no_primaries_to_write(self):
        self.assertFalse(hasattr(derive(NEUTRAL), "primaries"))


class ValidationTests(unittest.TestCase):
    def test_a_good_set_has_no_complaints(self):
        self.assertEqual(validate(NEUTRAL), [])
        self.assertEqual(validate(WARM), [])

    def test_missing_readings_are_named(self):
        self.assertTrue(any("white" in problem for problem in validate(without("white"))))

    def test_rejects_a_peak_no_display_could_produce(self):
        """A meter off the screen entirely reads room light, not the panel."""
        problems = validate(changed(white=reading(3.0, 0.3127, 0.3290)))
        self.assertTrue(any("outside anything a display" in p for p in problems))

    def test_rejects_a_black_that_is_really_room_light(self):
        problems = validate(changed(black=reading(80.0, 0.31, 0.33)))
        self.assertTrue(any("room light" in p for p in problems))

    def test_rejects_white_no_brighter_than_black(self):
        problems = validate(
            changed(white=reading(50.0, 0.3127, 0.3290), black=reading(60.0, 0.31, 0.33))
        )
        self.assertTrue(any("no brighter than black" in p for p in problems))

    def test_rejects_an_impossible_chromaticity(self):
        problems = validate(changed(red=reading(150.0, 1.4, 0.31)))
        self.assertTrue(any("impossible chromaticity" in p for p in problems))

    def test_a_true_black_of_zero_is_accepted(self):
        """It is what an OLED actually reads, and rejecting it would refuse the
        best display this could be pointed at."""
        self.assertEqual(validate(changed(black=reading(0.0, 0.31, 0.33))), [])

    def test_additivity_is_not_a_reason_to_throw_the_run_away(self):
        """Peak, black and the ramp are read directly or derived from ratios. Refusing
        all of them because the channels do not add up discards four minutes of good
        measurements to avoid one bad correction."""
        broken = reading(BALANCE * 1.6, 0.3127, 0.3290)
        self.assertEqual(validate(changed(**{"balance-white": broken})), [])


class BalanceProblemTests(unittest.TestCase):
    """What stops a white balance being solved, as distinct from what ruins a run."""

    def test_a_good_set_can_be_solved(self):
        self.assertEqual(balance_problems(NEUTRAL), [])
        self.assertEqual(balance_problems(WARM), [])

    def test_channels_that_do_not_add_up_can_still_be_solved(self):
        """Magnitudes come from the white measured beside the primaries, so the
        primaries' own luminances being wrong no longer stops anything."""
        broken = reading(BALANCE * 1.6, 0.3127, 0.3290)
        self.assertEqual(balance_problems(changed(**{"balance-white": broken})), [])

    def test_a_white_outside_its_own_primaries_cannot_be_solved(self):
        """No combination of three primaries produces a colour outside the triangle
        they make, so a white that sits outside one was misread."""
        outside = reading(BALANCE, 0.7, 0.25)
        problems = balance_problems(changed(**{"balance-white": outside}))
        self.assertTrue(any("outside the triangle" in p for p in problems), problems)

    def test_channels_that_are_the_same_colour_cannot_be_solved(self):
        same = NEUTRAL["balance-white"]
        problems = balance_problems(changed(red=same, green=same, blue=same))
        self.assertTrue(any("no white balance to solve" in p for p in problems), problems)

    def test_an_incomplete_set_is_left_to_validate(self):
        """Missing readings are a problem with the run, and saying so twice in two
        different vocabularies helps nobody."""
        self.assertEqual(balance_problems(without("red")), [])


class SustainedTests(unittest.TestCase):
    """Full screen, read until it stops falling, with an end even if it never does."""

    def setUp(self):
        self.display = FakeDisplay()
        self.slept = []

    def run_sustained(self, values, **kwargs):
        order = iter(values)
        return sustained(
            self.display, lambda: reading(next(order), 0.3127, 0.3290),
            peak_nits=1000.0, sleep=self.slept.append, **kwargs
        )

    def test_it_lights_the_whole_screen(self):
        """Sustained is what the display holds with everything lit. Nothing smaller
        answers the question."""
        self.run_sustained([250.0, 249.0])
        self.assertEqual(self.display.shown, ["sustained", "sustained"])
        self.assertEqual(self.display.steps[0].window_fraction, 1.0)

    def test_it_stops_once_two_readings_agree(self):
        result = self.run_sustained([300.0, 260.0, 250.0, 249.0, 1.0, 1.0])
        self.assertTrue(result.settled)
        self.assertAlmostEqual(result.nits, 249.0)
        self.assertEqual(result.readings, (300.0, 260.0, 250.0, 249.0))

    def test_a_panel_that_never_settles_still_ends(self):
        """The one patch that lights every pixel needs an end even when the readings do
        not give it one."""
        falling = [500.0 * (0.5 ** n) for n in range(SUSTAINED_MAX_READS + 4)]
        result = self.run_sustained(falling)
        self.assertFalse(result.settled)
        self.assertEqual(len(result.readings), SUSTAINED_MAX_READS)

    def test_it_reports_how_far_it_fell(self):
        result = self.run_sustained([400.0, 300.0, 250.0, 249.0])
        self.assertAlmostEqual(result.fell_by, (400.0 - 249.0) / 400.0, places=6)

    def test_a_steady_panel_settles_on_the_second_reading(self):
        result = self.run_sustained([243.0, 243.0])
        self.assertTrue(result.settled)
        self.assertEqual(len(result.readings), 2)

    def test_the_first_wait_is_longer_than_the_rest(self):
        """The limiter engages while the patch is coming up, so the first reading needs
        longer than the ones checking whether it has stopped."""
        self.run_sustained([300.0, 260.0, 259.0])
        self.assertEqual(self.slept[0], SUSTAINED_SETTLE_SECONDS)
        self.assertTrue(all(s == SUSTAINED_INTERVAL_SECONDS for s in self.slept[1:]))

    def test_abort_stops_it(self):
        with self.assertRaises(Aborted):
            self.run_sustained([300.0, 260.0], should_abort=lambda: True)

    def test_a_read_stopped_by_the_cancel_is_a_cancel(self):
        cancelled = {"now": False}

        def reader():
            cancelled["now"] = True
            raise MeterError("Stopped before the meter answered.")

        with self.assertRaises(Aborted):
            sustained(self.display, reader, peak_nits=1000.0, sleep=self.slept.append,
                      should_abort=lambda: cancelled["now"])

    def test_a_meter_failure_is_reported_not_swallowed(self):
        def bad():
            raise MeterError("diffuser closed")
        with self.assertRaises(MeasurementError):
            sustained(self.display, bad, peak_nits=1000.0, sleep=self.slept.append)


class PeakWindowTests(unittest.TestCase):
    """Peak is measured on a smaller window than everything else, on purpose.

    An emissive panel's limiter responds to total output, so the largest number it
    reaches is one it only has to hold over a few percent of the screen -- which is what
    the EDID reports and what a manufacturer means by a peak figure. Measuring it on the
    window the rest of the run uses answers a different question and reads far lower.
    """

    def steps(self):
        return {step.key: step for step in plan(1000.0)}

    def test_peak_uses_a_smaller_window_than_the_rest(self):
        steps = self.steps()
        self.assertEqual(steps["white"].window_fraction, PEAK_WINDOW_FRACTION)
        self.assertLess(steps["white"].window_fraction, WINDOW_AREA_FRACTION)

    def test_every_other_patch_uses_the_common_window(self):
        for key, step in self.steps().items():
            if key == "white":
                continue
            self.assertEqual(step.window_fraction, WINDOW_AREA_FRACTION, key)

    def test_the_same_drive_is_measured_on_both_windows(self):
        """Otherwise the two numbers differ for two reasons at once and neither is
        attributable to the limiter."""
        steps = self.steps()
        self.assertEqual(steps["window-white"].nits, steps["white"].nits)
        self.assertEqual(steps["window-white"].rgb, steps["white"].rgb)

    def test_both_peaks_are_carried_through(self):
        result = derive(NEUTRAL)
        self.assertAlmostEqual(result.peak_nits, PEAK, places=3)
        self.assertAlmostEqual(result.window_peak_nits, PEAK * 0.45, places=3)
        self.assertEqual(result.peak_window_fraction, PEAK_WINDOW_FRACTION)

    def test_a_run_without_the_window_patch_still_derives(self):
        """Readings from before this existed, and any short run that skips it. Zero says
        it was not measured rather than that the display produced nothing."""
        result = derive(without("window-white"))
        self.assertAlmostEqual(result.peak_nits, PEAK, places=3)
        self.assertEqual(result.window_peak_nits, 0.0)


class RampReversalTests(unittest.TestCase):
    """A display that gets dimmer when asked for more light cannot be corrected.

    A 1-D LUT inverts the transfer function, and a function that is not monotonic has no
    inverse. The inverse built here forces its table non-decreasing, so a reversal does
    not crash anything -- it silently flattens, and the curve comes out wrong across
    exactly the range the reversal ruined. That is the failure this refuses.
    """

    def ramp(self, pairs):
        return tuple(
            GreyPoint(index=i, target_nits=t, measured_nits=m, x=0.3127, y=0.3290)
            for i, (t, m) in enumerate(pairs)
        )

    def test_a_climbing_ramp_reverses_by_nothing(self):
        points = self.ramp([(1.0, 1.1), (10.0, 10.4), (100.0, 104.0), (400.0, 402.0)])
        self.assertEqual(_ramp_reversal(points), 0.0)

    def test_the_pg32ucdm_reversal_is_measured(self):
        """The real readings: asked for 47.5 nits it emitted 106.3, asked for 58.5 it
        emitted 61.5. Switching the monitor to DisplayHDR True Black 400 removed it."""
        points = self.ramp([(38.44, 85.36), (47.53, 106.28), (58.52, 61.55), (71.80, 75.06)])
        self.assertAlmostEqual(_ramp_reversal(points), (106.28 - 61.55) / 106.28, places=6)

    def test_a_dip_that_recovers_is_not_a_cliff(self):
        """The real numbers from a good preset: one reading of 257 between neighbours of
        307 and 359. Sixteen percent down and back above the previous high immediately.
        Blocking a four-minute run over one noisy patch is the failure this avoids."""
        points = self.ramp([(279.49, 306.879), (336.93, 257.392), (405.71, 359.057),
                            (488.04, 462.720)])
        self.assertEqual(_ramp_reversal(points), 0.0)

    def test_a_sustained_fall_is_still_a_cliff(self):
        """The real numbers from a preset that could not be corrected: down from 106 to
        60, and still under 106 five points later. That has no inverse."""
        points = self.ramp([(38.44, 85.360), (47.53, 106.284), (58.52, 61.545),
                            (71.80, 75.055), (87.81, 91.792), (107.09, 112.849)])
        self.assertAlmostEqual(_ramp_reversal(points), (106.284 - 61.545) / 106.284, places=6)

    def test_recovery_must_be_above_where_it_fell_from(self):
        """Climbing again is not recovering. A ramp that dips and then rises without
        passing its earlier high still has two codes for the same luminance."""
        points = self.ramp([(10.0, 100.0), (20.0, 60.0), (30.0, 70.0), (40.0, 80.0)])
        self.assertGreater(_ramp_reversal(points), 0.3)

    def test_noise_in_the_deep_shadows_does_not_count(self):
        """The ramp floor is half a nit, where the instrument's own noise is a large
        fraction of the reading. A step backwards there says nothing about the display."""
        points = self.ramp([(0.50, 0.40), (0.78, 0.20), (10.0, 10.4), (100.0, 104.0)])
        self.assertEqual(_ramp_reversal(points), 0.0)

    def test_a_reversed_ramp_yields_no_correction(self):
        readings = dict(NEUTRAL)
        steps = plan(1000.0)
        for step in steps:
            if step.key.startswith("grey-"):
                # Climbs to the halfway point, then falls back and climbs again.
                measured = step.nits * (2.2 if step.nits < 50.0 else 1.05)
                readings[step.key] = reading(measured, 0.3127, 0.3290)
        result = derive(readings, steps)
        self.assertGreater(result.ramp_reversal, MAX_RAMP_REVERSAL)
        self.assertIsNone(panel_response(result, lambda nits: (pq_inverse_eotf(nits),) * 3))

    def test_the_same_ramp_climbing_does_yield_one(self):
        """The guard must not swallow the ordinary case it was carved out of."""
        readings = dict(NEUTRAL)
        steps = plan(1000.0)
        for step in steps:
            readings[step.key] = readings.get(step.key) or reading(
                step.nits * 1.08, 0.3127, 0.3290
            )
        result = derive(readings, steps)
        self.assertEqual(result.ramp_reversal, 0.0)
        self.assertIsNotNone(panel_response(result, lambda nits: (pq_inverse_eotf(nits),) * 3))


class SolvedDespiteAdditivityTests(unittest.TestCase):
    """The PG32UCDM, exactly as logged on 2026-08-22.

    Its primaries read 2.30x, 2.26x and 2.04x their share of the white beside them, so
    the three sum to 2.11x it. Every earlier build refused this outright. The numbers
    below are the measurement, not a model of it, and the correction they now yield is
    within half a percent of what the same display returned on the runs that happened to
    satisfy the old check -- which is the only independent check available that the
    answer is right rather than merely produced.
    """

    def readings(self):
        return {
            "black": reading(0.0001, 0.3333, 0.3333),
            "white": reading(450.0045, 0.3273, 0.3293),
            "balance-white": reading(106.5509, 0.3289, 0.3285),
            "red": reading(48.8282, 0.6629, 0.3275),
            "green": reading(161.5608, 0.3156, 0.5991),
            "blue": reading(14.7438, 0.1476, 0.0562),
        }

    def test_the_readings_really_do_not_add_up(self):
        """If this ever stops being true the test below is proving nothing."""
        self.assertGreater(_additivity_error(self.readings()), 1.0)

    def test_it_is_no_longer_refused(self):
        self.assertEqual(balance_problems(self.readings()), [])
        self.assertEqual(derive(self.readings()).balance_refused, ())

    def test_the_contributions_add_up_to_the_white_by_construction(self):
        contributions, _matrix = channel_contributions(self.readings())
        self.assertAlmostEqual(sum(contributions), 106.5509, places=3)

    def test_the_contributions_are_close_to_bt709_shares(self):
        """The patches ask for BT.709 primaries, so their shares of white should land
        near BT.709's coefficients however badly the panel reads them."""
        contributions, _matrix = channel_contributions(self.readings())
        total = sum(contributions)
        for solved, expected in zip(contributions, (0.2126, 0.7152, 0.0722)):
            self.assertAlmostEqual(solved / total, expected, delta=0.015)

    def test_the_correction_matches_what_the_display_needed(self):
        """R -21%, G and B near zero: what this display returned on 2026-08-19 from the
        two runs that did add up."""
        red, green, blue = derive(self.readings()).channel_trims
        self.assertAlmostEqual(red, -20.9, delta=0.6)
        self.assertAlmostEqual(green, 0.0, delta=1.7)
        self.assertAlmostEqual(blue, -0.4, delta=0.6)

    def test_the_departure_is_still_reported(self):
        """Solving through it is not the same as pretending it did not happen."""
        self.assertGreater(derive(self.readings()).additivity_error, 1.0)

    def test_the_weights_are_the_same_contributions_the_gains_came_from(self):
        """Both halves of the correction have to describe one display. Solving the
        matrix against one white and holding grey to another is two calibrations of two
        different displays, applied to the same one."""
        contributions, _matrix = channel_contributions(self.readings())
        total = sum(contributions)
        expected = tuple(value / total for value in contributions)
        for solved, want in zip(derive(self.readings()).white_weights, expected):
            self.assertAlmostEqual(solved, want, places=12)

    def test_the_weights_sum_to_one_and_sit_near_bt709(self):
        weights = derive(self.readings()).white_weights
        self.assertAlmostEqual(sum(weights), 1.0, places=9)
        for solved, expected in zip(weights, (0.2126, 0.7152, 0.0722)):
            self.assertAlmostEqual(solved, expected, delta=0.015)


class RefusedBalanceTests(unittest.TestCase):
    """A run whose channels cannot be told apart still measured a display."""

    def broken(self):
        same = NEUTRAL["balance-white"]
        return changed(red=same, green=same, blue=same)

    def test_the_rest_of_the_measurement_survives(self):
        result = derive(self.broken())
        self.assertAlmostEqual(result.peak_nits, PEAK, places=2)
        self.assertGreaterEqual(result.black_nits, 0.0)

    def test_no_correction_is_invented(self):
        for gain in derive(self.broken()).channel_gains:
            self.assertAlmostEqual(gain, 1.0, places=9)

    def test_it_says_why_rather_than_looking_neutral(self):
        """(1, 1, 1) is also what a display that needs no correction measures. The two
        must not be indistinguishable to the caller -- one is a finished calibration and
        the other is a refusal."""
        result = derive(self.broken())
        self.assertTrue(result.balance_refused)
        self.assertTrue(any("no white balance to solve" in p for p in result.balance_refused))

    def test_a_good_run_refuses_nothing(self):
        self.assertEqual(derive(NEUTRAL).balance_refused, ())

    def test_a_warm_display_is_still_corrected(self):
        """The refusal must not swallow the ordinary case it was carved out of."""
        result = derive(WARM)
        self.assertEqual(result.balance_refused, ())
        self.assertNotEqual(result.channel_gains, (1.0, 1.0, 1.0))

    def test_a_peak_white_the_limiter_dimmed_is_not_an_additivity_failure(self):
        """This is the whole reason the balance set exists. Peak white is
        expected to fall well short of the channel sum."""
        self.assertEqual(validate(NEUTRAL), [])
        self.assertLess(NEUTRAL["balance-white"].Y, NEUTRAL["white"].Y)


class FakeDisplay:
    def __init__(self):
        self.shown = []
        # The steps themselves as well as their keys: the window a patch is shown at is
        # part of what was asked for, and a key cannot carry it.
        self.steps = []

    def show(self, step):
        self.shown.append(step.key)
        self.steps.append(step)


class RunTests(unittest.TestCase):
    """Sequencing, with both the screen and the instrument injected."""

    ORDER = ("black", "white", "window-white", "balance-white", "red", "green", "blue")

    def setUp(self):
        self.display = FakeDisplay()
        self.slept = []

    def run_sequence(self, reader, **kwargs):
        # The six-patch plan: these tests are about sequencing -- order, settling,
        # progress, what a bad reading does -- and none of that changes with sixty-three
        # more patches of the same two kinds. PlanTests owns what the long sweep contains.
        kwargs.setdefault("full", False)
        return run(
            self.display, reader, peak_nits=1015.24, sleep=self.slept.append, **kwargs
        )

    def good_reader(self):
        order = iter([NEUTRAL[key] for key in self.ORDER])
        return lambda: next(order)

    def test_shows_every_patch_in_plan_order(self):
        self.run_sequence(self.good_reader())
        self.assertEqual(self.display.shown, list(self.ORDER))

    def test_returns_a_calibration_from_the_readings(self):
        self.assertAlmostEqual(self.run_sequence(self.good_reader()).peak_nits, PEAK, places=2)

    def test_lets_the_panel_settle_before_each_reading(self):
        """A patch read the instant it appears is read mid-transition.

        Counting the sleeps does not establish that any of them happened before
        a reading -- moving the sleep to after ``read()`` left the count at six
        and the test passing. The order is what matters, so it is recorded.
        """
        order = []
        self.display.shown = order          # show() appends the patch key
        readings = iter([NEUTRAL[key] for key in self.ORDER])

        def read():
            order.append("read")
            return next(readings)

        def sleep(seconds):
            order.append("settle")
            self.slept.append(seconds)

        run(self.display, read, peak_nits=1015.24, sleep=sleep, full=False)

        self.assertEqual(len(self.slept), len(self.ORDER))
        self.assertTrue(all(delay > 0 for delay in self.slept))

        # Every read must be preceded by a settle for the patch just shown.
        for index, entry in enumerate(order):
            if entry == "read":
                self.assertEqual(
                    order[index - 1], "settle",
                    f"a reading at position {index} was taken without settling first",
                )

    def test_reports_progress_for_each_step(self):
        seen = []
        self.run_sequence(
            self.good_reader(),
            on_progress=lambda step, i, total: seen.append((step.key, i, total)),
        )
        self.assertEqual([entry[0] for entry in seen], list(self.ORDER))
        self.assertEqual(seen[0][2], len(self.ORDER))

    def test_a_failed_reading_ends_the_run_rather_than_being_skipped(self):
        def reader():
            raise MeterError("sensor in the wrong position")

        with self.assertRaises(MeasurementError) as caught:
            self.run_sequence(reader)
        self.assertIn("wrong position", str(caught.exception))

    def test_the_failing_step_is_named(self):
        def reader():
            raise MeterError("boom")

        with self.assertRaises(MeasurementError) as caught:
            self.run_sequence(reader)
        self.assertIn("Black level", str(caught.exception))

    def test_aborting_stops_without_producing_a_calibration(self):
        with self.assertRaises(Aborted):
            self.run_sequence(self.good_reader(), should_abort=lambda: True)
        self.assertEqual(self.display.shown, [])

    def test_a_read_stopped_by_the_cancel_is_a_cancel_not_a_meter_fault(self):
        """Esc now stops the spotread in flight, which fails that read. Reporting it as
        a failed measurement would tell the user who cancelled that the meter broke."""
        cancelled = {"now": False}

        def reader():
            cancelled["now"] = True
            raise MeterError("Stopped before the meter answered.")

        with self.assertRaises(Aborted):
            self.run_sequence(reader, should_abort=lambda: cancelled["now"])

    def test_readings_that_do_not_survive_validation_are_refused(self):
        """The run completing is not the same as the readings being usable.

        A peak no display produces means the meter was not against the screen, so
        nothing the run measured describes the display and there is nothing to keep.
        """
        order = iter([NEUTRAL["black"], reading(3.0, 0.3127, 0.3290),
                      NEUTRAL["window-white"], NEUTRAL["balance-white"],
                      NEUTRAL["red"], NEUTRAL["green"], NEUTRAL["blue"]])
        with self.assertRaises(MeasurementError):
            self.run_sequence(lambda: next(order))

    def test_a_run_whose_channels_do_not_add_up_still_returns_what_it_measured(self):
        """Peak and black were read directly. They do not stop being measurements
        because the white balance could not be solved from the patches beside them."""
        same = reading(BALANCE / 3.0, 0.3127, 0.3290)
        order = iter([NEUTRAL["black"], NEUTRAL["white"], NEUTRAL["window-white"],
                      reading(BALANCE, 0.3127, 0.3290), same, same, same])
        result = self.run_sequence(lambda: next(order))
        self.assertAlmostEqual(result.peak_nits, PEAK, places=2)
        self.assertEqual(result.channel_gains, (1.0, 1.0, 1.0))
        self.assertTrue(result.balance_refused)


class WhiteErrorMetricTests(unittest.TestCase):
    """xy is not perceptually uniform, so the verdict is given in u'v'."""

    def test_d65_against_itself_is_zero(self):
        self.assertAlmostEqual(delta_uv(D65_XY, D65_XY), 0.0, places=9)

    def test_d65_is_about_6504_kelvin(self):
        self.assertAlmostEqual(correlated_colour_temperature(*D65_XY), 6504.0, delta=5.0)

    def test_the_panel_as_measured_was_visibly_warm(self):
        """Recorded from the development display before any correction: 5616K,
        which is 0.0123 in u'v' -- four times the threshold a calibrated display
        is held to, and plainly visible."""
        measured = (0.3300, 0.3291)
        self.assertAlmostEqual(correlated_colour_temperature(*measured), 5616.0, delta=20.0)
        self.assertGreater(delta_uv(measured, D65_XY), VERIFIED_DELTA_UV * 4)

    def test_a_calibration_reports_its_own_verdict(self):
        warm = derive(WARM)
        self.assertFalse(warm.verified)
        self.assertGreater(warm.white_delta_uv, VERIFIED_DELTA_UV)
        self.assertTrue(derive(NEUTRAL).verified)

    def test_a_tiny_xy_error_in_an_insensitive_direction_still_passes(self):
        """The point of using u'v': the same dx means different things in
        different parts of the diagram, so dx alone cannot be the test."""
        self.assertLess(delta_uv((D65_XY[0] + 0.0005, D65_XY[1]), D65_XY), VERIFIED_DELTA_UV)


class ComposeGainsTests(unittest.TestCase):
    """A measurement is relative to the correction already in force."""

    APPLIED = (0.78677, 0.98424, 1.0)

    def test_a_perfect_re_measure_leaves_the_correction_alone(self):
        """Without this, a second pass undoes the first: a corrected display
        measures neutral, solves (1, 1, 1), and stores that."""
        composed = compose_gains(self.APPLIED, (1.0, 1.0, 1.0))
        for got, wanted in zip(composed, self.APPLIED):
            self.assertAlmostEqual(got, wanted, places=5)

    def test_a_residual_error_tightens_the_correction_further(self):
        composed = compose_gains(self.APPLIED, (0.97, 1.0, 1.0))
        self.assertLess(composed[0], self.APPLIED[0])

    def test_repeated_passes_converge_rather_than_oscillate(self):
        gains = (1.0, 1.0, 1.0)
        for _ in range(5):
            gains = compose_gains(gains, (0.9, 1.0, 1.0))
        # Each pass finds the same residual, so it keeps tightening; what must
        # not happen is a swing back and forth.
        self.assertLess(gains[0], 0.7)
        self.assertAlmostEqual(max(gains), 1.0, places=6)

    def test_nothing_is_ever_boosted_above_unity(self):
        composed = compose_gains((0.5, 1.0, 0.9), (1.0, 0.8, 1.0))
        self.assertAlmostEqual(max(composed), 1.0, places=6)

    def test_a_degenerate_correction_falls_back_to_neutral(self):
        self.assertEqual(compose_gains((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)), (1.0, 1.0, 1.0))


class PlacementDetectionTests(unittest.TestCase):
    """Whether the instrument is looking at the green placement target.

    A yes/no about placement, not a measurement, so the thresholds are deliberately
    loose: the meter may sit at a slight angle, the panel may be pulling the patch down
    under its own brightness limiter, and a colorimeter's idea of green can be some way
    from the panel's. Anything tight enough to be a real chromaticity check would refuse
    a meter that is correctly placed, which is the one outcome that makes the feature
    worse than the keypress it replaces.
    """

    def reading(self, Y, x, y):
        from sdr_hdr_profile_creator.meter import Reading

        return Reading(X=(x / y) * Y if y else 0.0, Y=Y, Z=0.0, x=x, y=y)

    def test_the_meter_on_the_target_is_detected(self):
        self.assertTrue(sees_placement_target(self.reading(95.0, 0.24, 0.71)))

    def test_a_dark_screen_is_not(self):
        """xy is numerically unstable near zero luminance, so a black reading can land
        anywhere on the diagram -- including on green."""
        self.assertFalse(sees_placement_target(self.reading(0.0003, 0.24, 0.71)))

    def test_the_room_is_not(self):
        """A meter face-up on the desk under a lamp is bright and warm, not green."""
        self.assertFalse(sees_placement_target(self.reading(38.0, 0.44, 0.40)))

    def test_a_white_patch_is_not(self):
        """Bright enough, but this is what the run itself shows -- confusing it with the
        target would mean the meter could 'detect' placement mid-measurement."""
        self.assertFalse(sees_placement_target(self.reading(100.0, 0.3127, 0.3290)))

    def test_a_dimmed_target_is_still_detected(self):
        """The limiter, an angled meter, or a filter over the aperture all cost
        luminance without changing the colour."""
        self.assertTrue(sees_placement_target(self.reading(20.0, 0.21, 0.72)))

    def test_green_too_dim_to_trust_is_not(self):
        """Below the floor the reading is as likely to be stray light as the target."""
        self.assertFalse(sees_placement_target(self.reading(8.0, 0.24, 0.71)))

    def test_both_conditions_are_required(self):
        """Either alone accepts something it should not: luminance alone takes a lit
        room, chromaticity alone takes black-screen noise."""
        bright_not_green = self.reading(200.0, 0.45, 0.40)
        green_not_bright = self.reading(1.0, 0.24, 0.71)
        self.assertFalse(sees_placement_target(bright_not_green))
        self.assertFalse(sees_placement_target(green_not_bright))
        self.assertTrue(sees_placement_target(self.reading(200.0, 0.24, 0.71)))


class PanelResponseTests(unittest.TestCase):
    """Reducing a ramp to what each channel delivered for the code it was sent."""

    def full_readings(self):
        steps = plan(1000.0)
        readings = {}
        for step in steps:
            # A display that tracks PQ but leans blue, so the split is not the same at
            # every level and there is something for a per-level correction to find.
            lean = 1.0 + 0.2 * (step.nits / 1000.0)
            weights = (0.2126, 0.7152, 0.0722 * lean)
            luminance = max(1e-6, step.nits)
            X = Y = Z = 0.0
            for level, weight, name in zip(step.rgb, weights, ("red", "green", "blue")):
                share = level * weight * luminance
                x, y = {"red": (0.64, 0.33), "green": (0.30, 0.60), "blue": (0.15, 0.06)}[name]
                X += share * x / y
                Y += share
                Z += share * (1.0 - x - y) / y
            total = max(1e-9, X + Y + Z)
            readings[step.key] = Reading(X, Y, Z, X / total, Y / total)
        return steps, readings

    def test_the_weights_describe_the_reference_white(self):
        steps, readings = self.full_readings()
        result = derive(readings, steps)
        self.assertAlmostEqual(sum(result.white_weights), 1.0, places=9)
        red, green, blue = result.white_weights
        self.assertGreater(green, red)
        self.assertGreater(red, blue)

    def test_a_response_pairs_every_ramp_point_with_the_code_it_was_sent(self):
        steps, readings = self.full_readings()
        result = derive(readings, steps)
        response = panel_response(result, lambda nits: (pq_inverse_eotf(nits),) * 3)
        self.assertIsNotNone(response)
        self.assertEqual(len(response.red), len(result.greyscale))
        self.assertEqual(len(response.red), len(response.blue))

    def test_a_six_patch_run_yields_no_response(self):
        """There is no ramp in it, so there is no transfer function to solve for. The
        six patches still produce a profile; they just cannot shape a curve."""
        steps = plan(1000.0, full=False)
        readings = {step.key: NEUTRAL[step.key] for step in steps}
        result = derive(readings, steps)
        self.assertEqual(result.greyscale, ())
        self.assertIsNone(panel_response(result, lambda nits: (0.5, 0.5, 0.5)))

    def test_readings_never_paired_with_a_plan_yield_no_response(self):
        """Without the level that was asked for, a reading says nothing about the
        panel's transfer function -- only that some light came out."""
        steps, readings = self.full_readings()
        unpaired = derive(readings)
        self.assertTrue(unpaired.greyscale)
        self.assertTrue(all(point.target_nits == 0.0 for point in unpaired.greyscale))
        self.assertIsNone(panel_response(unpaired, lambda nits: (pq_inverse_eotf(nits),) * 3))

    def test_a_grey_point_reports_its_reading_as_xyz(self):
        point = GreyPoint(index=3, target_nits=100.0, measured_nits=92.0, x=0.3127, y=0.3290)
        X, Y, Z = point.xyz
        self.assertAlmostEqual(Y, 92.0, places=9)
        self.assertAlmostEqual(X / (X + Y + Z), 0.3127, places=9)
        self.assertAlmostEqual(Y / (X + Y + Z), 0.3290, places=9)


class FullSweepPlanTests(unittest.TestCase):
    """The Calman-style run: a greyscale ramp and a saturation sweep per hue.

    Six patches say what peak, black and white are and nothing about the range between
    them, which is where a display's tone response and RGB balance actually live.
    """

    PEAK = 1015.24

    def test_the_core_patches_still_come_first_and_unchanged(self):
        """Everything the profile is built from is derived from these, so a longer run
        must not change what a short one would have produced."""
        full = plan(self.PEAK)
        quick = plan(self.PEAK, full=False)
        self.assertEqual(len(quick), 7)
        self.assertEqual([s.key for s in full[:7]], [s.key for s in quick])
        for long, short in zip(full[:7], quick):
            self.assertEqual(
                (long.rgb, long.nits, long.window_fraction),
                (short.rgb, short.nits, short.window_fraction),
            )

    def test_it_measures_more_than_thirty_of_each(self):
        full = plan(self.PEAK)
        grey = [s for s in full if s.key.startswith("grey-")]
        colour = [s for s in full if s.key.startswith("colour-")]
        self.assertGreaterEqual(len(grey), 30)
        self.assertGreaterEqual(len(colour), 30)

    def test_every_key_is_unique(self):
        """Readings are collected into a dict; a repeated key silently discards one."""
        keys = [s.key for s in plan(self.PEAK)]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_ramp_is_spaced_perceptually_not_linearly(self):
        """Even steps in nits would put half the samples above half peak, where the eye
        can barely separate two levels, and three or four across the whole of the
        shadows, where it easily can."""
        levels = greyscale_levels(self.PEAK)
        self.assertGreaterEqual(sum(1 for v in levels if v < 10.0), 8)
        # And it is monotonic, or it is not a ramp.
        self.assertEqual(list(levels), sorted(levels))

    def test_the_ramp_reaches_the_panels_peak_and_starts_above_the_noise(self):
        levels = greyscale_levels(self.PEAK)
        self.assertAlmostEqual(self.PEAK, levels[-1], delta=1.0)
        self.assertGreater(levels[0], 0.0)

    def test_saturation_is_a_path_from_white_to_the_primary(self):
        """Scaling the primary instead would change luminance along with colour, making
        the sweep a measurement of two things at once."""
        full = plan(self.PEAK)
        by_key = {s.key: s for s in full}
        full_red = by_key["colour-red-100"]
        part_red = by_key["colour-red-020"]
        self.assertEqual((1.0, 0.0, 0.0), full_red.rgb)
        # At 20% the off-channels have only come down a fifth of the way.
        self.assertAlmostEqual(1.0, part_red.rgb[0], places=6)
        self.assertAlmostEqual(0.8, part_red.rgb[1], places=6)
        self.assertAlmostEqual(0.8, part_red.rgb[2], places=6)

    def test_all_six_hues_are_swept(self):
        """Cyan, magenta and yellow are where a display's own colour handling shows
        itself; the three primaries alone cannot see it."""
        hues = {s.key.split("-")[1] for s in plan(self.PEAK) if s.key.startswith("colour-")}
        self.assertEqual({"red", "yellow", "green", "cyan", "blue", "magenta"}, hues)

    def test_the_colours_are_measured_at_one_level(self):
        """A hue read at a different drive samples a different point on the response
        and cannot be compared with the others."""
        levels = {s.nits for s in plan(self.PEAK) if s.key.startswith("colour-")}
        self.assertEqual(1, len(levels))

    def test_the_estimate_is_honest_about_how_long_this_takes(self):
        """Four minutes nobody was warned about reads as a hang."""
        full, quick = plan(self.PEAK), plan(self.PEAK, full=False)
        self.assertGreater(estimated_seconds(full), estimated_seconds(quick) * 5)
        self.assertGreater(estimated_seconds(full), 180.0)
        self.assertLess(estimated_seconds(full), 600.0)


class DisplayDriftTests(unittest.TestCase):
    """A run whose display changed underneath it is not a measurement of anything.

    The app keeps its own hands off the display while a run is up, but two things move
    it from outside: Windows leaving HDR by itself, and the standalone watchdog -- a
    separate process -- re-applying a profile. Either one between two patches means the
    readings on each side describe different displays while every number stays
    plausible, so the run carries the OS's view of the display and stops when it moves.
    """

    ORDER = RunTests.ORDER

    def setUp(self):
        self.display = FakeDisplay()
        self.delivered = []

    def good_reader(self):
        order = iter([NEUTRAL[key] for key in self.ORDER])
        return lambda: next(order)

    def flipping_probe(self, after_calls, before, after):
        """Reports `before` for the first `after_calls` samples, then `after`."""
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return dict(before if calls["n"] <= after_calls else after)

        probe.calls = calls
        return probe

    def sequence_probe(self, *samples):
        """Reports the samples in order, then repeats the last one."""
        pending = list(samples)

        def probe():
            if len(pending) > 1:
                return dict(pending.pop(0))
            return dict(pending[0])

        return probe

    def run_with(self, probe, **extra):
        return run(
            self.display, self.good_reader(), peak_nits=1015.24,
            sleep=lambda _s: None, full=False, probe=probe,
            on_reading=lambda step, value: self.delivered.append(step.key),
            **extra,
        )

    def test_a_display_not_in_the_expected_state_is_refused_before_the_first_patch(self):
        """The baseline used to be trusted rather than validated. The only HDR check
        on the meter path runs on the UI thread before the surface exists; a display
        that left HDR between that check and the worker's first sample gave an
        all-SDR run that was self-consistent from end to end, and it passed."""
        with self.assertRaises(MeasurementError) as caught:
            self.run_with(self.sequence_probe({"mode": "SDR", "profile": "A.icm"}),
                          expected={"mode": "HDR"})
        self.assertIn("before the first patch", str(caught.exception))
        self.assertIn("mode went from HDR to SDR", str(caught.exception))
        self.assertEqual([], self.display.shown, "a patch was shown to an SDR display")
        self.assertEqual([], self.delivered)

    def test_an_expected_state_that_matches_lets_the_run_proceed(self):
        self.run_with(self.sequence_probe({"mode": "HDR", "profile": "A.icm"}),
                      expected={"mode": "HDR"})
        self.assertEqual(list(self.ORDER), self.delivered)

    def test_the_reference_is_the_baseline_not_the_previous_sample(self):
        """A change that lands while one sample is unreadable must still be caught.
        Comparing each sample to the previous one would see None -> B.icm, treat the
        None side as unread, and let the swap through."""
        probe = self.sequence_probe(
            {"profile": "A.icm"}, {"profile": None}, {"profile": "B.icm"},
        )
        with self.assertRaises(MeasurementError) as caught:
            self.run_with(probe)
        self.assertIn("profile went from A.icm to B.icm", str(caught.exception))

    def test_a_field_unreadable_at_the_start_is_armed_by_its_first_reading(self):
        """Otherwise one failed call on the very first sample disarmed that field for
        the whole run: every later comparison saw None on the baseline side."""
        probe = self.sequence_probe(
            {"profile": None}, {"profile": "A.icm"}, {"profile": "A.icm"},
            {"profile": "B.icm"},
        )
        with self.assertRaises(MeasurementError) as caught:
            self.run_with(probe)
        self.assertIn("profile went from A.icm to B.icm", str(caught.exception))

    def test_sustained_is_refused_before_the_first_reading_when_not_in_hdr(self):
        from vhdr_color.measure import sustained

        stable = reading(400.0, 0.3130, 0.3290)
        with self.assertRaises(MeasurementError) as caught:
            sustained(self.display, lambda: stable, peak_nits=1000.0,
                      sleep=lambda _s: None, probe=lambda: {"mode": "SDR"},
                      expected={"mode": "HDR"})
        self.assertIn("before the first patch", str(caught.exception))
        self.assertEqual([], self.display.shown)

    def test_a_display_that_did_not_change_is_sampled_and_the_run_completes(self):
        probe = self.flipping_probe(99, {"mode": "HDR", "profile": "A.icm"}, {})
        self.run_with(probe)
        # Once before the first patch, then once after each of the seven readings.
        self.assertEqual(len(self.ORDER) + 1, probe.calls["n"])
        self.assertEqual(list(self.ORDER), self.delivered)

    def test_a_profile_swapped_mid_run_stops_it_and_names_what_moved(self):
        """The baseline plus two clean readings, then the watchdog re-associates."""
        probe = self.flipping_probe(
            3, {"mode": "HDR", "profile": "A.icm"}, {"mode": "HDR", "profile": "B.icm"}
        )
        with self.assertRaises(MeasurementError) as caught:
            self.run_with(probe)
        message = str(caught.exception)
        self.assertIn("profile went from A.icm to B.icm", message)
        self.assertIn("display changed during the run", message)
        # Stopped at the third patch: nothing after it was shown or read.
        self.assertEqual(list(self.ORDER[:3]), self.display.shown)
        # The reading taken under the old state is still delivered, so the log keeps
        # the number and the refusal can be read against it afterwards.
        self.assertEqual(list(self.ORDER[:3]), self.delivered)

    def test_a_mode_drop_is_said_in_words(self):
        from vhdr_color.measure import describe_drift

        self.assertEqual(
            "mode went from HDR to SDR",
            describe_drift({"mode": "HDR", "profile": "A.icm"},
                           {"mode": "SDR", "profile": "A.icm"}),
        )
        self.assertEqual(
            "mode went from HDR to SDR; sdr_white went from 240.0 to 80.0",
            describe_drift({"mode": "HDR", "sdr_white": 240.0},
                           {"mode": "SDR", "sdr_white": 80.0}),
        )
        self.assertEqual("", describe_drift({"mode": "HDR"}, {"mode": "HDR"}))

    def test_an_unreadable_sample_is_not_a_change(self):
        """One failed Windows call in the middle of a four-minute run is far likelier
        than the display changing at exactly that moment, and refusing the run over
        it would cost the user the whole run for nothing."""
        from vhdr_color.measure import describe_drift

        self.assertEqual("", describe_drift({"profile": "A.icm"}, {"profile": None}))
        self.assertEqual("", describe_drift({"profile": None}, {"profile": "A.icm"}))
        self.assertEqual("", describe_drift(None, {"profile": "A.icm"}))
        probe = self.flipping_probe(2, {"profile": "A.icm"}, {"profile": None})
        self.run_with(probe)   # must not raise
        self.assertEqual(list(self.ORDER), self.delivered)

    def test_a_run_without_a_probe_is_unchanged(self):
        self.run_with(None)
        self.assertEqual(list(self.ORDER), self.delivered)

    def test_sustained_is_refused_when_the_display_changes_while_held(self):
        from vhdr_color.measure import sustained

        stable = reading(400.0, 0.3130, 0.3290)
        probe = self.flipping_probe(1, {"mode": "HDR"}, {"mode": "SDR"})
        seen = []
        with self.assertRaises(MeasurementError) as caught:
            sustained(self.display, lambda: stable, peak_nits=1000.0,
                      sleep=lambda _s: None, probe=probe,
                      on_reading=lambda index, nits: seen.append(index))
        self.assertIn("after reading 1", str(caught.exception))
        self.assertIn("mode went from HDR to SDR", str(caught.exception))
        # The reading taken under the old state was delivered before the refusal, so
        # the log keeps it -- the same invariant run() documents.
        self.assertEqual([0], seen)

    def test_sustained_completes_when_the_display_holds_still(self):
        from vhdr_color.measure import sustained

        stable = reading(400.0, 0.3130, 0.3290)
        result = sustained(self.display, lambda: stable, peak_nits=1000.0,
                           sleep=lambda _s: None,
                           probe=lambda: {"mode": "HDR", "profile": "A.icm"})
        self.assertTrue(result.settled)


if __name__ == "__main__":
    unittest.main()
