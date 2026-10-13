"""Read a display's HDR capability from its own EDID.

Windows' ``DXGI_OUTPUT_DESC1`` is the obvious source and is wrong about two of the three
figures that matter. Measured against one panel whose EDID declares 1015.24 nits peak,
265.05 nits maximum frame-average and 0.0002 nits minimum, DXGI reported 1010.40, 1010.40
and 0.1956: peak roughly right, frame-average replaced by peak, and a minimum four hundred
times too high.

Maximum frame-average is the one that matters most and is the one DXGI discards. On an
emissive panel it is a small fraction of peak -- 265 against 1015 here -- because the
brightness limiter cannot sustain a full screen at peak. A tool that treats peak as the
full-screen figure asks the display for four times the light it can hold, and a profile
built on that describes a display nobody owns.

The EDID is stored by the Plug and Play enumerator, so this reads the panel's own
declaration rather than anything Windows derived from it. Encoding is CTA-861-G section
7.5.13: luminance codes are logarithmic, and minimum is a fraction of maximum.

The same applies to the display's primaries, for a worse reason. ``DXGI_OUTPUT_DESC1``
reports whatever ICC profile is currently associated, not the panel. On the display this
was developed against, DXGI reported (0.6746, 0.3144) for red while one profile was
applied and (0.6486, 0.3312) after another was, each matching its profile's colorant
tags to four decimal places, while the panel's own EDID says (0.6836, 0.3047) throughout.
That feedback loop is self-sustaining: a profile written from DXGI's answer becomes
DXGI's next answer. The EDID cannot be contaminated that way, so it is the source here.
Base-block chromaticity is 10-bit, about 0.001 in xy -- coarser than DXGI's float and
correct, which matters more.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None  # type: ignore[assignment]

_ENUM_ROOT = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"

# CTA-861 extension block, and the extended tag that carries HDR static metadata.
_CTA_EXTENSION_TAG = 0x02
_EXTENDED_TAG_BLOCK = 7
_HDR_STATIC_METADATA = 0x06

# EOTF support bits in the first payload byte.
_EOTF_PQ_BIT = 0x04


@dataclass(frozen=True)
class PanelMetadata:
    """What the display declares about itself, in nits."""

    peak_nits: float
    max_frame_average_nits: float
    min_nits: float
    supports_pq: bool
    # rx, ry, gx, gy, bx, by, wx, wy from the base block, or empty when the EDID
    # is malformed. Ordered to match ModeState.panel_primaries.
    primaries: tuple[float, ...] = ()

    @property
    def credible(self) -> bool:
        """Whether these figures are worth preferring over anything else.

        A panel that declares no PQ support, or a peak outside anything a display could
        produce, has not really answered the question.
        """
        return self.supports_pq and 40.0 <= self.peak_nits <= 10000.0


def parse_chromaticity(edid: bytes) -> tuple[float, ...]:
    """The display's primaries and white from EDID base-block bytes 0x19-0x22.

    Each coordinate is 10 bits: eight in its own byte, with the low two packed
    into one of two shared bytes. Returns rx, ry, gx, gy, bx, by, wx, wy, or an
    empty tuple when the block cannot be read.

    Resolution is 1/1024, about 0.001 in xy. That is coarser than the float DXGI
    reports and unlike it describes the panel rather than the profile in force.
    """
    if len(edid) < 0x23:
        return ()
    low_rg, low_bw = edid[0x19], edid[0x1A]

    def coordinate(high: int, low: int, shift: int) -> float:
        return ((edid[high] << 2) | ((low >> shift) & 0x03)) / 1024.0

    values = (
        coordinate(0x1B, low_rg, 6), coordinate(0x1C, low_rg, 4),
        coordinate(0x1D, low_rg, 2), coordinate(0x1E, low_rg, 0),
        coordinate(0x1F, low_bw, 6), coordinate(0x20, low_bw, 4),
        coordinate(0x21, low_bw, 2), coordinate(0x22, low_bw, 0),
    )
    # A panel that reports zeros, or coordinates outside the chromaticity
    # diagram, has not answered. Each y also divides when converting to XYZ.
    if any(not 0.0 < value < 1.0 for value in values):
        return ()
    if any(values[index] <= 0.0 for index in (1, 3, 5, 7)):
        return ()
    return values


def _luminance_from_code(code: int) -> float:
    """CTA-861 desired-luminance code to nits: 50 * 2^(code/32)."""
    return 50.0 * (2.0 ** (code / 32.0))


def _declared_luminance(code: int) -> float:
    """The same, except that a zero byte in a full-length block means nothing.

    CTA-861 lets a panel decline to state these by making the block shorter,
    which the caller already handles by length. A panel that sends the full
    block and leaves the bytes at 0x00 has filled in nothing, and reading that
    literally gives 50 nits -- which no HDR display is, and which is worse than
    useless downstream: it clears the 40-nit credibility floor so the figure is
    believed, it is truthy so every ``frame_average or peak`` fallback is
    skipped, and the 80-nit clamps then turn peak and sustained alike into
    exactly 80. A 1000-nit panel came out of Calibrate Display declaring 80.

    The arithmetic above stays faithful to the specification; this is a
    judgement about an unfilled field, which is a different thing.
    """
    return 0.0 if code == 0 else _luminance_from_code(code)


def parse_hdr_static_metadata(edid: bytes) -> PanelMetadata | None:
    """Pull the HDR static metadata data block out of a raw EDID, if it carries one."""
    if len(edid) < 256:
        return None
    for extension in range(1, len(edid) // 128):
        base = extension * 128
        if edid[base] != _CTA_EXTENSION_TAG:
            continue
        # Byte 2 is the offset of the first detailed timing descriptor, so the data block
        # collection runs from byte 4 up to it.
        end = base + edid[base + 2]
        cursor = base + 4
        while cursor < end and cursor < len(edid):
            tag = edid[cursor] >> 5
            length = edid[cursor] & 0x1F
            payload = cursor + 1
            if (
                tag == _EXTENDED_TAG_BLOCK
                and length >= 3
                and payload < len(edid)
                and edid[payload] == _HDR_STATIC_METADATA
                and payload + length <= len(edid)
            ):
                eotf = edid[payload + 1]
                # Only the maximum is mandatory once the block is present; a panel may
                # stop after it, so each later field is read only if it is really there.
                peak = _declared_luminance(edid[payload + 3]) if length >= 4 else 0.0
                frame_average = (
                    _declared_luminance(edid[payload + 4]) if length >= 5 else 0.0
                )
                minimum = (
                    peak * ((edid[payload + 5] / 255.0) ** 2) / 100.0 if length >= 6 else 0.0
                )
                return PanelMetadata(
                    peak_nits=peak,
                    max_frame_average_nits=frame_average,
                    min_nits=minimum,
                    supports_pq=bool(eotf & _EOTF_PQ_BIT),
                    # From the base block, not this extension: the panel's own
                    # gamut, which DXGI cannot be trusted to report.
                    primaries=parse_chromaticity(edid),
                )
            cursor += length + 1
    return None


def _registry_key_for(device_path: str) -> tuple[str, str] | None:
    """Split a monitor device path into the enumerator key it was registered under.

    ``\\\\?\\DISPLAY#AUS32F2#5&25649870&7&UID4357#{guid}`` is stored under
    ``DISPLAY\\AUS32F2\\5&25649870&7&UID4357``.
    """
    parts = device_path.split("#")
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def read_panel_metadata(device_path: str) -> PanelMetadata | None:
    """The HDR figures a display declares, or None if it declares none.

    Returns None rather than raising for every failure: a missing EDID, a panel with no
    HDR block, and a registry that cannot be read are all just "no answer", and the
    caller's fallback is the same in each case.
    """
    if winreg is None or not device_path:
        return None
    parsed = _registry_key_for(device_path)
    if parsed is None:
        return None
    hardware_id, instance = parsed
    path = f"{_ENUM_ROOT}\\{hardware_id}\\{instance}\\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            edid, _kind = winreg.QueryValueEx(key, "EDID")
    except OSError:
        return None
    if not isinstance(edid, (bytes, bytearray)):
        return None
    return parse_hdr_static_metadata(bytes(edid))
