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

scRGB carries linear light. The gamma pattern decodes SDR sRGB signals into it once.
Its reference averages black/white output optically after the active display transform;
matching the raw scRGB arithmetic mean would bypass the response being evaluated.

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
    gamma_target: float = 2.2  # Pattern goal only; never a profile control.

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

# Optical reference endpoints in desktop sRGB signal space.
GAMMA_REFERENCE_SIGNALS = (0.0, 1.0)


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
    """Use desktop white regardless of the correction option or profile sliders."""
    return context.sdr_white_nits


def gamma_target_nits(context: PatternContext, gamma: float | None = None) -> float:
    """Encode the selected SDR power-law target through piecewise sRGB exactly once.

    A 50% black/white reference requires signal 0.5**(1/gamma), not
    linear scRGB 0.5. The active display profile is applied later by Windows.
    """
    gamma = gamma_goal(context) if gamma is None else gamma
    low, high = GAMMA_REFERENCE_SIGNALS
    signal = ((low ** gamma + high ** gamma) / 2.0) ** (1.0 / gamma)
    return gamma_reference_nits(context) * _srgb_to_linear(signal)


def gamma_goal(context: PatternContext) -> float:
    value = float(context.gamma_target)
    return max(1.6, min(3.0, value)) if math.isfinite(value) else 2.2


