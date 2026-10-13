"""Independent colour-space vectors and sampled-gamut geometry; no hardware required."""
import math
import struct
import unittest

from vhdr_color import gamut, measure
from vhdr_color.patterns import PatternContext, measurement_frame


def readings(primaries):
    result = {}
    for step in measure.plan(1000, gamut=True):
        if not step.key.startswith("gamut-"):
            continue
        # Synthetic additive display with the requested reference triangle.
        xyz = tuple(sum(d * component for d, component in zip(step.rgb, row))
                    for row in zip(*[(x / y, 1, (1 - x - y) / y) for x, y in primaries]))
        total = sum(xyz)
        result[step.key] = measure.Reading(*xyz, xyz[0] / total, xyz[1] / total)
    return result


class WideGamutTests(unittest.TestCase):
    def test_conversion_reaches_standard_bt2020_primary_chromaticities(self):
        # Independent BT.709 -> XYZ matrix, not the production conversion matrix.
        to_xyz = ((.4123907993, .3575843394, .1804807884),
                  (.2126390059, .7151686788, .0721923154),
                  (.0193308187, .1191947798, .9505321522))
        for rgb, xy in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), gamut.REFERENCES['BT.2020']):
            converted = gamut.to_scrgb(rgb)
            self.assertLess(min(converted), 0)
            xyz = tuple(sum(a*b for a, b in zip(row, converted)) for row in to_xyz)
            self.assertAlmostEqual(xyz[0]/sum(xyz), xy[0], places=8)
            self.assertAlmostEqual(xyz[1]/sum(xyz), xy[1], places=8)

    def test_neutral_white_and_linear_scaling(self):
        for channel in gamut.to_scrgb((1, 1, 1)):
            self.assertAlmostEqual(channel, 1, places=9)
        for a, b in zip(gamut.to_scrgb((.5, 0, 0)), gamut.to_scrgb((1, 0, 0))):
            self.assertAlmostEqual(a, b/2)

    def test_fp16_frame_preserves_negative_components_and_absolute_level(self):
        raw = measurement_frame(20, 20, (0, 1, 0), 160, PatternContext(True), colour_space='BT.2020')
        rgb = struct.unpack_from('<4e', raw, (10*20+10)*8)
        for actual, expected in zip(rgb, gamut.to_scrgb((0, 1, 0))):
            self.assertAlmostEqual(actual, 2*expected, delta=.001)
        self.assertLess(rgb[0], 0)
        self.assertLess(rgb[2], 0)
        self.assertEqual(struct.unpack_from('<4e', raw)[0:3], (0, 0, 0))

    def test_sdr_and_invalid_stimuli_are_refused(self):
        for rgb in ((math.nan, 0, 0), (-1, 0, 0), (2, 0, 0), (1, 0)):
            with self.assertRaises(ValueError):
                gamut.to_scrgb(rgb)
        for context, space in ((PatternContext(False), 'BT.2020'), (PatternContext(True), 'unknown')):
            with self.assertRaises(ValueError):
                measurement_frame(2, 2, (1, 0, 0), 80, context, colour_space=space)

    def test_wide_plan_preserves_all_calibration_steps(self):
        old = measure.plan(1000)
        new = measure.plan(1000, gamut=True)
        self.assertEqual(new[:len(old)], old)
        self.assertEqual(len(new), 100)
        self.assertEqual(len({s.key for s in new}), 100)
        self.assertTrue(all(s.colour_space == 'BT.2020' for s in new[len(old):]))
        self.assertEqual(measure.plan(1000, full=False, gamut=True), measure.plan(1000, full=False))

    def test_reference_display_has_correct_intersection_coverage(self):
        for name, triangle in gamut.REFERENCES.items():
            result = gamut.report(readings(triangle))
            self.assertNotIn('refused', result)
            self.assertAlmostEqual(result['coverage_percent'][name], 100, places=3)
        limited = gamut.report(readings(gamut.REFERENCES['BT.709']))
        self.assertLess(limited['coverage_percent']['Display P3'], 100)
        self.assertLess(limited['coverage_percent']['BT.2020'], limited['coverage_percent']['Display P3'])

    def test_shifted_triangle_cannot_fake_full_coverage_with_area_ratio(self):
        ref = gamut.REFERENCES['BT.709']
        shifted = gamut.hull([(x+.1, y) for x, y in ref])
        self.assertAlmostEqual(gamut.area(shifted), gamut.area(ref))
        self.assertLess(gamut.coverage(shifted, ref), .9)
        self.assertEqual(gamut.coverage(((2, 2), (3, 2), (2, 3)), ref), 0)

    def test_bad_or_incomplete_readings_never_create_coverage(self):
        self.assertEqual(gamut.report({}), {})
        data = readings(gamut.REFERENCES['BT.2020'])
        self.assertIn('refused', gamut.report(dict(list(data.items())[:-1])))
        for bad in (measure.Reading(math.nan, 1, 1, .3, .3),
                    measure.Reading(1, 1, 1, .5, .3),
                    measure.Reading(0, 0, 0, .3, .3)):
            changed = {**data, next(iter(data)): bad}
            result = gamut.report(changed)
            self.assertIn('refused', result)
            self.assertNotIn('coverage_percent', result)

    def test_wide_diagnostics_do_not_change_calibration_or_bt709_sweeps(self):
        from dataclasses import replace
        from test_measure import NEUTRAL
        base = measure.derive(NEUTRAL)
        result = measure.derive({**NEUTRAL, **readings(gamut.REFERENCES['BT.2020'])})
        self.assertTrue(result.gamut_report['coverage_percent'])
        self.assertEqual(replace(result, gamut_report={}), base)

    def test_full_run_transports_wide_steps_and_probes_through_same_abort_guards(self):
        from test_measure import NEUTRAL, reading
        class Display:
            step = None
            def show(self, step):
                self.step = step
        display = Display()
        wide = readings(gamut.REFERENCES['Display P3'])
        def read():
            step = display.step
            return NEUTRAL.get(step.key, wide.get(step.key)) or reading(step.nits, .3127, .3290)
        result = measure.run(display, read, peak_nits=1000, gamut=True, sleep=lambda _: None)
        self.assertEqual(result.gamut_report['patch_count'], 30)
        self.assertEqual(len(result.greyscale), 33)
        self.assertEqual(len(result.colours), 30)
        with self.assertRaises(measure.Aborted):
            measure.run(display, read, peak_nits=1000, gamut=True, sleep=lambda _: None,
                        should_abort=lambda: display.step is not None
                        and display.step.key == 'gamut-red-020')


if __name__ == '__main__':
    unittest.main()
