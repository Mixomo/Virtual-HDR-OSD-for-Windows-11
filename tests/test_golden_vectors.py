"""The colour maths against the standards, not against itself.

Every other test of these functions checks the code against its own inverse or against
another function in this package, so a constant both sides share would pass wrong. Here
the reference value is computed inside the test, from the constants a standard publishes,
with exact or 50-digit arithmetic:

* PQ: SMPTE ST 2084 / ITU-R BT.2100.
* sRGB: IEC 61966-2-1.
* Bradford: Lam (1985), as adopted by ICC.1 Annex E.
* ICtCp and Delta ITP: ITU-R BT.2100 (RGB to LMS) and BT.2124 (the 720 scale).

The last class is different in kind: a regression pin on the bytes of two generated
profiles, so that moving the colour core between packages can be shown to change nothing.
"""

from __future__ import annotations

import unittest
from decimal import Decimal, getcontext
from fractions import Fraction

from vhdr_color import curves, delta_itp, gamma_correction, icc, patterns

getcontext().prec = 50

# ST 2084 publishes these as ratios. They are dyadic, so the floats can be exact.
M1 = Fraction(2610, 16384)
M2 = Fraction(2523, 4096) * 128
C1 = Fraction(3424, 4096)
C2 = Fraction(2413, 4096) * 32
C3 = Fraction(2392, 4096) * 32


def _d(value) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator) if isinstance(value, Fraction) else Decimal(value)


def pq_code(nits: float) -> float:
    """ST 2084 inverse EOTF, in 50-digit arithmetic."""
    y = Decimal(repr(nits)) / Decimal(10000)
    if y == 0:
        return float(_d(C1) ** _d(M2))
    ym1 = y ** _d(M1)
    return float(((_d(C1) + _d(C2) * ym1) / (1 + _d(C3) * ym1)) ** _d(M2))


def pq_nits(code: float) -> float:
    """ST 2084 EOTF, in 50-digit arithmetic."""
    v = Decimal(repr(code))
    if v == 0:
        return 0.0
    p = v ** (1 / _d(M2))
    return float(10000 * (max(p - _d(C1), Decimal(0)) / (_d(C2) - _d(C3) * p)) ** (1 / _d(M1)))


