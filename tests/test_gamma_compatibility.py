"""Tone output pinned to the supplied 1.0.0-hotfix-3 implementation."""
import json
import unittest
from pathlib import Path

from vhdr_color.curves import _shape_curve, build_transform
from vhdr_color.model import ModeState
from vhdr_color.gamma_correction import pq_inverse_eotf, pq_eotf
from vhdr_color.greyscale import sample


class Hotfix3ToneCompatibilityTests(unittest.TestCase):
    def test_all_reference_vectors_match_hotfix3(self):
        fixture = Path(__file__).parent / 'fixtures' / 'hotfix3_tone_vectors.json'
        data = json.loads(fixture.read_text(encoding='utf-8'))
        self.assertEqual(data['reference'], '1.0.0-hotfix-3')
        for vector in data['vectors']:
            state = ModeState.neutral('HDR')
            state.gamma = vector['gamma']
            state.contrast = vector['contrast']
            state.brightness_trim = vector['brightness']
            state.sdr_gamma_correction = vector['correction']
            with self.subTest(**vector):
                self.assertAlmostEqual(_shape_curve(vector['input'], state, True,
                                                   vector['white']), vector['expected'], places=12)

    def test_generated_on_off_luts_keep_plus10_above_white(self):
        state = ModeState.neutral('HDR')
        state.brightness_trim = 10
        curves = []
        for option in ('Off', 'Auto (Recommended)'):
            state.sdr_gamma_correction = option
            curves.append(build_transform(state, True, 200).red)
        for nits in (250, 500, 1000, 4000):
            code = pq_inverse_eotf(nits)
            off, on = (pq_eotf(sample(curve, code)) for curve in curves)
            self.assertAlmostEqual(off, on, places=7)
            self.assertGreater(on, nits)


if __name__ == '__main__':
    unittest.main()
