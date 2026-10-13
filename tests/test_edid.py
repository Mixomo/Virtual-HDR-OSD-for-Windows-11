"""Reading a display's HDR declaration from its own EDID.

Windows' DXGI_OUTPUT_DESC1 is the obvious source and gets two of the three figures wrong.
Measured against a real panel declaring 1015.24 peak, 265.05 maximum frame-average and
0.0002 minimum, DXGI reported 1010.40, 1010.40 and 0.1956 -- peak roughly right,
frame-average replaced by peak, minimum four hundred times too high.
"""

from __future__ import annotations

import unittest

from sdr_hdr_profile_creator.edid import (
    PanelMetadata,
    parse_hdr_static_metadata,
    _luminance_from_code,
    parse_chromaticity,
    read_panel_metadata,
)


def cta_edid(payload: bytes) -> bytes:
    """A minimal two-block EDID carrying one CTA data block."""
    base = bytes(128)
    block = bytes([(7 << 5) | len(payload)]) + payload
    # Byte 2 is the offset of the first detailed timing descriptor; the data block
    # collection runs from byte 4 up to it.
    extension = bytearray(128)
    extension[0] = 0x02
    extension[1] = 0x03
    extension[2] = 4 + len(block)
    extension[4:4 + len(block)] = block
    return base + bytes(extension)


def hdr_block(eotf=0x05, descriptor=0x00, peak=139, frame_average=77, minimum=1) -> bytes:
    return bytes([0x06, eotf, descriptor, peak, frame_average, minimum])


class LuminanceDecodingTests(unittest.TestCase):
    """CTA-861-G section 7.5.13: codes are logarithmic, minimum is a fraction of maximum."""

    def parsed(self, **overrides) -> PanelMetadata:
        result = parse_hdr_static_metadata(cta_edid(hdr_block(**overrides)))
        self.assertIsNotNone(result)
        return result

    def test_the_real_panels_codes_decode_to_its_real_figures(self):
        """Codes 139 and 77 are what one PG32UCDM declares."""
        panel = self.parsed()
        self.assertAlmostEqual(panel.peak_nits, 1015.24, places=1)
        self.assertAlmostEqual(panel.max_frame_average_nits, 265.05, places=1)

    def test_a_zero_byte_is_an_unfilled_field_rather_than_fifty_nits(self):
        """The formula really does give 50 for code 0, and _luminance_from_code
        still says so. But a panel that sends the full block and leaves the byte
        at 0x00 has stated nothing -- CTA-861's way of declining is a shorter
        block, which the parser handles by length. Reading it literally was
        actively harmful: 50 cleared the 40-nit credibility floor so the figure
        was believed, it is truthy so every `frame_average or peak` fallback was
        skipped, and the 80-nit clamps turned peak and sustained alike into
        exactly 80 on a 1000-nit display."""
        self.assertAlmostEqual(_luminance_from_code(0), 50.0, places=4)
        self.assertEqual(self.parsed(peak=0).peak_nits, 0.0)
        self.assertFalse(self.parsed(peak=0, frame_average=0).credible)

    def test_thirty_two_codes_is_a_doubling(self):
        self.assertAlmostEqual(self.parsed(peak=32).peak_nits, 100.0, places=4)
        self.assertAlmostEqual(self.parsed(peak=64).peak_nits, 200.0, places=4)

    def test_minimum_is_a_squared_fraction_of_maximum(self):
        panel = self.parsed(peak=139, minimum=1)
        self.assertAlmostEqual(panel.min_nits, 1015.24 * (1 / 255) ** 2 / 100, places=6)

    def test_pq_support_is_read_from_the_eotf_bits(self):
        self.assertTrue(self.parsed(eotf=0x05).supports_pq)
        self.assertFalse(self.parsed(eotf=0x01).supports_pq)


