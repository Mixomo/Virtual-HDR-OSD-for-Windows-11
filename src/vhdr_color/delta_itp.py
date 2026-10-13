"""Delta ITP (ITU-R BT.2124): how visible a colour error is, rather than how large.

A luminance error in percent and a white drift as a distance in xy, which is how
accuracy was reported before this, are two numbers in two units, and neither says the
thing anyone actually wants to know -- would you see it. A 2% luminance error at 0.5 nits and the same 2% at 500
nits are nothing like each other to the eye, and a percentage cannot express that.

dITP is defined on ICtCp (BT.2100), which is PQ-encoded and near enough perceptually
uniform across the whole HDR range, so one number covers the tone error and the
white-balance error together and means the same thing wherever on the ramp it is
measured. BT.2124 puts one just-noticeable difference at 1.0. Calibration work is
generally held to 3.0.

Deliberately self-contained: it depends only on the PQ curve, so nothing here can create
an import cycle with measure.py, which is where the measurements come from.
"""

from __future__ import annotations

import math
from typing import Final, Iterable, Sequence

from .gamma_correction import pq_inverse_eotf

# CIE xy of D65. This must agree with measure.D65_XY; the two are kept separate so this
# module stays a leaf, and tests/test_delta_itp.py asserts they have not drifted apart.
D65_XY: Final[tuple[float, float]] = (0.3127, 0.3290)

# Normalised XYZ (D65) to the LMS cone responses ICtCp is built on. BT.2100 defines LMS from
# BT.2020 RGB; this is that matrix composed with BT.2020's RGB-to-XYZ, to four places.
# tests/test_golden_vectors.py derives it from the standard and holds this to 1.5e-4.
XYZ_TO_LMS: Final[tuple[tuple[float, float, float], ...]] = (
    (0.3592, 0.6976, -0.0358),
    (-0.1922, 1.1004, 0.0755),
    (0.0070, 0.0749, 0.8434),
)

# BT.2124 reports the difference on a scale where 1.0 is one JND.
ITP_SCALE: Final[float] = 720.0

# One JND. Below this a difference is not visible to a trained observer under reference
# conditions; 3.0 is the figure calibration work is usually held to.
JND: Final[float] = 1.0
GOOD: Final[float] = 3.0


def xyz_to_ictcp(xyz: Sequence[float]) -> tuple[float, float, float]:
    """Absolute XYZ in nits (Y is the luminance) to ICtCp.

    Absolute, not normalised: PQ is an absolute curve, so 100 nits of white and 1000
    nits of white are different colours here, which is the entire point of using it.
    """
    x, y, z = (float(value) for value in xyz)
    lms = tuple(
        row[0] * x + row[1] * y + row[2] * z
        for row in XYZ_TO_LMS
    )
    # Negative cone responses happen on saturated real-world measurements; PQ is not
    # defined there and clamping is what the spec's own implementations do.
    long_, medium, short = (pq_inverse_eotf(max(0.0, value)) for value in lms)

    intensity = 0.5 * long_ + 0.5 * medium
    ct = (6610.0 * long_ - 13613.0 * medium + 7003.0 * short) / 4096.0
    cp = (17933.0 * long_ - 17390.0 * medium - 543.0 * short) / 4096.0
    return (intensity, ct, cp)


def delta_itp(measured: Sequence[float], reference: Sequence[float]) -> float:
    """dEITP between two absolute XYZ triples, in JND units."""
    i1, ct1, cp1 = xyz_to_ictcp(measured)
    i2, ct2, cp2 = xyz_to_ictcp(reference)
    # BT.2124 works in ITP, where T is Ct/2 -- hence the quarter weight on the Ct term.
    return ITP_SCALE * math.sqrt(
        (i1 - i2) ** 2 + 0.25 * (ct1 - ct2) ** 2 + (cp1 - cp2) ** 2
    )


def neutral_xyz(nits: float, white_xy: Sequence[float] = D65_XY) -> tuple[float, float, float]:
    """Absolute XYZ for a neutral patch of a given luminance at a given white point."""
    x, y = (float(value) for value in white_xy)
    if y <= 0.0:
        raise ValueError("white point y must be positive")
    luminance = max(0.0, float(nits))
    return (luminance * x / y, luminance, luminance * (1.0 - x - y) / y)


def grey_delta_itp(
    reading_xyz: Sequence[float],
    intended_nits: float,
    white_xy: Sequence[float] = D65_XY,
) -> float:
    """dITP of one neutral patch against what the profile asked it to be.

    Against the intent, never against PQ. They are the same number only while every
    control sits neutral: the SDR-in-HDR correction deliberately darkens the low end, so
    scoring a reading against the PQ level reports a deliberate setting as a large error
    in exactly the part of the range someone scrutinises hardest.
    """
    return delta_itp(reading_xyz, neutral_xyz(intended_nits, white_xy))


def curve(
    points: Iterable[tuple[float, Sequence[float]]],
    white_xy: Sequence[float] = D65_XY,
) -> list[tuple[float, float]]:
    """(intended nits, measured XYZ) pairs -> (intended nits, dITP), lowest level first.

    Points asking for zero light are dropped rather than scored: PQ has no headroom
    below black, so they measure the meter's noise floor and not the display.
    """
    scored = [
        (float(nits), grey_delta_itp(xyz, float(nits), white_xy))
        for nits, xyz in points
        if float(nits) > 0.0
    ]
    scored.sort(key=lambda item: item[0])
    return scored