def _inverse(m):
    a, b, c, d, e, f, g, h, i = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return (
        (e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det,
        (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det,
        (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det,
    )


def _mul(a, b):
    return tuple(sum(a[3 * r + k] * b[3 * k + c] for k in range(3)) for r in range(3) for c in range(3))


class PQTests(unittest.TestCase):
    def test_the_constants_are_exactly_st2084(self):
        self.assertEqual((float(M1), float(M2), float(C1), float(C2), float(C3)),
                         (gamma_correction.M1, gamma_correction.M2, gamma_correction.C1,
                          gamma_correction.C2, gamma_correction.C3))
        self.assertEqual(C1 + C2, 1 + C3, "10,000 nits must encode to exactly 1.0")

    def test_luminance_to_code(self):
        for nits in (0.0, 0.005, 1.0, 100.0, 203.0, 1000.0, 4000.0, 10000.0):
            with self.subTest(nits=nits):
                self.assertAlmostEqual(pq_code(nits), gamma_correction.pq_inverse_eotf(nits), delta=1e-12)

    def test_code_to_luminance(self):
        for code in (0.0, 0.1, 0.5, 0.58, 0.75, 0.9, 1.0):
            with self.subTest(code=code):
                expected = pq_nits(code)
                self.assertAlmostEqual(expected, gamma_correction.pq_eotf(code),
                                       delta=max(1e-12, expected * 1e-11))

    def test_reference_white_is_58_percent(self):
        """ITU-R BT.2408: HDR Reference White, 203 cd/m2, sits at 58% PQ."""
        self.assertEqual(58, round(100 * gamma_correction.pq_inverse_eotf(203.0)))


class SRGBTests(unittest.TestCase):
    """IEC 61966-2-1: decode V/12.92 below 0.04045, else ((V+0.055)/1.055)^2.4."""

    @staticmethod
    def decode(v: float) -> float:
        d = Decimal(repr(v))
        return float(d / Decimal("12.92") if d <= Decimal("0.04045")
                     else ((d + Decimal("0.055")) / Decimal("1.055")) ** Decimal("2.4"))

    @staticmethod
    def encode(l: float) -> float:
        d = Decimal(repr(l))
        return float(d * Decimal("12.92") if d <= Decimal("0.0031308")
                     else Decimal("1.055") * d ** (1 / Decimal("2.4")) - Decimal("0.055"))

    def test_decode(self):
        for value in (0.0, 0.02, 0.04045, 128 / 255, 0.5, 1.0):
            with self.subTest(value=value):
                self.assertAlmostEqual(self.decode(value), patterns._srgb_to_linear(value), delta=1e-12)

    def test_encode_away_from_the_cutoff(self):
        """The app breaks at 0.00313066844..., where the two branches actually meet; the
        standard rounds that to 0.0031308. Points either side of both agree."""
        for linear in (0.0, 0.001, 0.18, 0.5, 1.0):
            with self.subTest(linear=linear):
                self.assertAlmostEqual(self.encode(linear), gamma_correction.srgb_inverse_eotf(linear),
                                       delta=1e-12)

    def test_the_branches_meet_at_the_app_cutoff(self):
        cut = gamma_correction.SRGB_LINEAR_CUTOFF
        linear_branch = cut * 12.92
        power_branch = 1.055 * cut ** (1 / 2.4) - 0.055
        self.assertAlmostEqual(linear_branch, power_branch, delta=1e-12)


class BradfordTests(unittest.TestCase):
    LAM = (
        Fraction("0.8951"), Fraction("0.2664"), Fraction("-0.1614"),
        Fraction("-0.7502"), Fraction("1.7135"), Fraction("0.0367"),
        Fraction("0.0389"), Fraction("-0.0685"), Fraction("1.0296"),
    )

    def test_the_matrix_is_lams(self):
        self.assertEqual(tuple(float(v) for v in self.LAM), tuple(curves._BRADFORD))

    def test_d65_to_d50_is_the_bradford_adaptation(self):
        """Computed with exact fractions from D65 (0.95047, 1, 1.08883) to the PCS white
        the header declares, (0.9642, 1, 0.8249). Re-recorded on 3 October 2026: the
        matrix used to be built for (0.96422, 1, 0.82521), so it took D65 to a white
        0.0003 off in Z from the one the same profile declared."""
        src = (Fraction("0.95047"), Fraction(1), Fraction("1.08883"))
        dst = (Fraction("0.9642"), Fraction(1), Fraction("0.8249"))
        lms = lambda xyz: tuple(sum(self.LAM[3 * r + k] * xyz[k] for k in range(3)) for r in range(3))
        s, t = lms(src), lms(dst)
        scale = (t[0] / s[0], 0, 0, 0, t[1] / s[1], 0, 0, 0, t[2] / s[2])
        expected = _mul(_mul(_inverse(self.LAM), scale), self.LAM)
        for got, want in zip(icc.D65_TO_D50_CHAD, expected):
            self.assertAlmostEqual(float(want), got, delta=5e-8)

    def test_the_header_white_is_the_icc_pcs_illuminant(self):
        self.assertEqual((0.9642, 1.0, 0.8249), tuple(icc.D50_XYZ))


class ICtCpTests(unittest.TestCase):
    """BT.2100 defines LMS from BT.2020 RGB; the XYZ form the app uses is derived here."""

    RGB_TO_LMS = tuple(Fraction(v, 4096) for v in (1688, 2146, 262, 683, 2951, 462, 99, 309, 3688))

    @staticmethod
    def rgb2020_to_xyz():
        primaries = ((Fraction("0.708"), Fraction("0.292")), (Fraction("0.170"), Fraction("0.797")),
                     (Fraction("0.131"), Fraction("0.046")))
        wx, wy = Fraction("0.3127"), Fraction("0.3290")
        columns = [(x / y, Fraction(1), (1 - x - y) / y) for x, y in primaries]
        p = tuple(columns[c][r] for r in range(3) for c in range(3))
        white = (wx / wy, Fraction(1), (1 - wx - wy) / wy)
        inv = _inverse(p)
        gains = tuple(sum(inv[3 * r + k] * white[k] for k in range(3)) for r in range(3))
        return tuple(p[3 * r + c] * gains[c] for r in range(3) for c in range(3))

    def test_the_xyz_to_lms_matrix(self):
        expected = _mul(self.RGB_TO_LMS, _inverse(self.rgb2020_to_xyz()))
        got = tuple(v for row in delta_itp.XYZ_TO_LMS for v in row)
        for g, w in zip(got, expected):
            self.assertAlmostEqual(float(w), g, delta=1.5e-4)

    def test_rows_sum_to_one(self):
        for r in range(3):
            self.assertEqual(4096, sum(int(v * 4096) for v in self.RGB_TO_LMS[3 * r:3 * r + 3]))

    def test_a_neutral_luminance_step(self):
        """Neutrals have no chroma, so Delta ITP is 720 times the PQ step in I."""
        expected = 720 * (pq_code(110.0) - pq_code(100.0))
        got = delta_itp.delta_itp(delta_itp.neutral_xyz(110.0), delta_itp.neutral_xyz(100.0))
        self.assertAlmostEqual(expected, got, delta=1e-3)


class GeneratedProfileTests(unittest.TestCase):
    """A regression pin, not an independent reference: the bytes two states produce,
    with the creation time and profile ID zeroed by content_digest."""

    def digest(self, **changes) -> str:
        from vhdr_color.model import ModeState

        state = ModeState.neutral("HDR")
        for key, value in changes.items():
            setattr(state, key, value)
        return icc.content_digest(icc.build_profile("HDR", state, curves.build_transform(state, hdr=True)))

    def test_a_neutral_profile(self):
        self.assertEqual("4debf4940447b721ce48aa98a2f2584aef047687f916c3c1186bf1b9ac5ed430",
                         self.digest())

    def test_an_adjusted_profile(self):
        self.assertEqual(
            "83117dc95f1b8dc4cbea8ca005a5b6a7fe3b03c1bea6aee314d7f57862d79f79",
            self.digest(gamma=2.4, contrast=10.0, temperature=500.0, red_channel=-5.0,
                        peak_luminance_nits=950.0, minimum_luminance_nits=0.05,
                        sdr_gamma_correction="200 nits / Brightness 30"),
        )


if __name__ == "__main__":
    unittest.main()