class BlockShapeTests(unittest.TestCase):
    def test_an_edid_with_no_hdr_block_reports_nothing(self):
        other = bytes([0x05, 0x00, 0x00])   # an extended block that is not HDR metadata
        self.assertIsNone(parse_hdr_static_metadata(cta_edid(other)))

    def test_an_edid_with_no_extension_reports_nothing(self):
        self.assertIsNone(parse_hdr_static_metadata(bytes(128)))

    def test_a_truncated_edid_reports_nothing_rather_than_raising(self):
        self.assertIsNone(parse_hdr_static_metadata(b"\x00" * 40))

    def test_a_block_carrying_only_a_maximum_still_reads(self):
        """Only the maximum is mandatory once the block is present."""
        panel = parse_hdr_static_metadata(cta_edid(bytes([0x06, 0x05, 0x00, 139])))
        self.assertIsNotNone(panel)
        self.assertAlmostEqual(panel.peak_nits, 1015.24, places=1)
        self.assertEqual(panel.max_frame_average_nits, 0.0)
        self.assertEqual(panel.min_nits, 0.0)

    def test_a_block_too_short_to_be_hdr_metadata_is_skipped(self):
        self.assertIsNone(parse_hdr_static_metadata(cta_edid(bytes([0x06, 0x05]))))


class CredibilityTests(unittest.TestCase):
    """A declaration is only worth preferring over DXGI if it actually answered."""

    def test_a_real_declaration_is_credible(self):
        self.assertTrue(parse_hdr_static_metadata(cta_edid(hdr_block())).credible)

    def test_a_panel_claiming_no_pq_is_not(self):
        self.assertFalse(parse_hdr_static_metadata(cta_edid(hdr_block(eotf=0x01))).credible)

    def test_an_absurd_peak_is_not(self):
        panel = PanelMetadata(peak_nits=90000.0, max_frame_average_nits=100.0,
                              min_nits=0.0, supports_pq=True)
        self.assertFalse(panel.credible)

    def test_a_peak_below_any_real_display_is_not(self):
        panel = PanelMetadata(peak_nits=10.0, max_frame_average_nits=5.0,
                              min_nits=0.0, supports_pq=True)
        self.assertFalse(panel.credible)


class DevicePathTests(unittest.TestCase):
    """Every failure is just "no answer"; the caller's fallback is the same in each case."""

    def test_a_path_that_is_not_a_monitor_reports_nothing(self):
        self.assertIsNone(read_panel_metadata("not a device path"))

    def test_an_empty_path_reports_nothing(self):
        self.assertIsNone(read_panel_metadata(""))

    def test_an_unknown_monitor_reports_nothing_rather_than_raising(self):
        self.assertIsNone(read_panel_metadata(
            r"\\?\DISPLAY#NOSUCH000#1&00000000&0&UID0#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"))


class FrameAverageTests(unittest.TestCase):
    """The figure DXGI discards, and the reason this module exists."""

    def test_frame_average_is_read_separately_from_peak(self):
        panel = parse_hdr_static_metadata(cta_edid(hdr_block(peak=139, frame_average=77)))
        self.assertLess(panel.max_frame_average_nits, panel.peak_nits / 3,
                        "frame-average came out near peak, which is what DXGI does wrong")

    def test_an_emissive_panels_gap_survives_the_round_trip(self):
        """1015 peak against 265 sustained is the whole point: a tool that treats peak as
        the full-screen figure asks for four times the light the panel can hold."""
        panel = parse_hdr_static_metadata(cta_edid(hdr_block()))
        self.assertAlmostEqual(panel.peak_nits / panel.max_frame_average_nits, 3.83, places=1)


def encode_chromaticity(values):
    """Build EDID base-block bytes 0x19-0x22 for eight xy coordinates.

    Each coordinate is 10 bits: the high eight in its own byte, the low two
    packed into one of two shared bytes.
    """
    codes = [max(0, min(1023, round(value * 1024.0))) for value in values]
    rx, ry, gx, gy, bx, by, wx, wy = codes
    low_rg = ((rx & 3) << 6) | ((ry & 3) << 4) | ((gx & 3) << 2) | (gy & 3)
    low_bw = ((bx & 3) << 6) | ((by & 3) << 4) | ((wx & 3) << 2) | (wy & 3)
    block = bytearray(0x23)
    block[0x19] = low_rg
    block[0x1A] = low_bw
    for index, code in enumerate(codes):
        block[0x1B + index] = code >> 2
    return bytes(block)


