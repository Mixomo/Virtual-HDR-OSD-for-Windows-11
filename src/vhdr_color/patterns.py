"""Calibration test patterns, rendered as scRGB frames for :mod:`hdr_display`.

Every pattern is specified in **absolute nits** and encoded at render time, because what
a value means depends on the display:

* On an HDR output, scRGB is scene-referred and 1.0 is 80 nits, so a luminance maps
directly and patterns can address the full ST.2084 range.
* On an SDR output, scRGB is display-referred and 1.0 is that display's reference white.
Absolute luminance is simply not addressable, so the same pattern is rendered relative
to reference white and anything above it clips. :attr:`PatternContext.absolute` says
which of the two happened, so the UI can label a reading as real nits or as a ratio
rather than quietly implying precision it does not have.

scRGB is linear light, which matters for more than encoding: the gamma-match pattern
below relies on two interleaved lines averaging optically to their arithmetic mean, and
that is only true in a linear signal.

Rendering happens straight into a swapchain buffer sized in *device* pixels. That is not
an incidental detail -- a display at 125% scaling would resample a one-pixel line drawn
through Qt, and a resampled gamma-match pattern reports a gamma that is simply wrong.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Callable, Sequence

from .gamma_correction import SCRGB_WHITE_NITS, pq_eotf, pq_inverse_eotf, srgb_inverse_eotf

# Diffuse white for HDR reference content. BT.2408 uses 203 nits for graphics white, and
# it is the level most HDR mastering treats as paper white.
REFERENCE_WHITE_NITS = 203.0


@dataclass(frozen=True)
class PatternContext:
    """Everything a pattern needs to know about where it is being shown."""

    is_hdr: bool
    sdr_white_nits: float = 240.0
    peak_nits: float = 1000.0
    max_full_frame_nits: float = 600.0
    # The level a threshold pattern is currently probing at. Windows HDR Calibration
    # works this way and it is the right way round: the eye cannot say what luminance a
    # patch is, but it can say whether a shape is visible, so the pattern moves until the
    # shape disappears and the level where that happens is the reading. It also means a
    # meter can drive exactly the same pattern by stepping this value.
    probe_nits: float = 0.05
    gamma_white_nits: float | None = None
    gamma_view: str = "profile"
    reference_gamma: float = 2.2
    gamma_average_preview: bool = False

    @property
    def absolute(self) -> bool:
        """Whether values shown are real luminance rather than a ratio to white."""
        return self.is_hdr

    @property
    def ceiling_nits(self) -> float:
        """The brightest luminance worth asking for on this display.

        Above the panel's peak everything is rolled off or clipped, so a pattern that
        keeps climbing past it invites chasing a difference that cannot exist.
        """
        return self.peak_nits if self.is_hdr else self.sdr_white_nits

    def encode(self, nits: float) -> float:
        """Absolute luminance to an scRGB channel value for this display."""
        nits = max(0.0, float(nits))
        if self.is_hdr:
            return nits / SCRGB_WHITE_NITS
        # Display-referred: 1.0 is this display's white, and nothing exceeds it.
        return min(1.0, nits / max(1.0, self.sdr_white_nits))


def _pixel(value: float) -> bytes:
    return struct.pack("<4e", value, value, value, 1.0)


def _colour_pixel(red: float, green: float, blue: float) -> bytes:
    return struct.pack("<4e", red, green, blue, 1.0)


def _row(width: int, value: float) -> bytes:
    return _pixel(value) * width


def _spans(width: int, count: int) -> list[int]:
    """Split ``width`` into ``count`` spans summing to exactly ``width``.

    Giving the remainder to the last cell looks fine until the surface is narrower than
    the cell count, at which point that cell gets a negative width, silently renders as
    nothing, and the frame comes out the wrong size. Cells are dropped instead, and the
    remainder is spread over the leading cells so no single one is visibly wider.
    """
    count = max(1, min(int(count), max(1, int(width))))
    base, remainder = divmod(int(width), count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


@dataclass(frozen=True)
class Marker:
    """A label drawn into the pattern itself, positioned as a fraction of the window.

    Patterns without these are unusable by eye. A grey ramp shows what the display is
    doing but says nothing about what it should be doing, and a viewer with no target is
    not calibrating, they are looking at grey.
    """

    text: str
    x: float
    y: float
    target: bool = False


@dataclass(frozen=True)
class Pattern:
    """One test pattern, plus everything needed to act on what it shows.

    ``criterion`` is the single sentence describing what correct looks like. It is
    separate from ``instructions`` because it is the thing a user checks against, and
    burying it in a paragraph is how a pattern ends up being stared at rather than read.

    ``level_driven`` patterns are the ones where the *pattern* moves rather than the
    display: the user drives ``PatternContext.probe_nits`` until a shape disappears, and
    that level is the measurement. Fixed patterns are the ones where the display moves and
    the pattern holds still.
    """

    key: str
    title: str
    purpose: str
    criterion: str
    instructions: str
    render: Callable[[int, int, PatternContext], bytes]
    markers: Callable[[PatternContext], tuple[Marker, ...]] = lambda _context: ()
    level_driven: bool = False
    # Whether the level this pattern is driven to is an answer worth keeping. Full-frame
    # white is driven exactly like a measurement and is not one: what it finds is where the
    # signal clips, which is the same wherever the window size, so recording it would put a
    # clipping point in a field meaning sustained luminance.
    records: bool = True
    # Overrides the standard window. Only maximum full-frame luminance needs this: it is
    # defined as the whole screen lit, so measuring it in a tenth of one measures nothing.
    window_fraction: float | None = None
    # Levels this pattern is walked through one at a time, if any. Showing a whole range
    # at once is not an option for anything judged near threshold: adaptation follows the
    # brightest thing in view, so the dark end becomes unreadable regardless of the panel.
    levels: Callable[[PatternContext], tuple[float, ...]] | None = None


_NEAR_BLACK_LEVELS: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0)

# How much brighter the shape is than its surround in the clipping tests. This is the
# sensitivity of the measurement: the two merge once the display can no longer keep them
# apart, so a large gap only merges long after the real limit. 20% was far too coarse and
# put the threshold well above where a panel actually runs out.
SHAPE_CONTRAST = 1.08

# Relative sizes of the candidate patches in the gamma-match pattern. The middle entry is
# the true half-luminance, so it is the answer; the others exist so a miss reads as a
# direction rather than merely a failure.
GAMMA_CANDIDATES: tuple[tuple[float, str], ...] = (
    (0.70, "much darker"),
    (0.85, "darker"),
    (1.00, "TARGET"),
    (1.18, "brighter"),
    (1.40, "much brighter"),
)


def _disc_spans(width: int, height: int, diameter_fraction: float):
    """Per-row (start, span) of a centred disc, or None for rows it does not touch.

    A shape rather than a bar, because the question a threshold pattern asks is "can you
    see it", and a recognisable outline is far easier to answer that about than an edge
    which might be a gradient.
    """
    centre_x, centre_y = (width - 1) / 2.0, (height - 1) / 2.0
    radius = min(width, height) * max(0.0, min(1.0, diameter_fraction)) / 2.0
    for y in range(height):
        offset = y - centre_y
        if radius <= 0.0 or abs(offset) > radius:
            yield None
            continue
        half = (radius * radius - offset * offset) ** 0.5
        start = max(0, int(round(centre_x - half)))
        end = min(width, int(round(centre_x + half)) + 1)
        yield (start, end - start) if end > start else None


def _shape_on_field(width: int, height: int, field: float, shape: float) -> bytes:
    background = _row(width, field)
    rows: list[bytes] = []
    for span in _disc_spans(width, height, 0.55):
        if span is None:
            rows.append(background)
            continue
        start, count = span
        rows.append(
            background[: start * 8] + _pixel(shape) * count + background[(start + count) * 8:]
        )
    return b"".join(rows)


# ---------------------------------------------------------------------------------
# Individual patterns. Each returns width*height*8 bytes of scRGB half floats.


def _render_black_level(width: int, height: int, context: PatternContext) -> bytes:
    """A shape barely above black. The level where it vanishes is the black threshold.

    This is the pattern that moves rather than the display. The eye cannot say what
    luminance it is looking at, but it is very good at saying whether a shape is there,
    so lowering the probe until the shape disappears converts a judgement nobody can make
    into one everybody can.
    """
    return _shape_on_field(width, height, 0.0, context.encode(context.probe_nits))


def _render_peak_clip(width: int, height: int, context: PatternContext) -> bytes:
    """A shape slightly brighter than a bright surround, to find where the panel clips.

    Raising both together, the shape separates from its surround until the display runs
    out of range, at which point the two clip to the same output and the shape disappears.
    That level is peak luminance, measured rather than taken from metadata that on this
    kind of panel is a specification.
    """
    field = context.probe_nits
    return _shape_on_field(
        width, height, context.encode(field), context.encode(field * SHAPE_CONTRAST)
    )


def gamma_reference_nits(context: PatternContext) -> float:
    if context.gamma_view != "profile":
        return min(context.ceiling_nits, context.sdr_white_nits)
    reference = min(context.ceiling_nits, REFERENCE_WHITE_NITS)
    white = context.gamma_white_nits
    if white is not None and math.isfinite(white) and white > 0:
        reference = min(reference, white)
    return reference


def gamma_target_nits(context: PatternContext) -> float:
    reference = gamma_reference_nits(context)
    if context.gamma_view != "profile":
        # Browser RGB is nonlinear sRGB; scRGB must contain its decoded signal.
        return reference * _srgb_to_linear(
            ((0.25 ** context.reference_gamma + 0.75 ** context.reference_gamma) / 2.0) ** (1.0 / context.reference_gamma)
            if context.gamma_view == "match" else 0.5 ** (1.0 / context.reference_gamma))
    white = context.gamma_white_nits
    if white is None or not math.isfinite(white) or white <= 0:
        return reference / 2.0
    # A fixed neutral 2.2 target, not the slider's current value. The active SDR
    # correction reinterprets sRGB signal through a power curve. Encode the
    # neutral half-output back into its input so 2.2 matches on an ideal panel.
    signal = srgb_inverse_eotf(reference / white) * 0.5 ** (1.0 / 2.2)
    return white * _srgb_to_linear(signal)


def _render_gamma_match(width: int, height: int, context: PatternContext) -> bytes:
    """Interleaved lines beside solid patches: the honest way to read actual gamma.

    Alternating full and black device-pixel lines average optically to half the line
    luminance, and because scRGB is linear that average is exactly arithmetic. Whichever
    solid patch blends into the surrounding lines identifies the luminance the display is
    really producing for that signal, which is a measurement of the transfer function
    rather than an impression of it.

    The lines must land on real device pixels. Any resampling averages them together and
    the pattern then reports a gamma that was never on screen.
    """
    if context.gamma_view == "rows":
        return _render_gamma_rows(width, height, context)
    line_nits = gamma_reference_nits(context)
    if context.gamma_view == "match":
        high = line_nits * _srgb_to_linear(0.75)
        low = line_nits * _srgb_to_linear(0.25)
    else:
        high, low = line_nits, 0.0
    lit_pixel, dark_pixel = _pixel(context.encode(high)), _pixel(context.encode(low))
    # A one-device-pixel checkerboard averages in both directions. Horizontal
    # lines alone are more sensitive to viewing direction and panel row inversion.
    lit = (lit_pixel + dark_pixel) * (width // 2) + (lit_pixel if width % 2 else b"")
    dark = (dark_pixel + lit_pixel) * (width // 2) + (dark_pixel if width % 2 else b"")

    # Candidate patches bracket the true half-luminance so a mismatch reads as a
    # direction, not just a failure to match.
    ideal = gamma_target_nits(context)
    candidates = ([ideal] if context.gamma_view == "match" else
                  [ideal * factor for factor, _label in GAMMA_CANDIDATES])
    patch_width = max(1, width // 4)
    band = max(1, height // (len(candidates) + 1))

    rows: list[bytes] = []
    for y in range(height):
        index = (y - band // 2) // band
        base = lit if y % 2 == 0 else dark
        if context.gamma_view == "match" and context.gamma_average_preview:
            # Linear average is a preview only: it removes the spatial reference
            # needed to measure a display's nonlinear transfer function.
            base = _pixel(context.encode((high + low) / 2.0)) * width
        if 0 <= index < len(candidates) and (y - band // 2) % band < band * 2 // 3:
            left = (width - patch_width) // 2
            patch = _pixel(context.encode(candidates[index])) * patch_width
            rows.append(base[: left * 8] + patch + base[(left + patch_width) * 8:])
        else:
            rows.append(base)
    return b"".join(rows)


def _render_gamma_rows(width: int, height: int, context: PatternContext) -> bytes:
    """Original RGB-power reference rows, decoded once for the linear swapchain."""
    rows = []
    white = gamma_reference_nits(context)
    gammas = (1.8, 2.0, 2.2, 2.4, context.reference_gamma)
    for y in range(height):
        gamma = gammas[min(4, y * 5 // height)]
        spans = _spans(width, 6)
        row = b"".join(_pixel(context.encode(white * _srgb_to_linear(
            (index / max(1, len(spans) - 1)) ** (1.0 / gamma)))) * span
            for index, span in enumerate(spans))
        rows.append(row)
    return b"".join(rows)


def _render_grey_staircase(width: int, height: int, context: PatternContext) -> bytes:
    """Even perceptual steps from black to peak: crush, clipping and banding at a glance.

    Steps are spaced evenly in PQ rather than in luminance, because PQ is roughly
    perceptually uniform; evenly spaced *nits* would waste almost every step on
    highlights and show nothing where the eye is sensitive.
    """
    steps = 16
    spans = _spans(width, steps)
    top = pq_inverse_eotf(context.ceiling_nits)
    divisor = max(1, len(spans) - 1)
    row = b"".join(
        _pixel(context.encode(pq_eotf(top * (index / divisor)))) * span
        for index, span in enumerate(spans)
    )
    return row * height


def _render_near_black(width: int, height: int, context: PatternContext) -> bytes:
    """Shadow patches on black, to find where detail stops being distinguishable.

    The SDR-in-HDR correction takes 0.5 nits to roughly 0.1, so this is the range it
    changes most and the range where a wrong black level is most visible.
    """
    levels = [nits for nits in _NEAR_BLACK_LEVELS if nits <= context.ceiling_nits]
    levels = levels or [context.ceiling_nits]
    spans = _spans(width, len(levels))
    margin = height // 4

    patch_row = b"".join(
        _pixel(context.encode(level)) * span for level, span in zip(levels, spans)
    )
    black = _row(width, 0.0)
    return b"".join(black if y < margin or y >= height - margin else patch_row for y in range(height))


def _render_neutral_ramp(width: int, height: int, context: PatternContext) -> bytes:
    """A smooth grey sweep. Absolute white balance cannot be judged by eye, but a tint
    that *drifts* across the range -- green shadows into magenta highlights -- is obvious,
    and that is exactly what the per-channel trims exist to correct."""
    top = pq_inverse_eotf(context.ceiling_nits)
    row = b"".join(
        _pixel(context.encode(pq_eotf(top * (x / max(1, width - 1)))))
        for x in range(width)
    )
    return row * height


# How far above its own background each tracking patch sits, in PQ code.
#
# This has to sit near the threshold of visibility to be worth anything. What the eye sees
# is the transfer function's slope at that level -- the difference is roughly f'(c) times
# this step -- so the patch reports how steep the curve is where it sits. A step well above
# threshold produces an obvious block, and an obvious block stays obvious when the curve
# moves, which makes the pattern useless for judging a change. The first version used 0.03,
# roughly thirty times threshold, and patches 1.3x to 2.35x brighter than their surround:
# unmistakable, and almost completely insensitive to the controls it exists to set.
#
# One just-noticeable step is about one to two ten-bit PQ codes. Four is faint but findable,
# and near enough to threshold that a change in slope pushes a patch in or out of sight.
TONE_TRACKING_DELTA_PQ = 0.004

TONE_TRACKING_CELLS = 7


def tone_tracking_levels(context: PatternContext) -> tuple[float, ...]:
    """The background levels the tracking test walks through, dark to bright."""
    top = pq_inverse_eotf(context.ceiling_nits)
    count = max(1, TONE_TRACKING_CELLS - 1)
    # Kept off both ends: a patch at the very bottom has nothing to sit below it, and one
    # at the top would clip against the ceiling and read as a tracking error.
    return tuple(
        pq_eotf(top * (0.10 + 0.80 * (index / count))) for index in range(TONE_TRACKING_CELLS)
    )


def _render_tone_tracking(width: int, height: int, context: PatternContext) -> bytes:
    """One level at a time: a faint patch on a field, for setting the tone controls.

    Gamma, Midtone Brightness and Contrast all shape the same curve, and by eye none has
    an absolute reference to judge against. What the eye can do is say whether a patch is
    visible, and the patch sits a near-threshold step above its background, so what it
    reports is the steepness of the transfer function at that level.

    Showing every level at once does not work, and the first version did. Adaptation is
    set by the brightest thing in view, so a 536-nit field in the corner of the eye makes
    a 0.16-nit patch unjudgeable no matter what the display is doing -- the reading is
    then about the viewer, not the panel. One field at a time, stepped through, lets the
    eye settle at each level.
    """
    field = context.probe_nits
    patch = pq_eotf(min(1.0, pq_inverse_eotf(field) + TONE_TRACKING_DELTA_PQ))
    background = _row(width, context.encode(field))

    # A bar rather than a disc: an edge is easier to catch at threshold than a curve.
    top = height // 3
    bottom = height - top
    left = width // 3
    span = max(1, width - left * 2)
    lit = (
        background[: left * 8]
        + _pixel(context.encode(patch)) * span
        + background[(left + span) * 8:]
    )
    return b"".join(lit if top <= y < bottom else background for y in range(height))


def _tone_tracking_markers(context: PatternContext) -> tuple[Marker, ...]:
    levels = tone_tracking_levels(context)
    closest = min(range(len(levels)), key=lambda i: abs(levels[i] - context.probe_nits))
    reading = (f"{context.probe_nits:.3g} nits" if context.absolute
               else f"{context.probe_nits / max(1.0, context.ceiling_nits):.3f}")
    return (
        Marker(text=f"level {closest + 1} of {len(levels)}", x=0.5, y=0.12, target=True),
        Marker(text=reading, x=0.5, y=0.92),
    )


def _render_solid_patch(width: int, height: int, context: PatternContext) -> bytes:
    """A single flat level filling the window: the patch a meter reads."""
    return _row(width, context.encode(context.probe_nits)) * height


# ---------------------------------------------------------------------------------
# Markers. Positions are fractions of the window, so they follow it at any size.


def _gamma_markers(_context: PatternContext) -> tuple[Marker, ...]:
    """Name every candidate patch, and say plainly which one is the answer."""
    if _context.gamma_view == "rows":
        return tuple(Marker(f"Gamma {gamma:g}" if index < 4 else f"CUSTOM {gamma:.3f}", 0.5, (index + 0.5) / 5)
                     for index, gamma in enumerate((1.8, 2.0, 2.2, 2.4, _context.reference_gamma)))
    if _context.gamma_view == "match":
        return (Marker("TARGET", 0.80, 0.25 + 0.5 * 0.33, target=True),)
    count = len(GAMMA_CANDIDATES)
    band = 1.0 / (count + 1)
    return tuple(
        Marker(
            text=label,
            x=0.80,
            y=band * 0.5 + index * band + band * 0.33,
            target=(label == "TARGET"),
        )
        for index, (_factor, label) in enumerate(GAMMA_CANDIDATES)
    )


def _near_black_markers(context: PatternContext) -> tuple[Marker, ...]:
    levels = [nits for nits in _NEAR_BLACK_LEVELS if nits <= context.ceiling_nits]
    levels = levels or [context.ceiling_nits]
    step = 1.0 / len(levels)
    return tuple(
        Marker(text=f"{nits:g}", x=step * (index + 0.5), y=0.80)
        for index, nits in enumerate(levels)
    )


def _probe_markers(context: PatternContext) -> tuple[Marker, ...]:
    reading = f"{context.probe_nits:.4g} nits" if context.absolute else f"{context.probe_nits:.4g}"
    return (Marker(text=reading, x=0.5, y=0.92, target=True),)


def _staircase_markers(context: PatternContext) -> tuple[Marker, ...]:
    top = f"{context.ceiling_nits:.0f} nits" if context.absolute else "white"
    return (
        Marker(text="black", x=0.03, y=0.90),
        Marker(text=top, x=0.90, y=0.90),
    )


# Primaries, secondaries, and two memory colours the eye judges harshly.
_COLOUR_PATCHES: Sequence[tuple[str, tuple[float, float, float]]] = (
    ("red", (1.0, 0.0, 0.0)),
    ("green", (0.0, 1.0, 0.0)),
    ("blue", (0.0, 0.0, 1.0)),
    ("cyan", (0.0, 1.0, 1.0)),
    ("magenta", (1.0, 0.0, 1.0)),
    ("yellow", (1.0, 1.0, 0.0)),
    ("skin", (0.87, 0.66, 0.55)),
    ("sky", (0.35, 0.55, 0.85)),
    ("foliage", (0.35, 0.60, 0.30)),
    ("neutral", (1.0, 1.0, 1.0)),
)


def _render_colour_patches(width: int, height: int, context: PatternContext) -> bytes:
    """Saturation and hue sanity, held at diffuse white rather than at peak.

    Judging colour at peak luminance mostly measures the panel's highlight rolloff; at
    reference white it measures the colour itself.
    """
    level = context.encode(min(context.ceiling_nits, REFERENCE_WHITE_NITS))
    column_spans = _spans(width, 5)
    band_count = (len(_COLOUR_PATCHES) + len(column_spans) - 1) // len(column_spans)
    band_spans = _spans(height, band_count)

    bands: list[bytes] = []
    for band_index, band_height in enumerate(band_spans):
        pieces: list[bytes] = []
        for column_index, span in enumerate(column_spans):
            index = band_index * len(column_spans) + column_index
            if index < len(_COLOUR_PATCHES):
                red, green, blue = _COLOUR_PATCHES[index][1]
                pieces.append(_colour_pixel(red * level, green * level, blue * level) * span)
            else:
                pieces.append(_pixel(0.0) * span)
        bands.append(b"".join(pieces) * band_height)
    return b"".join(bands)


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        key="gamma-match",
        title="Gamma match",
        purpose="Reads the transfer function the display is actually producing.",
        criterion="The patch labelled TARGET is the one that vanishes into the lines.",
        instructions=(
            "View from a distance until the fine checkerboard blends into grey. "
            "In PROFILE adjustment, the fixed TARGET must match that grey. "
            "Left lowers Gamma and brightens TARGET; Right raises Gamma and darkens it. "
            "Tab selects Gamma, Midtone Brightness or Contrast. Wait for Live profile applied.\n\n"
            "G switches to REFERENCE match: its separate readout moves only the pattern "
            "to measure gamma, like a browser test. G again shows reference rows, then "
            "returns to PROFILE adjustment. If a fixed target cannot match, use the "
            "reference views or skip; do not mistake a pattern readout for a profile correction."
        ),
        render=_render_gamma_match,
        markers=_gamma_markers,
    ),
    Pattern(
        key="black-level",
        title="Black level",
        purpose="Measures the darkest luminance this display can still show.",
        criterion="The lowest level at which the shape remains just visible.",
        instructions=(
            "Best performed in a dark room, after a minute of visual adjustment.\n\n"
            "1. Hold Left until the circle disappears completely.\n"
            "2. Tap Right until it becomes just visible again.\n"
            "3. Press Enter."
        ),
        render=_render_black_level,
        markers=_probe_markers,
        level_driven=True,
    ),
    Pattern(
        key="peak-white",
        title="Peak white",
        purpose="Measures peak luminance rather than trusting the panel's declared figure.",
        criterion="The highest level at which the circle still separates from its surround.",
        instructions=(
            "The circle is slightly brighter than the square around it.\n\n"
            "1. Hold Right until the circle merges into the square.\n"
            "2. Tap Left until the two separate again.\n"
            "3. Press Enter.\n\n"
            "Allow a few seconds between steps near the top of the range. Emissive panels "
            "take time to settle, and a reading taken before they do will be wrong."
        ),
        render=_render_peak_clip,
        markers=_probe_markers,
        level_driven=True,
    ),
    Pattern(
        key="full-frame-white",
        title="Full-frame white",
        purpose="Calibrates the full-screen white clipping threshold.",
        criterion="The highest level at which the circle still separates from its surround.",
        instructions=(
            "The full screen is lit. Allow a few seconds to settle near the threshold.\n\n"
            "1. Raise the level until the circle disappears.\n"
            "2. Fine-adjust to the clipping threshold.\n"
            "3. Press Enter to record, or N to skip in the guided run.\n\n"
            "This is a visual tone-mapping threshold, not an instrument measurement of sustained panel luminance."
        ),
        render=_render_peak_clip,
        markers=_probe_markers,
        level_driven=True,
        window_fraction=1.0,
    ),
    Pattern(
        key="tone-tracking",
        title="Tone tracking",
        purpose="Identifies which tone control is misadjusted, at normal viewing distance.",
        criterion="The bar only just visible, at this level and at every other.",
        instructions=(
            "A faint bar crosses the middle of the patch. Levels are shown one at a time, "
            "because a bright area anywhere on screen prevents a dark one from being "
            "judged.\n\n"
            "1. Assess the bar here: too faint to find, about right, or obvious.\n"
            "2. Up and Down move between levels. Allow a few seconds at each.\n"
            "3. Cover all seven, then compare the result against the table below.\n"
            "4. Tab selects a control, Left and Right adjust it. Repeat the sweep.\n\n"
            "too faint at the DARK levels only    -> raise Midtone Brightness\n"
            "too faint at the BRIGHT levels only  -> lower Midtone Brightness\n"
            "too faint at BOTH ends               -> lower Contrast\n"
            "too obvious at BOTH ends             -> raise Contrast\n"
            "wrong by the same amount EVERYWHERE  -> adjust Gamma\n\n"
            "Only the overall pattern is meaningful. No single level indicates anything on "
            "its own. Adjust one control at a time."
        ),
        render=_render_tone_tracking,
        markers=_tone_tracking_markers,
        levels=tone_tracking_levels,
    ),
    Pattern(
        key="grey-staircase",
        title="Grey staircase",
        purpose="Shows crush, clipping and banding across the whole range at once.",
        criterion="Every step distinguishable from both of its neighbours.",
        instructions=(
            "Steps merging at the dark end indicate crush. Steps merging at the bright end "
            "indicate clipping.\n\n"
            "This is a verification pattern rather than an adjustment. Correct the cause "
            "using Black level and Peak white, then return here to confirm."
        ),
        render=_render_grey_staircase,
        markers=_staircase_markers,
    ),
    Pattern(
        key="near-black",
        title="Shadow ladder",
        purpose="Shows how far into the shadows detail survives, in absolute luminance.",
        criterion="Every labelled patch distinguishable from the one beside it.",
        instructions=(
            "Labels are nits. The darkest patch still separable from the background marks "
            "where shadow detail ends; everything below it is crushed.\n\n"
            "Also shows the cost of the SDR-in-HDR correction, which takes 0.5 nits down "
            "to roughly 0.1."
        ),
        render=_render_near_black,
        markers=_near_black_markers,
    ),
    Pattern(
        key="neutral-ramp",
        title="Neutral ramp",
        purpose="Exposes tint that drifts across the luminance range.",
        criterion="A sweep with no colour visible at any point.",
        instructions=(
            "Look for colour appearing at one end of the sweep and not the other.\n\n"
            "A uniform cast cannot be judged reliably without a reference. A drifting one "
            "can, and it is what the per-channel Fine Balance trims correct."
        ),
        render=_render_neutral_ramp,
    ),
    Pattern(
        key="solid-patch",
        title="Solid patch",
        purpose="A single flat level for meter measurement.",
        criterion="Not judged by eye. This is the patch a meter reads.",
        instructions=(
            "1. Set the level.\n"
            "2. Place the meter against the centre of the patch.\n"
            "3. Allow the reading to settle before recording it.\n\n"
            "Emissive panels drift for several seconds after a bright patch appears. A "
            "reading taken before the panel settles will be wrong."
        ),
        render=_render_solid_patch,
        markers=_probe_markers,
        level_driven=True,
    ),
    Pattern(
        key="colour-patches",
        title="Colour patches",
        purpose="Hue and saturation check, including memory colours.",
        criterion="Skin, sky and foliage all appearing unremarkable.",
        instructions=(
            "Memory colours are the hardest for the eye to accept as wrong, which is what "
            "makes them useful here.\n\n"
            "Check this pattern after greyscale is correct, never before: white balance "
            "moves every one of them."
        ),
        render=_render_colour_patches,
    ),
)


# Minimum, central peak and full-screen clipping calibration, as in Windows HDR
# Calibration. These visual thresholds are calibration metadata, not photometry.
MEASUREMENT_SEQUENCE: tuple[str, ...] = ("black-level", "peak-white", "full-frame-white")

# Three luminance calibration steps followed by this app's gamma comparison.
GUIDED_SEQUENCE: tuple[str, ...] = MEASUREMENT_SEQUENCE + ("gamma-match",)


def pattern_by_key(key: str) -> Pattern | None:
    for pattern in PATTERNS:
        if pattern.key == key:
            return pattern
    return None


def render(pattern: Pattern, width: int, height: int, context: PatternContext) -> bytes:
    """Render a pattern, guaranteeing the frame is exactly the size the surface wants."""
    width, height = max(1, int(width)), max(1, int(height))
    frame = pattern.render(width, height, context)
    expected = width * height * 8
    if len(frame) != expected:
        raise ValueError(
            f"{pattern.key} produced {len(frame)} bytes, expected {expected} "
            f"for {width}x{height}"
        )
    return frame


# ---------------------------------------------------------------------------------
# Composition: a black field, a centred window, and controls pushed to one edge.

# A tenth of the screen area is the long-standing convention for display patches. It is
# small enough that an emissive panel's brightness limiter barely engages and large enough
# to overfill a meter's aperture at a normal working distance.
WINDOW_AREA_FRACTION = 0.10

# The overlay is held far below diffuse white. It shares the screen with the patch, so its
# light counts towards what the panel is being asked to produce, and a bright strip of text
# beside a near-black patch would both engage the limiter and destroy dark adaptation.
OVERLAY_NITS = 12.0

# Markers sit on the patch itself, so they are dimmer again than the edge strip. A bright
# label beside a near-black shape raises local adaptation and moves the very threshold the
# pattern exists to find.
MARKER_NITS = 4.0


def window_size(width: int, height: int, fraction: float = WINDOW_AREA_FRACTION) -> tuple[int, int]:
    """Dimensions of a centred window covering ``fraction`` of the screen *area*."""
    side = max(0.0, min(1.0, float(fraction))) ** 0.5
    return (
        max(1, min(int(width), round(int(width) * side))),
        max(1, min(int(height), round(int(height) * side))),
    )


def _srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


# One entry per possible byte, so the sRGB decode is a lookup rather than a pow() per
# channel per pixel. Text overlays are the only thing that needs it and they are redrawn on
# every keystroke, so this is the difference between a responsive view and a sluggish one.
_SRGB_LINEAR_LUT: tuple[float, ...] = tuple(_srgb_to_linear(index / 255.0) for index in range(256))


def _blend_over(
    frame: bytearray,
    width: int,
    height: int,
    overlay: tuple[bytes, int, int],
    context: PatternContext,
    nits: float,
    *,
    left: int = 0,
    top: int = 0,
) -> None:
    """Alpha-blend RGBA8 text into a frame in place.

    Text is overwhelmingly transparent, so rows with no coverage are skipped wholesale and
    each remaining row is walked only between its first and last opaque pixel. Converting
    the empty space as well was costing 7 million pointless conversions per frame, which is
    half a second of lag on every keypress at 4K.
    """
    pixels, source_width, source_height = overlay
    source_width, source_height = int(source_width), int(source_height)
    if source_width <= 0 or source_height <= 0:
        return

    alpha_plane = pixels[3::4]
    encode = context.encode
    for row in range(source_height):
        y = top + row
        if not 0 <= y < height:
            continue
        row_start = row * source_width
        row_alpha = alpha_plane[row_start:row_start + source_width]
        if row_alpha.count(0) == len(row_alpha):
            continue
        first = len(row_alpha) - len(row_alpha.lstrip(b"\x00"))
        last = len(row_alpha.rstrip(b"\x00"))
        for column in range(first, last):
            alpha = row_alpha[column]
            if not alpha:
                continue
            x = left + column
            if not 0 <= x < width:
                continue
            weight = alpha / 255.0
            source = (row_start + column) * 4
            destination = (y * width + x) * 8
            existing = struct.unpack_from("<4e", frame, destination)
            struct.pack_into(
                "<4e", frame, destination,
                *(
                    existing[channel] * (1.0 - weight)
                    + encode(_SRGB_LINEAR_LUT[pixels[source + channel]] * nits) * weight
                    for channel in range(3)
                ),
                1.0,
            )


def compose(
    width: int,
    height: int,
    pattern: Pattern,
    context: PatternContext,
    *,
    fraction: float = WINDOW_AREA_FRACTION,
    overlay: tuple[bytes, int, int] | None = None,
    overlay_side: str = "right",
    overlay_nits: float = OVERLAY_NITS,
    window_overlay: tuple[bytes, int, int] | None = None,
    marker_nits: float = MARKER_NITS,
) -> bytes:
    """Build a full frame: black everywhere, the pattern in a centred window.

    This is how display patches have always been presented, and on an emissive panel it
    is the only way readings stay comparable. A pattern that fills the screen makes the
    brightness limiter engage differently for a dark pattern than a bright one, so two
    measurements taken minutes apart are not measuring the same thing. Confining every
    pattern to the same window area holds that variable still.

    ``overlay`` is straight RGBA8, as produced by a Qt paint into a QImage, and is pinned
    to the far left or far right rather than floated over the middle: anything near the
    patch contaminates both the reading and the viewer's adaptation.
    """
    width, height = max(1, int(width)), max(1, int(height))
    block_width, block_height = window_size(
        width, height, pattern.window_fraction if pattern.window_fraction is not None else fraction
    )
    block = render(pattern, block_width, block_height, context)

    left = (width - block_width) // 2
    top = (height - block_height) // 2
    black_row = _row(width, 0.0)

    rows: list[bytes] = []
    for y in range(height):
        if top <= y < top + block_height:
            offset = (y - top) * block_width * 8
            rows.append(
                black_row[: left * 8]
                + block[offset:offset + block_width * 8]
                + black_row[(left + block_width) * 8:]
            )
        else:
            rows.append(black_row)
    frame = bytearray(b"".join(rows))

    # Both overlays go through one routine, blended into the assembled frame. Markers land
    # on the window, guidance against an edge, and each is painted over whatever is beneath
    # so nothing is silently dropped where they meet.
    if window_overlay is not None:
        _blend_over(frame, width, height, window_overlay, context, marker_nits,
                    left=left, top=top)
    if overlay is not None:
        overlay_width = min(int(overlay[1]), width)
        overlay_height = min(int(overlay[2]), height)
        _blend_over(
            frame, width, height, overlay, context, overlay_nits,
            # "centre" is for a screen with no patch on it: nothing to keep stray light
            # away from, so nothing to gain by hiding the text against an edge.
            left=((width - overlay_width) // 2 if overlay_side == "centre"
                  else (width - overlay_width) if overlay_side == "right" else 0),
            top=max(0, (height - overlay_height) // 2),
        )
    return bytes(frame)


#: How bright the placement target is drawn, in nits. Bright enough for the instrument
#: to be sure of it over any ambient light leaking under its rim, dim enough that the
#: panel is not left adapting to it -- an emissive display's brightness limiter responds
#: to what was on screen a moment ago, so a target at peak white would still be
#: affecting the first reading of the run.
PLACEMENT_NITS = 100.0

#: Drawn in green rather than white, because green is what makes the target
#: self-verifying. The instrument is looking for a specific chromaticity, and green is
#: the primary furthest from both a dark screen and from the warm cast of typical room
#: lighting -- so "the meter can see the target" and "the meter is picking up the room"
#: cannot be confused. White would sit close enough to ambient to be ambiguous.
PLACEMENT_RGB = (0.0, 1.0, 0.0)


def placement_frame(
    width: int,
    height: int,
    context: PatternContext,
    *,
    fraction: float = WINDOW_AREA_FRACTION,
) -> bytes:
    """The target the meter is placed on, at the exact geometry the patches will use.

    Filled rather than outlined, and green rather than white, because the instrument
    reads it: once the meter covers this square it sees green and the run can start by
    itself. That is the whole point -- the alternative is asking someone to find Enter
    on a keyboard they cannot look at while holding an instrument flat against a screen.

    Shown only before the run, which is the one moment anything may be lit here. Once
    measuring starts the screen carries a single patch on black, because every other lit
    pixel is light the instrument would integrate along with the patch.

    ``fraction`` must be the *smallest* window any patch in the run will use, not the
    one most of them use. Covering this target has to guarantee covering every patch,
    and the peak patch is deliberately smaller than the rest -- a meter centred well
    enough for a tenth of the screen can still overhang a patch a thirtieth of it, and
    a patch edge under the instrument's aperture reads part black and returns a
    luminance that is wrong in a way nothing downstream can detect. Drawing the target
    at the size most patches use would put that failure exactly on the one reading the
    small window exists to get right.
    """
    width, height = max(1, int(width)), max(1, int(height))
    level = context.encode(PLACEMENT_NITS)
    red, green, blue = (max(0.0, channel) * level for channel in PLACEMENT_RGB)

    patch_width, patch_height = window_size(width, height, fraction)
    left = (width - patch_width) // 2
    top = (height - patch_height) // 2

    black_row = _pixel(0.0) * width
    patch_row = (
        _pixel(0.0) * left
        + _colour_pixel(red, green, blue) * patch_width
        + _pixel(0.0) * (width - left - patch_width)
    )
    return b"".join(
        patch_row if top <= y < top + patch_height else black_row for y in range(height)
    )


def measurement_frame(
    width: int,
    height: int,
    rgb: tuple[float, float, float],
    nits: float,
    context: PatternContext,
    *,
    fraction: float = WINDOW_AREA_FRACTION,
    colour_space: str = "BT.709",
) -> bytes:
    """A solid patch of ``rgb`` at ``nits``, centred on black, for a meter to read.

    Deliberately plainer than the by-eye patterns: no shape, no markers, no guidance
    text. Anything else on screen adds light the instrument would integrate, and a
    patch that has to be judged by eye needs contrast a patch being measured does not.

    ``nits`` is the level white would be shown at, and ``rgb`` scales each channel from
    there, so a primary is driven exactly as hard as the white it is compared against.
    Reading primaries at some other drive would measure a different point on the
    panel's response and make the chromaticities incomparable.

    ``colour_space`` labels the linear input drive. BT.709 preserves the balance
    path's non-negative channels; BT.2020 converts to signed scRGB without clipping.
    The latter is HDR-only and describes the active output path, not raw panel gamut.

    The window covers a fixed fraction of screen *area* for the same reason it does
    everywhere else here: on an emissive panel the brightness limiter responds to total
    output, so a patch of changing size is not measuring one thing.
    """
    width, height = max(1, int(width)), max(1, int(height))
    level = context.encode(nits)
    if colour_space == "BT.2020":
        from .gamut import to_scrgb
        if not context.is_hdr:
            raise ValueError("BT.2020 gamut measurement requires HDR output")
        red, green, blue = (channel * level for channel in to_scrgb(rgb))
    elif colour_space == "BT.709":
        red, green, blue = (max(0.0, float(channel)) * level for channel in rgb)
    else:
        raise ValueError(f"Unsupported measurement colour space: {colour_space}")

    patch_width, patch_height = window_size(width, height, fraction)
    left = (width - patch_width) // 2
    top = (height - patch_height) // 2

    black_row = _pixel(0.0) * width
    patch_row = (
        _pixel(0.0) * left
        + _colour_pixel(red, green, blue) * patch_width
        + _pixel(0.0) * (width - left - patch_width)
    )
    rows = [patch_row if top <= y < top + patch_height else black_row for y in range(height)]
    return b"".join(rows)
