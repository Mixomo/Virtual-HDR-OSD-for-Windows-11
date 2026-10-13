from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .curves import D65_XYZ, CalibrationTransform, _inverse3, estimate_curve_gamma
from .curves import _matvec3 as _matrix_vector
from .model import DisplayMode, ModeState, normalize_primaries

D50_XYZ = (0.9642, 1.0, 0.8249)
# Tag groups that are only coherent when they all come from the same source.
COUPLED_TAG_GROUPS: tuple[tuple[bytes, ...], ...] = (
    (b"rXYZ", b"gXYZ", b"bXYZ"),   # colorants
    (b"rTRC", b"gTRC", b"bTRC"),   # per-channel tone curves
    (b"wtpt", b"chad"),            # media white and its adaptation to the PCS
)

PRIMARIES = {
    "SDR": (0.640, 0.330, 0.300, 0.600, 0.150, 0.060, 0.3127, 0.3290),
    "HDR": (0.708, 0.292, 0.170, 0.797, 0.131, 0.046, 0.3127, 0.3290),
}


@dataclass(slots=True)
class ImportedProfile:
    mode: DisplayMode
    state: ModeState
    exact_state: bool
    tags: tuple[str, ...]
    description: str
    warnings: tuple[str, ...]


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * ((4 - len(data) % 4) % 4)


def _s15fixed16(value: float) -> bytes:
    return struct.pack(">i", int(round(value * 65536.0)))


def _xyz_type(xyz: tuple[float, float, float]) -> bytes:
    return b"XYZ " + b"\0" * 4 + b"".join(_s15fixed16(value) for value in xyz)


def _sf32_type(values: Iterable[float]) -> bytes:
    return b"sf32" + b"\0" * 4 + b"".join(_s15fixed16(value) for value in values)


def _curve_gamma_type(gamma: float) -> bytes:
    encoded = max(1, min(65535, int(round(gamma * 256.0))))
    return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", encoded) + b"\0\0"


def _mluc_type(text: str) -> bytes:
    raw = text.encode("utf-16-be")
    record_count = 1
    record_size = 12
    string_offset = 16 + record_count * record_size
    data = bytearray(b"mluc" + b"\0" * 4)
    data += struct.pack(">II", record_count, record_size)
    data += b"enUS" + struct.pack(">II", len(raw), string_offset)
    data += raw
    return _pad4(bytes(data))


def _text_type(text: str) -> bytes:
    return _pad4(b"text" + b"\0" * 4 + text.encode("utf-8"))


def _msca_text_type() -> bytes:
    # Windows HDR Calibration profiles store this private companion tag as
    # textType containing a compact Python-style dictionary. It is metadata;
    # MHC2 is the calibration payload consumed by the Windows loader.
    return _text_type("{'D65Adapted':True}")


def _chromaticities_to_xyz(
    values: tuple[float, ...],
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    xr, yr, xg, yg, xb, yb, xw, yw = values

    def xyz(x: float, y: float) -> tuple[float, float, float]:
        return (x / y, 1.0, (1.0 - x - y) / y)

    r = xyz(xr, yr)
    g = xyz(xg, yg)
    b = xyz(xb, yb)
    w = xyz(xw, yw)
    matrix = (
        (r[0], g[0], b[0]),
        (r[1], g[1], b[1]),
        (r[2], g[2], b[2]),
    )
    determinant = (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )
    if abs(determinant) < 1e-12:
        raise ValueError("Invalid display primaries")

    inverse = (
        (
            (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1]) / determinant,
            (matrix[0][2] * matrix[2][1] - matrix[0][1] * matrix[2][2]) / determinant,
            (matrix[0][1] * matrix[1][2] - matrix[0][2] * matrix[1][1]) / determinant,
        ),
        (
            (matrix[1][2] * matrix[2][0] - matrix[1][0] * matrix[2][2]) / determinant,
            (matrix[0][0] * matrix[2][2] - matrix[0][2] * matrix[2][0]) / determinant,
            (matrix[0][2] * matrix[1][0] - matrix[0][0] * matrix[1][2]) / determinant,
        ),
        (
            (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0]) / determinant,
            (matrix[0][1] * matrix[2][0] - matrix[0][0] * matrix[2][1]) / determinant,
            (matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]) / determinant,
        ),
    )
    scales = tuple(sum(inverse[row][column] * w[column] for column in range(3)) for row in range(3))
    return (
        tuple(r[index] * scales[0] for index in range(3)),
        tuple(g[index] * scales[1] for index in range(3)),
        tuple(b[index] * scales[2] for index in range(3)),
    )