class ChromaticityTests(unittest.TestCase):
    """The panel's own gamut, which is the only source DXGI cannot contaminate."""

    # Read from the development panel's real EDID.
    PANEL = (0.68359375, 0.3046875, 0.244140625, 0.708984375,
             0.1435546875, 0.0556640625, 0.3134765625, 0.3291015625)

    def test_round_trips_the_panel_it_was_read_from(self):
        self.assertEqual(parse_chromaticity(encode_chromaticity(self.PANEL)), self.PANEL)

    def test_resolution_is_about_a_thousandth_in_xy(self):
        """10 bits per coordinate. Coarser than the float DXGI reports, and
        unlike it, describing the panel rather than the profile in force."""
        decoded = parse_chromaticity(encode_chromaticity((0.6800, 0.3200, 0.2650, 0.6900,
                                                          0.1500, 0.0600, 0.3127, 0.3290)))
        for got, wanted in zip(decoded, (0.6800, 0.3200, 0.2650, 0.6900,
                                         0.1500, 0.0600, 0.3127, 0.3290)):
            self.assertAlmostEqual(got, wanted, delta=0.001)

    def test_a_truncated_edid_yields_nothing_rather_than_raising(self):
        self.assertEqual(parse_chromaticity(b"\x00" * 16), ())

    def test_an_all_zero_block_is_rejected(self):
        """A panel that answers with zeros has not answered, and each y divides
        when the coordinates are converted to XYZ."""
        self.assertEqual(parse_chromaticity(bytes(0x23)), ())

    def test_the_panel_gamut_is_wider_than_p3(self):
        """A QD-OLED exceeds P3, and a check that quietly assumed otherwise
        would reject the display it was written for."""
        values = parse_chromaticity(encode_chromaticity(self.PANEL))
        rx, ry, gx, gy, bx, by = values[:6]
        area = abs((gx - rx) * (by - ry) - (bx - rx) * (gy - ry)) / 2
        self.assertGreater(area, 0.1520)   # DCI-P3
        self.assertLess(area, 0.2119)      # BT.2020, which bounds anything real


class UnstatedLuminanceTests(unittest.TestCase):
    """A panel that carries the block but leaves the luminance bytes at zero.

    Those three bytes are optional, so zero means "not stated". Read literally
    through 50 * 2^(code/32) it came out as 50 nits, which cleared the 40-nit
    credibility floor, was truthy so every ``frame_average or peak`` fallback was
    skipped, and was then raised to exactly 80 by the clamps. A 1000-nit panel
    would have been calibrated as an 80-nit one, with Windows tone-mapping
    everything to a twelfth of what it can do.
    """

    def synthetic(self, peak_code, average_code, minimum_code, length=6):
        base = bytearray(128)
        extension = bytearray(128)
        extension[0] = 0x02          # CTA-861 extension
        extension[2] = 20            # first detailed timing descriptor
        extension[4] = (7 << 5) | length
        extension[5] = 0x06          # HDR static metadata
        extension[6] = 0x04          # ET_2 = PQ
        extension[7] = 0x00
        extension[8] = peak_code
        extension[9] = average_code
        extension[10] = minimum_code
        return bytes(base + extension)

    def test_the_formula_stays_faithful_to_the_specification(self):
        """50 * 2^(0/32) really is 50. The judgement about an unfilled byte
        belongs in the parser, not in the arithmetic."""
        self.assertAlmostEqual(_luminance_from_code(0), 50.0, places=4)

    def test_a_nonzero_code_is_unaffected(self):
        self.assertAlmostEqual(_luminance_from_code(1), 51.095, places=2)
        self.assertAlmostEqual(_luminance_from_code(0x9A), 1405.0, places=0)

    def test_a_block_with_no_luminance_declared_is_not_credible(self):
        panel = parse_hdr_static_metadata(self.synthetic(0, 0, 0))
        self.assertIsNotNone(panel)
        self.assertEqual(panel.peak_nits, 0.0)
        self.assertEqual(panel.max_frame_average_nits, 0.0)
        self.assertFalse(panel.credible)

    def test_a_panel_that_does_declare_figures_is_credible(self):
        panel = parse_hdr_static_metadata(self.synthetic(0x9A, 0x76, 0x08))
        self.assertTrue(panel.credible)
        self.assertGreater(panel.peak_nits, 1000.0)
        self.assertGreater(panel.max_frame_average_nits, 100.0)

    def test_the_development_panels_own_figures_still_parse(self):
        """1015.24 / 265.05 / 0.000156, read from the real display."""
        panel = parse_hdr_static_metadata(self.synthetic(0x92, 0x6D, 0x0A))
        self.assertTrue(panel.credible)
        self.assertGreater(panel.peak_nits, 900.0)
        self.assertLess(panel.max_frame_average_nits, panel.peak_nits / 2)