def _render_gamma_match(width: int, height: int, context: PatternContext) -> bytes:
    """Nine feathered dots on horizontal native-pixel stripes, like classic DCCW.

    Feathering replaces complete black/white row pairs with solid target pixels.
    This spatial mixture preserves optical matching through a nonlinear display
    transform; interpolating source grey values would introduce a false halo.
    """
    white = _pixel(context.encode(gamma_reference_nits(context)))
    black = _pixel(0.0)
    solid = _pixel(context.encode(gamma_target_nits(context)))
    comparison_width = max(1, round(width * 0.72))
    side = min(comparison_width, height)
    cell = side / 3
    left, top = (comparison_width-side)/2, (height-side)/2
    centres = [(left+cell*(column+0.5), top+cell*(row+0.5))
               for row in range(3) for column in range(3)]
    core, outer = max(1.0,cell*0.08), max(1.5,cell*0.36)
    bayer = ((0,8,2,10),(12,4,14,6),(3,11,1,9),(15,7,13,5))
    rows=[]
    for y in range(height):
        row = bytearray((white if y%2==0 else black)*comparison_width + black*(width-comparison_width))
        pair_y = (y//2)*2+1
        for cx,cy in centres:
            dy = pair_y-cy
            if abs(dy)>=outer: continue
            half=math.sqrt(outer*outer-dy*dy)
            for x in range(max(0,math.ceil(cx-half)),min(comparison_width,math.floor(cx+half)+1)):
                radius=math.hypot(x+0.5-cx,dy)
                t=max(0.0,min(1.0,(outer-radius)/(outer-core)))
                coverage=t*t*(3-2*t)
                if coverage>(bayer[(y//2)%4][x%4]+0.5)/16:
                    row[x*8:(x+1)*8]=solid
        rows.append(bytes(row))
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


def _render_solid_patch(width: int, height: int, context: PatternContext) -> bytes:
    """A single flat level filling the window: the patch a meter reads."""
    return _row(width, context.encode(context.probe_nits)) * height


# ---------------------------------------------------------------------------------
# Markers. Positions are fractions of the window, so they follow it at any size.


def _gamma_markers(context: PatternContext) -> tuple[Marker, ...]:
    goal=gamma_goal(context)
    role="SDR desktop" if abs(goal-2.2)<0.0005 else ("SDR video" if abs(goal-2.4)<0.0005 else ("DCI cinema" if abs(goal-2.6)<0.0005 else "Custom SDR goal"))
    return (Marker(f"{goal:.3f} SDR TARGET",0.85,0.45,target=True),
            Marker(role,0.85,0.55))


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


def _render_highlight_steps(width: int,height: int,context: PatternContext) -> bytes:
    spans=_spans(width,12)
    row=b"".join(_pixel(context.encode(context.ceiling_nits*(.75+.25*i/max(1,len(spans)-1))))*span
                 for i,span in enumerate(spans))
    return row*height


def _render_uniformity(width: int,height: int,context: PatternContext) -> bytes:
    return _row(width,context.encode(context.sdr_white_nits*_srgb_to_linear(.5)))*height


def _render_sharpness(width: int,height: int,context: PatternContext) -> bytes:
    white=_pixel(context.encode(context.sdr_white_nits));black=_pixel(0)
    rows=[]
    for y in range(height):
        row=[]
        for x in range(width):
            lit=(x%2==0) if x<width//3 else ((y%2==0) if x<width*2//3 else ((x+y)%2==0))
            row.append(white if lit else black)
        rows.append(b"".join(row))
    return b"".join(rows)


def _render_colour_gradients(width: int,height: int,context: PatternContext) -> bytes:
    rows=[]
    for channel,span in enumerate(_spans(height,3)):
        row=[]
        for x in range(width):
            value=context.encode(context.sdr_white_nits*_srgb_to_linear(x/max(1,width-1)))
            rgb=[0.0,0.0,0.0];rgb[channel]=value
            row.append(_colour_pixel(*rgb))
        rows.append(b"".join(row)*span)
    return b"".join(rows)


def _render_pixel_fields(width: int,height: int,context: PatternContext) -> bytes:
    white=context.encode(context.sdr_white_nits)
    colours=((0,0,0),(white,0,0),(0,white,0),(0,0,white),(white,white,white))
    row=b"".join(_colour_pixel(*colour)*span for colour,span in zip(colours,_spans(width,5)))
    return row*height


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        key="gamma-match",
        title="Gamma match",
        purpose="Checks effective SDR desktop gamma against a selected goal.",
        criterion="Minimize the nine centre dots against the blended horizontal stripes.",
        instructions=(
            "Use native resolution. Move back or slightly defocus until the horizontal stripes blend. "
            "Adjust the profile Gamma to minimize the dots, as in classic Windows calibration.\n\n"
            "Up/Down changes the pattern target (1.600-3.000, step 0.1); Home restores 2.200. "
            "This does not change the profile. Left/Right adjusts the selected profile control; "
            "Tab selects Gamma, Midtone Brightness or Contrast. Wait for Live profile applied.\n\n"
            "Choose correction mode first. Start with neutral brightness/contrast; recheck after edits. "
            "2.2: SDR desktop. 2.4: dim-viewing SDR video. 2.6: DCI cinema. "
            "HDR uses PQ (HDR10) or HLG. The profile slider is neutral at 2.200, not measured gamma."
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


# Separate catalog: only fixed diagnostic frames, never guided calibration targets.
DIAGNOSTIC_PATTERNS: tuple[Pattern,...] = tuple(
    Pattern(p.key,p.title,p.purpose,p.criterion,
            "Inspect the displayed result. This view changes no settings. " + (
                "Merged steps indicate crushing or clipping." if p.key=="grey-staircase" else
                "Look for lost shadow detail." if p.key=="near-black" else
                "Look for tint shifts and banding." if p.key=="neutral-ramp" else
                "Inspect hue and saturation; there is no visual absolute colour reference."),
            p.render,p.markers,records=False)
    for p in PATTERNS if p.key in ("grey-staircase","near-black","neutral-ramp","colour-patches")
) + (
    Pattern("highlight-steps","Highlight separation","Reviews detail near the reported peak.",
            "Adjacent bright steps remain distinguishable.","Fixed nominal HDR levels; merged steps can indicate tone mapping or clipping.",_render_highlight_steps,records=False),
    Pattern("uniformity","Grey uniformity","Reviews full-screen luminance and tint uniformity.",
            "The field has no obvious bright patches or colour shifts.","Inspect from a normal viewing distance. The field stays at a fixed mid-grey level.",_render_uniformity,records=False,window_fraction=1.0),
    Pattern("sharpness","Pixel sharpness","Checks native-pixel detail without scaling.",
            "Fine lines and the checkerboard remain regular.","Use native display resolution. Moire, blur or halos can indicate scaling or sharpening.",_render_sharpness,records=False),
    Pattern("colour-gradients","RGB gradients","Reviews channel banding and colour transitions.",
            "Each gradient remains smooth.","Inspect the fixed red, green and blue ramps for banding or abrupt transitions.",_render_colour_gradients,records=False),
    Pattern("pixel-fields","RGB pixel fields","Inspects black, primary-colour and white regions.",
            "No isolated unexpected pixels are visible.","These fixed regions can reveal pixel or channel defects; they are not a complete full-screen dead-pixel test.",_render_pixel_fields,records=False),
)


def pattern_by_key(key: str) -> Pattern | None:
    for pattern in PATTERNS + DIAGNOSTIC_PATTERNS:
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

# Readable UI luminance, separate from the calibration patch's own levels.
OVERLAY_NITS = 80.0
# In-pattern labels remain a little dimmer than the edge controls.
MARKER_NITS = 48.0


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
