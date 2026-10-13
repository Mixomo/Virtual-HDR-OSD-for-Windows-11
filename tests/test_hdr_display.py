"""Behaviour of the HDR presentation layer that does not need HDR hardware.

The swapchain itself cannot be exercised without a display, but everything that decides
*what* to send it -- luminance encoding, which output a window is on, and whether a
panel's own metadata is trustworthy -- is pure and is tested here.
"""

from __future__ import annotations

import struct
import unittest
from unittest import mock

from sdr_hdr_profile_creator import hdr_display
from sdr_hdr_profile_creator.hdr_display import (
    DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709,
    DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
    DisplayCapability,
    nits_to_scrgb,
    scrgb_pixel,
)


def capability(**overrides) -> DisplayCapability:
    base = dict(
        device_name="\\\\.\\DISPLAY1", left=0, top=0, right=3840, bottom=2160,
        bits_per_color=10, color_space=DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
        min_nits=0.0, max_nits=1080.0, max_full_frame_nits=1080.0,
        red_primary=(0.67, 0.31), green_primary=(0.27, 0.69),
        blue_primary=(0.15, 0.06), white_point=(0.3127, 0.329),
    )
    base.update(overrides)
    return DisplayCapability(**base)


class LuminanceEncodingTests(unittest.TestCase):
    def test_scrgb_one_is_eighty_nits(self):
        """The whole encoding hangs off this constant; Direct2D names it explicitly."""
        self.assertAlmostEqual(nits_to_scrgb(80.0), 1.0)

    def test_absolute_nits_scale_linearly(self):
        for nits, expected in ((0.0, 0.0), (203.0, 2.5375), (1000.0, 12.5), (4000.0, 50.0)):
            with self.subTest(nits=nits):
                self.assertAlmostEqual(nits_to_scrgb(nits), expected, places=6)

    def test_negative_luminance_is_clamped_not_wrapped(self):
        self.assertEqual(nits_to_scrgb(-5.0), 0.0)

    def test_a_pixel_is_four_half_floats(self):
        raw = scrgb_pixel(12.5, 12.5, 12.5)
        self.assertEqual(len(raw), 8, "R16G16B16A16_FLOAT is 8 bytes per pixel")
        self.assertEqual(struct.unpack("<4e", raw), (12.5, 12.5, 12.5, 1.0))

    def test_values_above_diffuse_white_survive_encoding(self):
        """The point of FP16: scRGB above 1.0 is legal and must not clip."""
        red, green, blue, _alpha = struct.unpack("<4e", scrgb_pixel(*(nits_to_scrgb(4000.0),) * 3))
        self.assertAlmostEqual(red, 50.0)
        self.assertEqual((red, green, blue), (red, red, red))


class DisplayKindTests(unittest.TestCase):
    def test_hdr10_output_reports_hdr(self):
        self.assertTrue(capability().is_hdr)

    def test_sdr_output_does_not_report_hdr(self):
        sdr = capability(color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709)
        self.assertFalse(sdr.is_hdr)

    def test_wcg_is_indistinguishable_from_sdr_here(self):
        """DXGI reports auto-colour-managed SDR displays as plain SDR.

        Recorded as behaviour so nobody later reads is_hdr as "not advanced colour".
        """
        wcg = capability(color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709, bits_per_color=10)
        self.assertFalse(wcg.is_hdr)


class MetadataCredibilityTests(unittest.TestCase):
    """Cheap panels report absent or nonsense luminance; calibrating to it would be worse
    than admitting we do not know."""

    def test_a_real_panel_is_credible(self):
        self.assertTrue(capability().luminance_is_credible)

    def test_zero_peak_is_not_credible(self):
        self.assertFalse(capability(max_nits=0.0).luminance_is_credible)

    def test_peak_beyond_st2084_is_not_credible(self):
        self.assertFalse(capability(max_nits=65535.0).luminance_is_credible)

    def test_missing_full_frame_is_not_credible(self):
        self.assertFalse(capability(max_full_frame_nits=0.0).luminance_is_credible)

    def test_sdr_mode_luminance_is_never_credible(self):
        """DXGI describes the mode the output is in, not the panel.

        Observed on a real display: HDR off reports 240/240 nits and BT.709 primaries,
        HDR on reports 1080/1080 and DCI-P3. 240 is the SDR reference white and passes
        any plain range check, so a calibration step would target it as if it were peak.
        """
        with_hdr_off = capability(
            color_space=DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709,
            max_nits=240.0, max_full_frame_nits=240.0,
        )
        self.assertFalse(with_hdr_off.luminance_is_credible)

    def test_the_same_panel_is_credible_once_hdr_is_on(self):
        self.assertTrue(capability(max_nits=1080.0, max_full_frame_nits=1080.0).luminance_is_credible)


def _signed(code: int) -> int:
    """An HRESULT as the c_long a COM call returns."""
    return code - (1 << 32) if code & 0x80000000 else code


