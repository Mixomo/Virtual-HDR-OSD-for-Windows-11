from __future__ import annotations

import math
from typing import Callable, Final

CORRECTION_OPTIONS: Final[tuple[str, ...]] = (
    "Off",
    "Auto (Recommended)",
    "100 nits / Brightness 5",
    "200 nits / Brightness 30",
    "300 nits / Brightness 55",
    "400 nits / Brightness 80",
)

# Manual presets published by dylanraga. Auto is an app extension that reads the
# active Windows SDR reference white instead of exposing a duplicate brightness slider.
_PRESET_WHITE_NITS: Final[dict[str, float]] = {
    "100 nits / Brightness 5": 100.0,
    "200 nits / Brightness 30": 200.0,
    "300 nits / Brightness 55": 300.0,
    "400 nits / Brightness 80": 400.0,
}

# Retired names. "Unspecified" and "SDR" were filenames in the upstream download list
# rather than settings anyone would choose, and meant nothing in a dropdown. They are still
# resolved so a saved profile or state file naming one keeps working instead of silently
# falling back to a different correction than it was built with.
_RETIRED_WHITE_NITS: Final[dict[str, float]] = {
    "SDR": 80.0,
    "Unspecified": 200.0,
}

M1 = 0.1593017578125
M2 = 78.84375
C1 = 0.8359375
C2 = 18.8515625
C3 = 18.6875
SRGB_LINEAR_CUTOFF = 0.00313066844250063

# scRGB 1.0 is D65 at this luminance; Direct2D calls it SCENE_REFERRED_SDR_WHITE_LEVEL.
SCRGB_WHITE_NITS = 80.0


def pq_eotf(value: float) -> float:
    """ST 2084 code value -> absolute luminance in nits.

    NaN is black. ``min(1.0, nan)`` is 1.0, so the plain clamp turned a NaN into peak
    white, the worst answer for a display.
    """
    v = float(value)
    if math.isnan(v):
        return 0.0
    v = max(0.0, min(1.0, v))
    p = v ** (1.0 / M2)
    return 10000.0 * (max(p - C1, 0.0) / (C2 - C3 * p)) ** (1.0 / M1)


def pq_inverse_eotf(luminance_nits: float) -> float:
    """Absolute luminance in nits -> ST 2084 code value, within 0..1.

    Clamped at 10,000 nits, the top of the curve: above it the formula returned codes
    past 1.0, and at infinity NaN. NaN is no light, as it always was; it is checked
    first because ``min(10000.0, nan)`` is 10000.0.
    """
    l = float(luminance_nits)
    if math.isnan(l):
        l = 0.0
    l = max(0.0, min(10000.0, l)) / 10000.0
    return ((C1 + C2 * l**M1) / (1.0 + C3 * l**M1)) ** M2


def srgb_inverse_eotf(linear: float) -> float:
    """Linear light -> piecewise sRGB signal value (upstream srgbInvEotf)."""
    x = max(0.0, float(linear))
    return x * 12.92 if x <= SRGB_LINEAR_CUTOFF else 1.055 * x ** (1.0 / 2.4) - 0.055


def resolve_white_level(option: str, windows_white_nits: float | None) -> float | None:
    if option == "Off":
        return None
    if option == "Auto (Recommended)":
        if windows_white_nits is not None and math.isfinite(windows_white_nits):
            return max(80.0, min(480.0, float(windows_white_nits)))
        return 200.0
    if option in _PRESET_WHITE_NITS:
        return _PRESET_WHITE_NITS[option]
    return _RETIRED_WHITE_NITS.get(option, 200.0)


def transform_piecewise_srgb_to_gamma(
    pq_input: float,
    white_level_nits: float,
    target_gamma: float = 2.2,
    shape: Callable[[float], float] | None = None,
) -> float:
    """Port of dylanraga's current NVIDIA LUT generator direction.

    PQ input -> absolute luminance -> piecewise-sRGB signal relative to SDR white
    -> reinterpret that signal through a pure power curve -> absolute luminance -> PQ.
    Values above diffuse SDR white are left untouched.

    ``target_gamma`` is the curve the SDR range is reinterpreted through. dylanraga's web
    generator exposes it for the same reason it is exposed here: it is the one parameter
    of the correction a user might genuinely want to move, and moving it *inside* the
    correction keeps everything above diffuse white at exact identity. Applying a separate
    power afterwards does not -- see :func:`~.curves._shape_curve`.

    ``shape`` is the same idea for any other tone control: a function on the relative SDR
    signal, applied before the power curve. It must hold 0 at 0 and 1 at 1, which is what
    keeps diffuse white where it is and the curve continuous where the SDR range meets the
    identity above it.
    """
    x = max(0.0, min(1.0, float(pq_input)))
    if x <= 0.0:
        return 0.0
    white = max(1.0, float(white_level_nits))
    luminance = pq_eotf(x)
    if luminance > white:
        return x
    srgb_signal = srgb_inverse_eotf(luminance / white)
    if shape is not None:
        srgb_signal = max(0.0, min(1.0, shape(srgb_signal)))
    gamma_luminance = white * srgb_signal ** max(0.1, float(target_gamma))
    return max(0.0, min(1.0, pq_inverse_eotf(gamma_luminance)))



transform_piecewise_srgb_to_gamma22 = transform_piecewise_srgb_to_gamma


def transform_piecewise_srgb_gamma_adjustment(
    pq_input: float, white_level_nits: float, gamma: float = 2.2,
) -> float:
    """Relative desktop gamma with piecewise sRGB retained as the neutral baseline.

    Decode PQ, recover desktop sRGB signal, reshape that signal relative to 2.2,
    decode sRGB again, then encode PQ. This does not enable the pure-power fix.
    White and native HDR above white stay fixed; gamma 2.2 is exact identity.
    """
    x = max(0.0, min(1.0, float(pq_input)))
    if abs(float(gamma) - 2.2) < 1e-12 or x <= 0.0:
        return x
    white = max(1.0, float(white_level_nits))
    luminance = pq_eotf(x)
    if luminance >= white:
        return x
    signal = srgb_inverse_eotf(luminance / white)
    adjusted = signal ** max(0.1, float(gamma) / 2.2)
    linear = adjusted / 12.92 if adjusted <= 0.04045 else ((adjusted + 0.055) / 1.055) ** 2.4
    return pq_inverse_eotf(white * linear)