class ChromaticityInTheBaseBlockTests(unittest.TestCase):
    """The panel's gamut, read from the base block rather than from DXGI.

    Every fixture in this file built its base block as 128 zero bytes, which
    parse_chromaticity rejects outright -- so `primaries=parse_chromaticity(edid)` in
    parse_hdr_static_metadata returned () at every call site and replacing it with
    `primaries=()` was an *equivalent* mutation. Nothing here could see the difference,
    and every other module mocks read_panel_metadata with a hand-built PanelMetadata.
    """

    # The panel this fork was developed against, as it states itself.
    PG32UCDM = (0.6836, 0.3047, 0.2441, 0.7090, 0.1436, 0.0557, 0.3135, 0.3291)

    @staticmethod
    def encode_chromaticity(values) -> bytes:
        """The inverse of parse_chromaticity: ten bytes for 0x19-0x22.

        Each coordinate is ten bits -- eight in its own byte, the low two packed into
        one of two shared bytes -- so the resolution is 1/1024 and a round trip is only
        exact to about 0.001.
        """
        codes = [min(1023, max(0, round(value * 1024.0))) for value in values]
        low_rg = ((codes[0] & 3) << 6) | ((codes[1] & 3) << 4) | \
                 ((codes[2] & 3) << 2) | (codes[3] & 3)
        low_bw = ((codes[4] & 3) << 6) | ((codes[5] & 3) << 4) | \
                 ((codes[6] & 3) << 2) | (codes[7] & 3)
        return bytes([low_rg, low_bw] + [code >> 2 for code in codes])

    def edid_with(self, values, peak_code=0xBE, average_code=0x9C, minimum_code=0x08):
        """A two-block EDID: real chromaticity in the base, HDR metadata in the CTA."""
        base = bytearray(128)
        base[0x19:0x23] = self.encode_chromaticity(values)

        extension = bytearray(128)
        extension[0] = 0x02
        extension[2] = 20
        extension[4] = (7 << 5) | 6
        extension[5] = 0x06          # HDR static metadata
        extension[6] = 0x04          # ET_2 = PQ
        extension[7] = 0x00
        extension[8] = peak_code
        extension[9] = average_code
        extension[10] = minimum_code
        return bytes(base) + bytes(extension)

    def test_the_base_block_round_trips_through_the_encoder(self):
        """If this fails the fixture is wrong, not the code under test."""
        parsed = parse_chromaticity(self.edid_with(self.PG32UCDM))
        self.assertEqual(8, len(parsed))
        for expected, actual in zip(self.PG32UCDM, parsed):
            self.assertAlmostEqual(expected, actual, delta=1 / 1024)

    def test_the_declaration_carries_the_panels_own_gamut(self):
        """The assertion the file could not make before: parse_hdr_static_metadata
        surfaces the base block's primaries, so dropping that argument fails here."""
        panel = parse_hdr_static_metadata(self.edid_with(self.PG32UCDM))
        self.assertIsNotNone(panel)
        self.assertEqual(8, len(panel.primaries), "the panel's gamut was not carried")
        for expected, actual in zip(self.PG32UCDM, panel.primaries):
            self.assertAlmostEqual(expected, actual, delta=1 / 1024)

    def test_a_wide_gamut_is_not_flattened_to_something_generic(self):
        """The value of reading the panel at all: this display is wider than P3, and a
        BT.709 or BT.2020 stand-in would describe a different display."""
        panel = parse_hdr_static_metadata(self.edid_with(self.PG32UCDM))
        red_x, green_y = panel.primaries[0], panel.primaries[3]
        self.assertGreater(red_x, 0.67, "red was not the panel's own")
        self.assertGreater(green_y, 0.69, "green was not the panel's own")

    def test_a_zeroed_base_block_reports_no_gamut_rather_than_a_wrong_one(self):
        """The other half of the rule, and the state every other fixture is in: a panel
        that answered with zeros has not answered, and must not be taken at its word."""
        blank = bytearray(self.edid_with(self.PG32UCDM))
        blank[0x19:0x23] = bytes(10)
        panel = parse_hdr_static_metadata(bytes(blank))
        self.assertEqual((), panel.primaries)

    def test_a_coordinate_outside_the_diagram_is_refused(self):
        """Each y also divides when converting to XYZ, so a zero there is fatal."""
        impossible = list(self.PG32UCDM)
        impossible[3] = 0.0          # green y
        panel = parse_hdr_static_metadata(self.edid_with(impossible))
        self.assertEqual((), panel.primaries)


if __name__ == "__main__":
    unittest.main()