class PresentResultTests(unittest.TestCase):
    """What present() does with Present's answer, which used to be thrown away.

    The COM call layer is faked, so this runs without a GPU: the swapchain answers with
    a chosen HRESULT and everything else succeeds."""

    def present_answering(self, code):
        import ctypes

        surface = object.__new__(hdr_display.HdrSurface)
        surface._device, surface._context = ctypes.c_void_p(1), ctypes.c_void_p(2)
        surface._swapchain = ctypes.c_void_p(3)
        surface._width = surface._height = 2
        surface._hwnd = 0

        def fake_vcall(pointer, index, _argtypes, *_args):
            if pointer is surface._swapchain and index == hdr_display._PRESENT:
                return _signed(code)
            return 0

        with mock.patch.object(hdr_display, "_vcall", fake_vcall), \
                mock.patch.object(hdr_display, "_release", lambda _pointer: None):
            surface.present(bytes(surface.stride * surface._height))

    def test_success_is_quiet(self):
        self.present_answering(0)

    def test_a_lost_device_says_what_happened(self):
        for code, word in ((0x887A0005, "removed"), (0x887A0006, "hung"), (0x887A0007, "reset")):
            with self.subTest(word=word):
                with self.assertRaises(hdr_display.HdrDisplayError) as caught:
                    self.present_answering(code)
                self.assertIn(word, str(caught.exception))

    def test_any_other_failure_still_raises(self):
        with self.assertRaises(hdr_display.HdrDisplayError) as caught:
            self.present_answering(0x80004005)
        self.assertIn("0x80004005", str(caught.exception))


class WindowAssociationTests(unittest.TestCase):
    """DXGI must not act on Alt+Enter for the measurement window.

    Left associated, Alt+Enter asks DXGI to take the window fullscreen on its own -- a
    display mode change in the middle of a patch, under the meter."""

    def test_alt_enter_and_window_changes_are_taken_away_from_dxgi(self):
        calls = []

        def fake_vcall(pointer, index, _argtypes, *args):
            if index in (hdr_display._QUERY_INTERFACE, hdr_display._GET_PARENT,
                         hdr_display._CREATE_SWAPCHAIN_FOR_HWND):
                args[-1]._obj.value = 100 + len(calls)   # the out-parameter
            calls.append((pointer.value, index, args))
            return 0

        class FakeD3D11:
            def D3D11CreateDevice(self, *args):
                args[7]._obj.value, args[9]._obj.value = 1, 2   # device, context
                return 0

        with mock.patch.object(hdr_display.ctypes, "WinDLL", lambda _name: FakeD3D11()), \
                mock.patch.object(hdr_display, "_vcall", fake_vcall), \
                mock.patch.object(hdr_display, "_release", lambda _pointer: None), \
                mock.patch.object(hdr_display, "IS_WINDOWS", True):
            hdr_display.HdrSurface(0x1234, 4, 4)

        factory = next(value for value, index, args in calls if index == hdr_display._CREATE_SWAPCHAIN_FOR_HWND)
        associations = [args for value, index, args in calls
                        if index == hdr_display._MAKE_WINDOW_ASSOCIATION and value == factory]
        self.assertEqual(1, len(associations), "the factory that made the swapchain was never told")
        hwnd, flags = associations[0]
        self.assertEqual(0x1234, hwnd.value)
        self.assertEqual(
            hdr_display._DXGI_MWA_NO_ALT_ENTER | hdr_display._DXGI_MWA_NO_WINDOW_CHANGES, flags
        )


@unittest.skipUnless(struct.calcsize("P") == 8, "layouts are pinned for 64-bit Windows")
class StructLayoutTests(unittest.TestCase):
    """The DXGI and D3D11 structs, against their SDK definitions.

    DXGI fills GetDesc1's struct and reads CreateSwapChainForHwnd's without checking the
    caller's layout. A wrong one is not rejected: the fields after the mistake are read
    from the wrong offsets. Sizes follow the definitions on Microsoft Learn under x64
    alignment: 4-byte UINT, BOOL and enums, 8-byte pointers and HMONITOR.
    """

    def test_struct_sizes_match_the_sdk(self):
        import ctypes

        expected = {
            "_GUID": 16,
            "_RECT": 16,
            "_DXGI_OUTPUT_DESC1": 152,
            "_DXGI_SAMPLE_DESC": 8,
            "_DXGI_SWAP_CHAIN_DESC1": 48,
            "_D3D11_TEXTURE2D_DESC": 44,
            "_D3D11_SUBRESOURCE_DATA": 16,
        }
        for name, size in expected.items():
            with self.subTest(struct=name):
                self.assertEqual(size, ctypes.sizeof(getattr(hdr_display, name)))

    def test_the_output_fields_the_capability_reads_are_where_dxgi_writes_them(self):
        """HMONITOR is 8-byte aligned, so everything after it moves if it is declared as
        a 4-byte handle."""
        desc = hdr_display._DXGI_OUTPUT_DESC1
        expected = {
            "DesktopCoordinates": 64, "Monitor": 88, "BitsPerColor": 96, "ColorSpace": 100,
            "RedPrimary": 104, "GreenPrimary": 112, "BluePrimary": 120, "WhitePoint": 128,
            "MinLuminance": 136, "MaxLuminance": 140, "MaxFullFrameLuminance": 144,
        }
        for field, offset in expected.items():
            with self.subTest(field=field):
                self.assertEqual(offset, getattr(desc, field).offset)


if __name__ == "__main__":
    unittest.main()
