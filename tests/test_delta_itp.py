"""Delta ITP, checked against properties the standard guarantees rather than against
numbers copied out of this implementation.

A test that asserts what the code already returns proves only that nobody changed it.
These pin things BT.2124 and BT.2100 require: D65 is achromatic, the metric is a metric,
and -- the reason for adopting it at all -- equal percentage errors at different
luminances are not equally visible.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhdr_color import delta_itp as itp  # noqa: E402
from vhdr_color.measure import D65_XY as MEASURE_D65  # noqa: E402


class WhitePointTests(unittest.TestCase):
    def test_the_d65_constant_has_not_drifted_from_the_one_measurements_use(self):
        """delta_itp keeps its own copy so it stays a leaf module and cannot form an
        import cycle with measure.py. That is only safe while the two agree."""
        self.assertEqual(itp.D65_XY, MEASURE_D65)

    def test_a_d65_neutral_is_achromatic(self):
        """The defining property of ICtCp: at the D65 white point the two chroma axes
        are zero at every luminance. If this drifts, the matrix is wrong and every
        reported figure is quietly biased.

        The tolerance is 1e-4 because BT.2100 publishes the matrix to four decimals, so
        a residue around 3e-5 is the table's own rounding rather than an error here.
        """
        for nits in (0.1, 0.5, 5.0, 100.0, 203.0, 1000.0, 4000.0, 10000.0):
            with self.subTest(nits=nits):
                _i, ct, cp = itp.xyz_to_ictcp(itp.neutral_xyz(nits))
                self.assertLess(abs(ct), 1e-4)
                self.assertLess(abs(cp), 1e-4)

    def test_a_neutral_carries_the_luminance_and_chromaticity_it_was_asked_for(self):
        x, y, z = itp.neutral_xyz(250.0)
        self.assertAlmostEqual(y, 250.0)
        total = x + y + z
        self.assertAlmostEqual(x / total, itp.D65_XY[0], places=9)
        self.assertAlmostEqual(y / total, itp.D65_XY[1], places=9)

    def test_a_white_point_with_no_luminance_is_rejected_rather_than_dividing_by_zero(self):
        with self.assertRaises(ValueError):
            itp.neutral_xyz(100.0, (0.3127, 0.0))


class MetricTests(unittest.TestCase):
    def test_a_colour_differs_from_itself_by_nothing(self):
        self.assertEqual(itp.delta_itp(itp.neutral_xyz(100.0), itp.neutral_xyz(100.0)), 0.0)

    def test_the_difference_is_symmetric(self):
        a, b = itp.neutral_xyz(100.0), itp.neutral_xyz(140.0)
        self.assertAlmostEqual(itp.delta_itp(a, b), itp.delta_itp(b, a), places=12)

    def test_a_bigger_error_scores_higher(self):
        target = 100.0
        scores = [itp.grey_delta_itp(itp.neutral_xyz(target * k), target)
                  for k in (1.01, 1.05, 1.20, 1.50)]
        self.assertEqual(scores, sorted(scores))

    def test_zero_and_negative_input_do_not_raise(self):
        """Meters return small negatives at black. PQ is undefined there, so the values
        are clamped rather than allowed to become a crash mid-run."""
        self.assertGreaterEqual(itp.delta_itp((0.0, 0.0, 0.0), itp.neutral_xyz(0.5)), 0.0)
        self.assertGreaterEqual(itp.delta_itp((-0.01, -0.01, -0.01), (0.0, 0.0, 0.0)), 0.0)


class WhyItIsWorthHavingTests(unittest.TestCase):
    """The two things a luminance-percentage report structurally cannot do."""

    def test_the_same_percentage_error_is_not_equally_visible_at_every_level(self):
        """This is the whole argument for the metric. The report's median percentage
        weights a 5% miss at 0.5 nits the same as a 5% miss at 1000, and the eye does
        not. If these ever came out equal, dITP would be adding nothing."""
        ratio = 1.05
        low = itp.grey_delta_itp(itp.neutral_xyz(1.0 * ratio), 1.0)
        high = itp.grey_delta_itp(itp.neutral_xyz(1000.0 * ratio), 1000.0)
        self.assertGreater(high, low * 1.5)

    def test_a_pure_white_balance_error_is_caught_with_the_luminance_exactly_right(self):
        """A tinted grey with the correct Y is invisible to a luminance-ratio report --
        error 0.00% -- and is exactly what the eye notices first on a greyscale ramp."""
        target = 100.0
        tinted = itp.neutral_xyz(target, (itp.D65_XY[0] + 0.008, itp.D65_XY[1]))
        self.assertAlmostEqual(tinted[1], target, places=9, msg="luminance must be untouched")
        self.assertGreater(itp.grey_delta_itp(tinted, target), itp.GOOD)

    def test_a_visible_shift_scores_above_the_threshold_and_a_tiny_one_below(self):
        """Calibrated against the tolerance this project already uses elsewhere: white
        within 0.003 of D65 counts as neutral in measure.py, and that lands just under
        one JND here. The two were arrived at independently."""
        target = 100.0
        near = itp.neutral_xyz(target, (itp.D65_XY[0] + 0.001, itp.D65_XY[1]))
        far = itp.neutral_xyz(target, (itp.D65_XY[0] + 0.010, itp.D65_XY[1]))
        self.assertLess(itp.grey_delta_itp(near, target), itp.JND)
        self.assertGreater(itp.grey_delta_itp(far, target), itp.GOOD)


class CurveTests(unittest.TestCase):
    def test_the_curve_is_ordered_by_level_whatever_order_it_arrives_in(self):
        points = [
            (100.0, itp.neutral_xyz(100.0)),
            (1.0, itp.neutral_xyz(1.0)),
            (10.0, itp.neutral_xyz(10.0)),
        ]
        self.assertEqual([nits for nits, _ in itp.curve(points)], [1.0, 10.0, 100.0])

    def test_points_asking_for_no_light_are_dropped_rather_than_scored(self):
        """PQ has no headroom below black, so a zero-nit target scores the meter's noise
        floor rather than the display, and one such point can dominate a median."""
        points = [(0.0, (0.0, 0.0, 0.0)), (100.0, itp.neutral_xyz(100.0))]
        self.assertEqual([nits for nits, _ in itp.curve(points)], [100.0])

    def test_a_perfect_ramp_scores_zero_throughout(self):
        points = [(nits, itp.neutral_xyz(nits)) for nits in (0.5, 5.0, 50.0, 500.0)]
        self.assertEqual([round(score, 12) for _n, score in itp.curve(points)], [0.0] * 4)


if __name__ == "__main__":
    unittest.main()
