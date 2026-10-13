"""Turning a measured greyscale ramp into the three curves MHC2 carries.

MHC2 is a matrix followed by three independent 1-D LUTs indexed by PQ code. That is a
matrix-shaper, and it decides exactly what a measurement can and cannot fix. Tone
response and grey tracking are per-code, per-channel quantities, so they are squarely
inside what the tag can express. Saturation tracking is not: no matrix-shaper represents
an error that depends on hue *and* saturation, which is why the colour sweeps this app
measures are reported and never applied.

**What is corrected, and against what.** Two separate targets, deliberately:

*Luminance* is corrected against an absolute one. A code means a luminance under
ST.2084, so the target for input code ``c`` is ``pq_eotf(c)`` and nothing about the
display enters into it. This is the whole of EOTF tracking.

*Balance* is corrected against the reference white, not against D65. The correction is
therefore zero at the reference level by construction, and what it removes is the
*drift* of grey away from that white as level changes -- which is precisely what a
multi-point control adds over a two-point one. Absolute white balance stays where it
already was, in the MHC2 matrix, so the two never fight and a second pass does not
undo the first.

**Above the measured peak nothing is corrected.** The panel is rolling off there and we
do not know how; sending it the code for its own maximum would replace whatever it does
with a hard clip. The correction is faded out to nothing between the measured peak and
code 1.0 instead, which also keeps the curve continuous. Everything here follows from
the same limit a Calman HDR10 run works under: the target is PQ up to the measured peak
and undefined above it.

The response describes the panel, not the correction on top of it -- each sample pairs
a channel's delivered luminance with the code that channel was *actually sent*, after
the LUT in force at measurement time. So a later pass replaces it rather than composing
with it, and cannot double-apply.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gamma_correction import pq_eotf, pq_inverse_eotf

#: Floats per stored sample: red code, red nits, green code, green nits, blue code,
#: blue nits. Flat because ModeState serialises through ``asdict`` into JSON, and a
#: flat tuple is what ``panel_primaries`` already established for this.
RESPONSE_STRIDE = 6

#: Below this many usable samples a ramp is not a curve, it is a few points with a line
#: drawn through them. A full run gives 33; anything this short means most of the run
#: failed validation, and inventing a transfer function from the remainder would be
#: worse than leaving the display alone.
MIN_RESPONSE_POINTS = 8

#: How far a correction may move a code, as a backstop against a bad measurement rather
#: than a working limit -- a real correction is a small fraction of this. PQ is steep:
#: 0.10 of code is already more than a factor of two in luminance through the midrange,
#: so a response that wants more than this is not describing a display that was measured
#: properly, and clamping keeps the damage bounded instead of unbounded.
MAX_CODE_SHIFT = 0.10

#: How much brighter a ramp point must read than everything before it to count as the
#: panel still rising rather than the meter wobbling on a clipped plateau. A genuine step
#: near the top of the ramp is around 20% in luminance, so no real point is ever dropped;
#: a plateau that varies by a few tenths of a percent is.
PLATEAU_MARGIN = 0.005


@dataclass(frozen=True, slots=True)
class PanelResponse:
    """What each channel delivered for the code it was sent, plus the white it holds to.

    ``weights`` are the fractions of the reference white's luminance contributed by red,
    green and blue. They sum to 1 and they are what "neutral" means here.
    """

    red: tuple[tuple[float, float], ...]
    green: tuple[tuple[float, float], ...]
    blue: tuple[tuple[float, float], ...]
    weights: tuple[float, float, float]

    @property
    def usable(self) -> bool:
        """Whether there is enough here to correct from."""
        return (
            len(self.red) >= MIN_RESPONSE_POINTS
            and len(self.red) == len(self.green) == len(self.blue)
            and abs(sum(self.weights) - 1.0) < 1e-3
            and all(weight > 0.0 for weight in self.weights)
        )

    @property
    def measured_peak_nits(self) -> float:
        """The brightest neutral the ramp reached, as the three channels sum to it."""
        if not (self.red and self.green and self.blue):
            return 0.0
        return max(
            red[1] + green[1] + blue[1]
            for red, green, blue in zip(self.red, self.green, self.blue)
        )


def to_values(response: PanelResponse) -> tuple[float, ...]:
    """Flatten for storage. The inverse of :func:`from_values`."""
    values: list[float] = []
    for red, green, blue in zip(response.red, response.green, response.blue):
        values.extend((red[0], red[1], green[0], green[1], blue[0], blue[1]))
    return tuple(values)


def from_values(
    values: object, weights: object
) -> PanelResponse | None:
    """Rebuild a response from storage, or ``None`` if it does not survive the trip.

    Returning ``None`` rather than an empty response matters: an empty one would be a
    claim that the panel delivers nothing, and callers treat "no response" and "a
    response saying zero" very differently.
    """
    try:
        numbers = [float(value) for value in values]  # type: ignore[union-attr]
        share = tuple(float(value) for value in weights)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    if len(share) != 3 or len(numbers) < RESPONSE_STRIDE:
        return None
    if not all(value > 0.0 for value in share):
        return None

    total = sum(share)
    if total <= 0.0:
        return None
    share = tuple(value / total for value in share)

    usable = len(numbers) - len(numbers) % RESPONSE_STRIDE
    red: list[tuple[float, float]] = []
    green: list[tuple[float, float]] = []
    blue: list[tuple[float, float]] = []
    for start in range(0, usable, RESPONSE_STRIDE):
        row = numbers[start : start + RESPONSE_STRIDE]
        if any(value != value or value in (float("inf"), float("-inf")) for value in row):
            return None
        red.append((_clamp(row[0]), max(0.0, row[1])))
        green.append((_clamp(row[2]), max(0.0, row[3])))
        blue.append((_clamp(row[4]), max(0.0, row[5])))

    response = PanelResponse(tuple(red), tuple(green), tuple(blue), share)  # type: ignore[arg-type]
    return response if response.usable else None


def correct(
    shaped: list[float], response: PanelResponse
) -> tuple[list[float], list[float], list[float]]:
    """Warp one intent curve into three that make the panel deliver what a code means.

    ``shaped`` is what the controls asked for: input code in, output code out, with the
    display not yet considered. Everything the panel gets wrong is layered on after it,
    so moving a slider still does what it always did and the measurement decides only
    how much drive that intent needs.
    """
    if not shaped or not response.usable:
        return (list(shaped), list(shaped), list(shaped))

    inverses = [
        _Inverse(channel) for channel in (response.red, response.green, response.blue)
    ]
    if not all(inverse.usable for inverse in inverses):
        return (list(shaped), list(shaped), list(shaped))

    peak = response.measured_peak_nits
    # Where the panel's own maximum falls on the input axis. Above it the display is
    # rolling off by rules we did not measure and cannot infer, so the correction is
    # faded away rather than extrapolated.
    peak_code = _clamp(pq_inverse_eotf(peak)) if peak > 0.0 else 1.0
    curves: list[list[float]] = [[], [], []]

    # Above the peak the correction is *held* at what it was there and faded out, not
    # solved afresh. Solving asks for a luminance the ramp never reached, so the inverse
    # can only answer with the top code it knows -- a shift that grows with every step
    # past the peak and pulls the top of the range down towards the panel's maximum.
    # That is the hard clip this is supposed to avoid, arrived at from the other side.
    #
    # Faded on the *intent* rather than the input code, because what decides whether the
    # ramp has anything to say is the luminance being asked for, not where the request
    # came from. A control that darkens the top of the range pulls a request back inside
    # what was measured, and keying on the input code would stop correcting a level the
    # ramp covers perfectly well. The two are the same whenever the controls are neutral.
    held = [0.0, 0.0, 0.0]
    for intent in shaped:
        wanted = pq_eotf(_clamp(intent))
        fade = _fade(_clamp(intent), peak_code)
        for channel, inverse in enumerate(inverses):
            if fade >= 1.0:
                solved = inverse.code_for(wanted * response.weights[channel])
                held[channel] = max(
                    -MAX_CODE_SHIFT, min(MAX_CODE_SHIFT, solved - intent)
                )
            curves[channel].append(_clamp(intent + held[channel] * fade))

    return tuple(_monotonic(curve) for curve in curves)  # type: ignore[return-value]


def sample(curve: list[float], code: float) -> float:
    """Read a built curve at an arbitrary code, interpolating between entries.

    A LUT is a sampled function and callers ask it about codes that fall between
    samples. Taking the nearest entry instead quantises to 1/4095 of the range, which
    is invisible in a gradient and very visible in a measurement solved from it.
    """
    if not curve:
        return _clamp(code)
    position = _clamp(code) * (len(curve) - 1)
    low = int(position)
    high = min(len(curve) - 1, low + 1)
    return curve[low] + (position - low) * (curve[high] - curve[low])


def _fade(code: float, peak_code: float) -> float:
    """1 below the measured peak, falling to 0 at code 1.0."""
    if code <= peak_code:
        return 1.0
    if peak_code >= 1.0:
        return 0.0
    return max(0.0, 1.0 - (code - peak_code) / (1.0 - peak_code))


class _Inverse:
    """One channel's response, read backwards: luminance in, the code for it out.

    Interpolation is in PQ rather than in nits. Luminance against code is close to
    exponential, so a straight line between two samples in nits sits well below the
    curve it is standing in for -- most of the error landing in the shadows, where the
    ramp is densest and the correction matters most.
    """

    def __init__(self, samples: tuple[tuple[float, float], ...]) -> None:
        # Sorted by code and made non-decreasing in luminance. A ramp that dips is
        # measurement noise, not a panel that gets darker when driven harder, and an
        # inverse over a non-monotonic curve is not a function.
        ordered = sorted(samples, key=lambda sample: sample[0])
        codes: list[float] = [0.0]
        levels: list[float] = [0.0]
        highest = 0.0
        for code, nits in ordered:
            if code <= codes[-1]:
                continue
            if nits <= highest * (1.0 + PLATEAU_MARGIN):
                # The panel has stopped rising. Every run plans the ramp to what the EDID
                # claims and most panels stop well short of it, so the top of a real ramp
                # is a plateau of clipped readings -- 1015 asked for, about 460 delivered,
                # on the display this was developed against.
                #
                # Keeping those gave the table entries whose code climbs against an
                # unchanged level, so code_for's "beyond what was measured" branch handed
                # back the top ramp code for every level at or above the clip. A channel
                # that happened to be drifting when it clipped then carried that drift as
                # one large fixed shift across the whole highlight range: measured here at
                # +0.0697 for blue against +0.0084 for red and green, a 0.061 spread where
                # everything below the clip agrees to 0.0015.
                #
                # The first clipped sample survives this test on its own merits -- it is a
                # genuine rise, the last real step the panel took -- so levels[-1] still
                # equals the clip level and still lines up with the peak_code the fade in
                # correct() is keyed on. No gap opens between the table and the fade.
                continue
            highest = nits
            codes.append(code)
            levels.append(highest)
        self._codes = codes
        self._levels = [pq_inverse_eotf(level) for level in levels]
        self.usable = len(codes) > MIN_RESPONSE_POINTS and levels[-1] > 0.0

    def code_for(self, nits: float) -> float:
        """The code this channel must be sent to deliver ``nits``."""
        if nits <= 0.0:
            return 0.0
        wanted = pq_inverse_eotf(nits)
        if wanted >= self._levels[-1]:
            # Beyond what was measured. The caller fades the correction out up here, so
            # holding at the last known code keeps the join continuous.
            return self._codes[-1]
        for index in range(1, len(self._levels)):
            if wanted <= self._levels[index]:
                low, high = self._levels[index - 1], self._levels[index]
                span = high - low
                if span <= 0.0:
                    return self._codes[index]
                position = (wanted - low) / span
                return self._codes[index - 1] + position * (
                    self._codes[index] - self._codes[index - 1]
                )
        return self._codes[-1]


def _monotonic(curve: list[float]) -> list[float]:
    """Non-decreasing, and pinned at both ends.

    The pins are the part that earns its place. When a bad reading puts the measured
    peak past the top of PQ there is no fade region at all, so the last entry is
    corrected like any other and the top code stops addressing the top of the range --
    measured at 0.900 instead of 1.0 from a response 40x too bright. Black has to stay
    black for the same reason at the other end.

    The ordering pass is a backstop rather than a repair: ``_Inverse`` already forces
    its table non-decreasing, so a curve solved from it does not go backwards, and no
    response tried here has made one. It stays because a LUT that inverts a gradient is
    a visible fault and the check costs one comparison per entry.
    """
    if not curve:
        return curve
    highest = 0.0
    for index, value in enumerate(curve):
        highest = max(highest, _clamp(value))
        curve[index] = highest
    curve[0] = 0.0
    curve[-1] = 1.0
    return curve


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value