def _display_primaries_xyz(
    mode: DisplayMode, state: ModeState
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """The panel's own primaries where they are known, else the generic table.

    This matters most when there is no base profile to inherit rXYZ/gXYZ/bXYZ
    from, because the HDR entry in PRIMARIES is BT.2020 and virtually no display
    covers it. Describing a P3 panel as BT.2020 misplaces every saturated colour.
    """
    measured = normalize_primaries(state.panel_primaries)
    if measured:
        # Take only the R/G/B chromaticities and scale them to the same D65 the
        # wtpt tag declares. The panel's native white is typically a hair off
        # D65, but that is a calibration error for the MHC2 transform to remove,
        # not part of the gamut description -- and scaling to it here would
        # leave rXYZ+gXYZ+bXYZ disagreeing with the profile's own white.
        white = PRIMARIES[mode][6:]
        try:
            return _chromaticities_to_xyz(measured[:6] + white)
        except (ValueError, ZeroDivisionError):
            # Coordinates that pass range checks can still be collinear.
            pass
    return _chromaticities_to_xyz(PRIMARIES[mode])


# Linear Bradford chromatic adaptation from D65 to the ICC D50 PCS.
#
# Computed for D50_XYZ above, the PCS white ICC.1 fixes and the header declares. The
# matrix this replaced was the widely copied one built for D50 = (0.96422, 1, 0.82521),
# so it took D65 to a white 3e-4 off in Z from the one the same profile declares.
D65_TO_D50_CHAD = (
    1.0478400, 0.0228979, -0.0501808,
    0.0295537, 0.9904924, -0.0170663,
    -0.0092459, 0.0150633, 0.7518389,
)


def _mhc2_type(transform: CalibrationTransform, state: ModeState) -> bytes:
    curves = (transform.red, transform.green, transform.blue)
    lut_size = len(transform.red)
    if not all(len(curve) == lut_size for curve in curves):
        raise ValueError("MHC2 curves must have equal lengths")

    # MHC2 offsets are relative to the beginning of the tag. The fixed header
    # ends at byte 36, followed immediately by the 3x4 sf32 matrix.
    matrix_offset = 8 + 4 + 4 + 4 + 4 * 4
    header_size = matrix_offset + 12 * 4
    red_blob = _sf32_type(transform.red)
    green_blob = _sf32_type(transform.green)
    blue_blob = _sf32_type(transform.blue)
    red_offset = header_size
    green_offset = red_offset + len(red_blob)
    blue_offset = green_offset + len(green_blob)

    data = bytearray(b"MHC2" + b"\0" * 4)
    data += struct.pack(">I", lut_size)
    data += _s15fixed16(state.minimum_luminance_nits)
    data += _s15fixed16(state.peak_luminance_nits)
    data += struct.pack(">IIII", matrix_offset, red_offset, green_offset, blue_offset)
    data += b"".join(_s15fixed16(value) for value in transform.matrix)
    data += red_blob + green_blob + blue_blob
    return _pad4(bytes(data))


def _vcgt_type(transform: CalibrationTransform, entries: int = 256) -> bytes:
    sampled: list[list[int]] = []
    for curve in (transform.red, transform.green, transform.blue):
        values: list[int] = []
        for index in range(entries):
            position = index * (len(curve) - 1) / (entries - 1)
            left = int(math.floor(position))
            right = min(len(curve) - 1, left + 1)
            fraction = position - left
            value = curve[left] * (1.0 - fraction) + curve[right] * fraction
            values.append(max(0, min(65535, int(round(value * 65535.0)))))
        sampled.append(values)

    data = bytearray(b"vcgt" + b"\0" * 4)
    data += struct.pack(">IHHH", 0, 3, entries, 2)
    for channel in sampled:
        data += b"".join(struct.pack(">H", value) for value in channel)
    return _pad4(bytes(data))


def _build_header(total_size: int, _hdr: bool = False) -> bytearray:
    now = dt.datetime.now(dt.timezone.utc)
    header = bytearray(128)
    struct.pack_into(">I", header, 0, total_size)
    header[4:8] = b"\0" * 4  # no preferred CMM
    header[8:12] = b"\x04\x40\x00\x00"
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    struct.pack_into(">6H", header, 24, now.year, now.month, now.day, now.hour, now.minute, now.second)
    header[36:40] = b"acsp"
    header[40:44] = b"MSFT"
    struct.pack_into(">I", header, 64, 0)
    # ICC.1 requires the Profile Connection Space illuminant to be D50.
    header[68:80] = b"".join(_s15fixed16(value) for value in D50_XYZ)
    header[80:84] = b"SHPC"
    return header


def _apply_profile_id(profile: bytes) -> bytes:
    mutable = bytearray(profile)
    mutable[44:48] = b"\0" * 4
    mutable[64:68] = b"\0" * 4
    mutable[84:100] = b"\0" * 16
    digest = hashlib.md5(mutable).digest()
    result = bytearray(profile)
    result[84:100] = digest
    return bytes(result)


def build_profile(mode: DisplayMode, state: ModeState, transform: CalibrationTransform) -> bytes:
    hdr = mode == "HDR"
    red_xyz, green_xyz, blue_xyz = _display_primaries_xyz(mode, state)
    if hdr:
        # Match the D65-adapted convention used by Windows HDR Calibration:
        # physical D65 colorimetry plus identity chad and an MSCA marker.
        white = D65_XYZ
        # s15Fixed16Array of exactly nine values. A short array here produces a
        # malformed chromaticAdaptationTag that mscms may reject outright.
        chad = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    else:
        # Keep the standard SDR display profile colorimetry ICC-compliant by
        # adapting its native D65 measurements to the D50 PCS.
        red_xyz = _matrix_vector(D65_TO_D50_CHAD, red_xyz)
        green_xyz = _matrix_vector(D65_TO_D50_CHAD, green_xyz)
        blue_xyz = _matrix_vector(D65_TO_D50_CHAD, blue_xyz)
        white = D50_XYZ
        chad = D65_TO_D50_CHAD
    state_payload = _text_type(
        json.dumps(
            {"schema": "sdr-hdr-profile-state-v2", "mode": mode, "state": state.to_dict()},
            separators=(",", ":"),
        )
    )
    generated: dict[bytes, bytes] = {
        b"desc": _mluc_type(state.profile_name),
        b"lumi": _xyz_type((0.0, state.full_frame_luminance_nits, 0.0)),
        b"MHC2": _mhc2_type(transform, state),
        b"sdhs": state_payload,
    }
    if mode == "SDR":
        generated[b"vcgt"] = _vcgt_type(transform)

    defaults: list[tuple[bytes, bytes]] = [
        (b"cprt", _mluc_type("Generated by Virtual HDR OSD for Windows")),
        (b"desc", generated[b"desc"]),
        (b"wtpt", _xyz_type(white)),
        (b"rXYZ", _xyz_type(red_xyz)),
        (b"gXYZ", _xyz_type(green_xyz)),
        (b"bXYZ", _xyz_type(blue_xyz)),
        (b"rTRC", _curve_gamma_type(2.2 if mode == "SDR" else 1.0)),
        (b"gTRC", _curve_gamma_type(2.2 if mode == "SDR" else 1.0)),
        (b"bTRC", _curve_gamma_type(2.2 if mode == "SDR" else 1.0)),
        (b"chad", _sf32_type(chad)),
        (b"lumi", generated[b"lumi"]),
        (b"MHC2", generated[b"MHC2"]),
        (b"sdhs", generated[b"sdhs"]),
    ]
    if mode == "HDR":
        defaults.append((b"MSCA", _msca_text_type()))
    else:
        defaults.append((b"vcgt", generated[b"vcgt"]))

    template_tags: dict[bytes, bytes] = {}
    template_profile = state.base_profile or state.imported_profile
    if template_profile:
        try:
            source = Path(template_profile)
            if source.is_file():
                template_tags = _read_tags(source.read_bytes(), strict=True)
        except (OSError, ValueError, struct.error):
            # A truncated or malformed base is not usable as a template. Fall back
            # to a wholly self-consistent generated profile instead of splicing.
            template_tags = {}

    if template_tags:
        # Colorants inherited from a base that never said what white they were adapted
        # to. ICC requires a display profile's rXYZ/gXYZ/bXYZ to be D50-adapted, and a
        # v2 file that omits chad is relying on exactly that convention -- but the
        # (wtpt, chad) pair is about to be dropped by the coupled-group rule below and
        # replaced with this profile's own D65 white and an identity chad. Splicing the
        # two together describes a display that does not exist: measured against real
        # installed profiles the colorant sum missed the declared white by 0.044, and a
        # consumer honouring the identity chad reads green at 0.3212,0.5979 instead of
        # 0.3000,0.6000 -- four times the module's own PRIMARY_MISMATCH_THRESHOLD_XY.
        #
        # Adapt the colorants out of D50 and into this profile's white instead. Only
        # the colorants: those files store wtpt unadapted, so putting it through the
        # same matrix yields a white of 0.2806,0.2959, which is not a white at all.
        colorants = (b"rXYZ", b"gXYZ", b"bXYZ")
        if (
            all(signature in template_tags for signature in colorants)
            and b"chad" not in template_tags
            and mode == "HDR"
        ):
            d50_to_d65 = _inverse3(D65_TO_D50_CHAD)
            for signature in colorants:
                parsed = _parse_xyz(template_tags[signature])
                if parsed is None:
                    continue
                template_tags[signature] = _xyz_type(_matrix_vector(d50_to_d65, parsed))

        # Tags that only mean anything as a set. Taking some members from the base
        # profile and synthesising the rest produces a plausible-looking profile
        # describing a display that does not exist — a base missing gTRC and bTRC
        # yielded its real red curve beside linear green and blue, a gross colour
        # cast, with no error raised anywhere.
        for group in COUPLED_TAG_GROUPS:
            if not all(signature in template_tags for signature in group):
                for signature in group:
                    template_tags.pop(signature, None)

        # MSCA says "these colorants are D65-adapted", which is true of the generated
        # form -- D65 white beside an identity chad -- and false whenever a base brings
        # its own chad through, because those colorants are then still in the D50 PCS.
        # Claiming otherwise misdescribes the profile to anything that reads the marker.
        inherited_chad = _parse_chad(template_tags.get(b"chad", b""))
        identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        adapted_to_d65 = inherited_chad is None or all(
            abs(inherited_chad[i] - identity[i]) < 1e-6 for i in range(9)
        )

        tags: list[tuple[bytes, bytes]] = []
        seen: set[bytes] = set()
        for signature, payload in template_tags.items():
            if signature == b"vcgt" and mode != "SDR":
                continue
            replacement = generated.get(signature, payload)
            tags.append((signature, replacement))
            seen.add(signature)
        for signature, payload in defaults:
            if signature == b"MSCA" and not adapted_to_d65:
                continue
            if signature not in seen:
                tags.append((signature, payload))
                seen.add(signature)
    else:
        tags = defaults

    table_size = 4 + 12 * len(tags)
    body = bytearray()
    records: list[tuple[bytes, int, int]] = []
    for signature, payload in tags:
        absolute_offset = 128 + table_size + len(body)
        padding = (4 - absolute_offset % 4) % 4
        body += b"\0" * padding
        absolute_offset += padding
        records.append((signature, absolute_offset, len(payload)))
        body += payload

    total_size = 128 + table_size + len(body)
    header = _build_header(total_size, hdr)
    table = bytearray(struct.pack(">I", len(records)))
    for signature, offset, size in records:
        table += signature + struct.pack(">II", offset, size)
    return _apply_profile_id(bytes(header + table + body))


def content_digest(profile: bytes) -> str:
    """Fingerprint a profile by its calibration content, ignoring when it was made.

    Every generated profile embeds the current time in its ICC header, and the
    profile ID is an MD5 over that header. Two profiles built from identical
    settings a second apart are therefore not byte-identical even though they
    describe exactly the same calibration. Callers deciding whether a profile
    needs reinstalling must compare this, not the raw bytes.
    """
    mutable = bytearray(profile)
    mutable[24:36] = b"\0" * 12  # header dateTime
    mutable[84:100] = b"\0" * 16  # profile id, derived from the header
    return hashlib.sha256(bytes(mutable)).hexdigest()


# A tag table is at most 256 entries of 12 bytes after the 132 byte header.
_TAG_TABLE_LIMIT = 132 + 256 * 12


def is_app_generated(path: Path) -> bool:
    """True when this app produced the profile, including under older names.

    The test is the private ``sdhs`` tag, not the filename, because a generated
    profile can be renamed and because releases before the stable working-profile
    names installed theirs as ``<base>_HDR.icm``, which no prefix rule catches.

    Note that this does not make a profile unusable as a base: ``import_profile``
    reads the embedded state back exactly, including the base it was itself built
    from, so loading one restores settings rather than stacking a second
    correction. It marks a profile as *ours*, which is what callers listing
    calibration sources need to know.

    ``MHC2`` cannot serve as the test. Windows HDR Calibration writes that tag
    too, and its profiles are exactly the ones most users start from.

    Only the header and tag table are read, so this stays cheap to call for
    every profile in the colour directory.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(_TAG_TABLE_LIMIT)
    except OSError:
        return False
    if len(head) < 132 or head[36:40] != b"acsp":
        return False
    count = struct.unpack_from(">I", head, 128)[0]
    if count > 256 or 132 + count * 12 > len(head):
        return False
    return any(head[132 + index * 12 : 136 + index * 12] == b"sdhs" for index in range(count))


def _read_tags(data: bytes, *, strict: bool = False) -> dict[bytes, bytes]:
    """Parse the tag table.

    Entries pointing past the end of the file are dropped, which keeps importing a
    slightly odd profile working. ``strict`` refuses such a profile instead: a
    caller using it as a *template* must not silently inherit half of it.
    """
    if len(data) < 132 or data[36:40] != b"acsp":
        raise ValueError("Not a valid ICC profile")
    declared_size = struct.unpack_from(">I", data, 0)[0]
    if strict and (declared_size < 132 or declared_size > len(data)):
        raise ValueError("Invalid or truncated ICC profile size")
    if 132 <= declared_size <= len(data):
        data = data[:declared_size]
    count = struct.unpack_from(">I", data, 128)[0]
    if count > 256 or 132 + count * 12 > len(data):
        raise ValueError("Invalid ICC tag table")
    result: dict[bytes, bytes] = {}
    dropped = 0
    for index in range(count):
        offset = 132 + index * 12
        signature = data[offset : offset + 4]
        payload_offset, payload_size = struct.unpack_from(">II", data, offset + 4)
        if payload_offset >= 132 + count * 12 and payload_offset + payload_size <= len(data):
            result[signature] = data[payload_offset : payload_offset + payload_size]
        else:
            dropped += 1
    if strict and dropped:
        raise ValueError(f"ICC profile is truncated: {dropped} of {count} tags are unreadable")
    return result


def _parse_text(payload: bytes) -> str:
    if payload[:4] == b"text":
        return payload[8:].rstrip(b"\0").decode("utf-8", "replace")
    if payload[:4] == b"mluc" and len(payload) >= 28:
        count, record_size = struct.unpack_from(">II", payload, 8)
        if count and record_size >= 12:
            length, offset = struct.unpack_from(">II", payload, 20)
            if offset + length <= len(payload):
                return payload[offset : offset + length].decode("utf-16-be", "replace")
    if payload[:4] == b"desc" and len(payload) >= 12:
        length = struct.unpack_from(">I", payload, 8)[0]
        return payload[12 : 12 + max(0, length - 1)].decode("ascii", "replace")
    return ""


def _parse_sf32_curve(payload: bytes, offset: int, entries: int) -> list[float]:
    if offset < 0 or offset + 8 + entries * 4 > len(payload):
        return []
    if payload[offset : offset + 4] != b"sf32":
        return []
    return [struct.unpack_from(">i", payload, offset + 8 + index * 4)[0] / 65536.0 for index in range(entries)]


def _parse_mhc2(
    payload: bytes,
) -> tuple[float, float, tuple[float, ...], list[float], list[float], list[float]] | None:
    if len(payload) < 36 or payload[:4] != b"MHC2":
        return None
    entries = struct.unpack_from(">I", payload, 8)[0]
    if entries == 1 or entries > 4096:
        return None
    minimum_nits = struct.unpack_from(">i", payload, 12)[0] / 65536.0
    peak_nits = struct.unpack_from(">i", payload, 16)[0] / 65536.0
    matrix_offset, red_offset, green_offset, blue_offset = struct.unpack_from(">IIII", payload, 20)

    identity_matrix = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    if matrix_offset == 0:
        matrix = identity_matrix
    else:
        # Compatibility with pre-1.0 development profiles that wrote every
        # MHC2 payload offset four bytes too far forward.
        curve_offsets = (red_offset, green_offset, blue_offset)
        nonzero_curve_offsets = tuple(offset for offset in curve_offsets if offset)
        if nonzero_curve_offsets and not all(payload[offset : offset + 4] == b"sf32" for offset in nonzero_curve_offsets):
            shifted = tuple(offset - 4 if offset else 0 for offset in curve_offsets)
            nonzero_shifted = tuple(offset for offset in shifted if offset)
            if matrix_offset >= 4 and nonzero_shifted and all(
                offset >= 0 and payload[offset : offset + 4] == b"sf32" for offset in nonzero_shifted
            ):
                matrix_offset -= 4
                red_offset, green_offset, blue_offset = shifted
        if matrix_offset + 48 > len(payload):
            return None
        matrix = tuple(
            struct.unpack_from(">i", payload, matrix_offset + index * 4)[0] / 65536.0
            for index in range(12)
        )

    if entries == 0:
        red = green = blue = [0.0, 1.0]
    else:
        def curve_or_identity(offset: int) -> list[float]:
            return [0.0, 1.0] if offset == 0 else _parse_sf32_curve(payload, offset, entries)

        red = curve_or_identity(red_offset)
        green = curve_or_identity(green_offset)
        blue = curve_or_identity(blue_offset)
        if not red or not green or not blue:
            return None
    return minimum_nits, peak_nits, matrix, red, green, blue


def _parse_xyz_y(payload: bytes) -> float | None:
    if len(payload) < 20 or payload[:4] != b"XYZ ":
        return None
    return struct.unpack_from(">i", payload, 12)[0] / 65536.0


def _parse_xyz(payload: bytes) -> tuple[float, float, float] | None:
    if len(payload) < 20 or payload[:4] != b"XYZ ":
        return None
    return tuple(struct.unpack_from(">i", payload, 8 + 4 * i)[0] / 65536.0 for i in range(3))


def _parse_chad(payload: bytes) -> tuple[float, ...] | None:
    """The chromatic adaptation matrix, as nine s15Fixed16 values after an 8 byte header."""
    if len(payload) < 44 or payload[:4] != b"sf32":
        return None
    return tuple(struct.unpack_from(">i", payload, 8 + 4 * i)[0] / 65536.0 for i in range(9))


def _to_xy(xyz: tuple[float, float, float]) -> tuple[float, float]:
    total = sum(xyz)
    if total <= 0.0:
        return (0.0, 0.0)
    return (xyz[0] / total, xyz[1] / total)


def profile_primaries_xy(
    profile: bytes,
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]] | None:
    """The display primaries and white a profile describes, as CIE xy chromaticities.

    ICC stores colorant tags adapted to the D50 profile connection space, so the raw
    rXYZ/gXYZ/bXYZ values are not the display's primaries and comparing them against
    anything measured would be wrong. The profile's own ``chad`` is inverted to undo
    that adaptation.

    A profile without a chad is **not** unadapted, which is what this used to assume.
    ICC colorants are D50-relative whether or not the file bothers to say so, and a v2
    display profile that omits chad is relying on exactly that convention. Reading them
    raw put the primaries 0.021 to 0.025 off across the eight chad-less profiles
    installed on the machine this was found on -- four to five times
    PRIMARY_MISMATCH_THRESHOLD_XY -- so a base that describes its panel perfectly still
    reported a gamut mismatch. Absent a chad, undo the standard D65-to-D50 adaptation
    instead of undoing nothing.

    Returns ``None`` when the profile has no colorant tags at all, which is normal for
    LUT-based profiles.
    """
    try:
        tags = _read_tags(profile)
    except ValueError:
        return None
    try:
        red = _parse_xyz(tags[b"rXYZ"])
        green = _parse_xyz(tags[b"gXYZ"])
        blue = _parse_xyz(tags[b"bXYZ"])
        white = _parse_xyz(tags[b"wtpt"])
    except KeyError:
        return None
    if red is None or green is None or blue is None or white is None:
        return None

    chad = _parse_chad(tags.get(b"chad", b"")) or D65_TO_D50_CHAD
    try:
        undo = _inverse3(chad)
    except ValueError:
        return None
    # The colorants only. Those v2 files store wtpt as the real media white rather
    # than the PCS illuminant, so putting it through the same matrix turns a D65 white
    # into 0.2806,0.2959, which is not a white at all.
    red, green, blue = (_matrix_vector(undo, vector) for vector in (red, green, blue))
    if tags.get(b"chad"):
        white = _matrix_vector(undo, white)
    return (_to_xy(red), _to_xy(green), _to_xy(blue), _to_xy(white))


# A matching profile and panel agree to about 0.00005 xy on a real display, while the
# smallest gamut change a monitor's OSD can make -- DCI-P3 to BT.709 -- moves red by 0.035.
# Anything between those is a comfortable threshold; this sits ~100x above the noise and
# ~7x below the smallest real change.
PRIMARY_MISMATCH_THRESHOLD_XY = 0.005


def primaries_disagree(
    profile_primaries: tuple[tuple[float, float], ...],
    panel_primaries: tuple[tuple[float, float], ...],
    *,
    threshold: float = PRIMARY_MISMATCH_THRESHOLD_XY,
) -> float:
    """Largest per-primary xy distance between a profile and a panel, or 0.0 if they agree.

    A monitor's gamut mode lives in its own OSD, where nothing on the PC can observe it
    changing. Switch a display from DCI-P3 to sRGB and every HDR profile silently
    describes the wrong panel, with no error anywhere. Comparing the two readings is the
    only way to notice.
    """
    worst = 0.0
    for profile_xy, panel_xy in zip(profile_primaries[:3], panel_primaries[:3]):
        distance = math.hypot(profile_xy[0] - panel_xy[0], profile_xy[1] - panel_xy[1])
        worst = max(worst, distance)
    return worst if worst > threshold else 0.0


def _parse_vcgt(payload: bytes) -> tuple[list[float], list[float], list[float]] | None:
    if len(payload) < 18 or payload[:4] != b"vcgt":
        return None
    gamma_type, channels, entries, entry_size = struct.unpack_from(">IHHH", payload, 8)
    if gamma_type != 0 or channels != 3 or entries < 2 or entry_size not in (1, 2):
        return None
    offset = 18
    expected = offset + channels * entries * entry_size
    if expected > len(payload):
        return None
    curves: list[list[float]] = []
    for _ in range(3):
        channel: list[float] = []
        for _index in range(entries):
            if entry_size == 1:
                value = payload[offset] / 255.0
            else:
                value = struct.unpack_from(">H", payload, offset)[0] / 65535.0
            offset += entry_size
            channel.append(value)
        curves.append(channel)
    return curves[0], curves[1], curves[2]


def _estimate_state_from_curves(mode: DisplayMode, curves: tuple[list[float], list[float], list[float]]) -> ModeState:
    state = ModeState.neutral(mode)
    red, green, blue = curves
    # Our traditional Gamma control serializes a common power curve y=x^(gamma/2.2).
    # estimate_curve_gamma returns the reciprocal exponent, so convert back to the
    # user-facing gamma value. This is only approximate for third-party profiles.
    reciprocal_powers = [max(1e-6, estimate_curve_gamma(curve)) for curve in curves]
    average_reciprocal = sum(reciprocal_powers) / len(reciprocal_powers)
    state.gamma = max(1.6, min(3.0, 2.2 / average_reciprocal))
    middle = min(len(red), len(green), len(blue)) // 2
    neutral = max(1e-6, (red[middle] + green[middle] + blue[middle]) / 3.0)
    state.red_channel = max(-25.0, min(25.0, (red[middle] / neutral - 1.0) * 100.0))
    state.green_channel = max(-25.0, min(25.0, (green[middle] / neutral - 1.0) * 100.0))
    state.blue_channel = max(-25.0, min(25.0, (blue[middle] / neutral - 1.0) * 100.0))
    # Imported two-point MHC2 profiles must never constrain subsequent edits to two endpoints.
    state.lut_entries = 4096
    return state


def import_profile(path: Path, fallback_mode: DisplayMode) -> ImportedProfile:
    data = path.read_bytes()
    tags = _read_tags(data)
    description = _parse_text(tags.get(b"desc", b"")) or path.stem
    warnings: list[str] = []

    embedded = _parse_text(tags.get(b"sdhs", b""))
    if embedded:
        try:
            decoded = json.loads(embedded)
            embedded_mode: DisplayMode = "HDR" if decoded.get("mode") == "HDR" else "SDR"
            payload = dict(decoded.get("state", {}))
            if decoded.get("schema") != "sdr-hdr-profile-state-v2":
                legacy_gamma = max(0.05, float(payload.get("gamma", 1.0)))
                payload["gamma"] = math.log2(legacy_gamma)
                payload["saturation"] = float(payload.get("saturation", 100.0)) - 100.0
            state = ModeState.from_dict(payload, embedded_mode)
            state.imported_profile = str(path)
            return ImportedProfile(
                mode=embedded_mode,
                state=state,
                exact_state=True,
                tags=tuple(signature.decode("ascii", "replace") for signature in tags),
                description=description,
                warnings=(),
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            warnings.append("The embedded app state tag could not be decoded.")

    parsed_mhc2 = _parse_mhc2(tags.get(b"MHC2", b""))
    if parsed_mhc2:
        minimum_nits, peak_nits, matrix, red, green, blue = parsed_mhc2
        state = _estimate_state_from_curves(fallback_mode, (red, green, blue))
        state.minimum_luminance_nits = max(0.0, min(100.0, minimum_nits))
        state.peak_luminance_nits = max(80.0, min(10000.0, peak_nits))
        full_frame = _parse_xyz_y(tags.get(b"lumi", b""))
        if full_frame is not None:
            state.full_frame_luminance_nits = max(80.0, min(state.peak_luminance_nits, full_frame))
        # A third-party XYZ matrix may contain calibration, primaries adaptation, or
        # vendor-specific transforms that cannot be safely decomposed into our fine
        # Temperature/Tint/Saturation controls. Keep those user corrections neutral.
        state.temperature = 0.0
        state.tint = 0.0
        state.saturation = 0.0
        warnings.append(
            "External MHC2 profile: colorimetry and vendor tags are preserved; Gamma/RGB recovery from its 1D LUTs is approximate, while Temperature/Tint/Saturation start neutral."
        )
    else:
        parsed_vcgt = _parse_vcgt(tags.get(b"vcgt", b""))
        if parsed_vcgt:
            state = _estimate_state_from_curves(fallback_mode, parsed_vcgt)
            warnings.append("External vcgt profile: slider recovery is an approximation derived from its calibration curves.")
        else:
            state = ModeState.neutral(fallback_mode)
            warnings.append("No recoverable MHC2, vcgt, or embedded app state was found; neutral sliders were loaded.")

    state.profile_name = description
    state.imported_profile = str(path)
    state.base_profile = str(path)
    # The FILENAME, not the ICC description. Every consumer of base_profile_name
    # treats it as a name it can hand back to Windows: the app reapplies it as a
    # default association, and the watchdog checks it against the colour directory
    # to avoid capturing an app-managed profile as its HDR fallback. A description
    # like "HDR Calibrated Profile 8/14/2026 132247" is not a filename, and the
    # slashes in it make it an invalid path, so both silently did nothing.
    state.base_profile_name = path.name
    return ImportedProfile(
        mode=fallback_mode,
        state=state,
        exact_state=False,
        tags=tuple(signature.decode("ascii", "replace") for signature in tags),
        description=description,
        warnings=tuple(warnings),
    )
