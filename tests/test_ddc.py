"""The DDC/CI read layer, against a simulated link.

Nothing here opens a monitor. Every claim about the hardware came from probing an ASUS
PG32UCDM directly: 25 VCP codes answer, stable across two reads, and a first
single-attempt probe reported brightness, contrast and red and green gain as
unsupported when they were not.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sdr_hdr_profile_creator import ddc  # noqa: E402


class FakeLink:
    """A monitor whose controls answer reads, failing the first few as real ones do."""

    def __init__(self, values=None, maximum=100, *, cold_reads=0):
        self.values = dict({ddc.RED_GAIN: 86} if values is None else values)
        self.maximum = maximum
        self.cold_reads = cold_reads
        self.reads = 0

    def read(self, code):
        self.reads += 1
        if self.cold_reads > 0:
            self.cold_reads -= 1
            return None
        if code not in self.values:
            return None
        return ddc.Control(code=code, current=self.values[code], maximum=self.maximum)


class ReadControlTests(unittest.TestCase):
    def test_a_cold_link_is_retried_rather_than_called_unsupported(self):
        """The first probe of the real monitor reported four controls as unsupported
        that a second pass found present and stable. A single failed read is a cold
        link, and treating it as an absent feature would report a calibratable display
        as uncalibratable."""
        link = FakeLink(cold_reads=2)
        control = ddc.read_control(link, ddc.RED_GAIN, pause=0.0)
        self.assertIsNotNone(control)
        self.assertEqual(86, control.current)

    def test_a_control_that_never_answers_is_given_up_on(self):
        link = FakeLink(values={})
        self.assertIsNone(ddc.read_control(link, ddc.RED_GAIN, attempts=3, pause=0.0))
        self.assertEqual(3, link.reads)

    def test_an_unavailable_link_reads_nothing(self):
        self.assertIsNone(ddc.read_control(ddc.UnavailableLink("no DDC"), ddc.RED_GAIN, pause=0.0))


class HandleReleaseTests(unittest.TestCase):
    """Physical-monitor handles are Windows allocations, and nothing used to release them:
    one leaked per attached monitor on every open_link, for the life of the process.

    dxva2 is replaced by a recorder, so these run without a monitor."""

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock

        self.destroyed = []
        recorder = SimpleNamespace(
            DestroyPhysicalMonitor=lambda handle: self.destroyed.append(handle.value) or True
        )
        for patcher in (mock.patch.object(ddc, "_dxva2", recorder, create=True),
                        mock.patch.object(ddc, "IS_WINDOWS", True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def links(self, *descriptions):
        found = [ddc.MonitorLink(index + 1, text) for index, text in enumerate(descriptions)]
        # Zeroed before the recorder is unpatched, so no finaliser can reach real dxva2.
        self.addCleanup(lambda: [setattr(link, "_handle", 0) for link in found])
        return found

    def open(self, found, name):
        from unittest import mock

        with mock.patch.object(ddc, "monitors", lambda: iter(found)):
            return ddc.open_link(name)

    def test_close_releases_the_handle_once(self):
        link = ddc.MonitorLink(5, "PG32UCDM")
        link.close()
        link.close()
        self.assertEqual([5], self.destroyed)

    def test_dropping_the_last_reference_releases_it(self):
        """How every caller in the app uses a link: read, then let it go."""
        import gc

        link = ddc.MonitorLink(6, "PG32UCDM")
        del link
        gc.collect()
        self.assertEqual([6], self.destroyed)

    def test_open_link_releases_every_monitor_it_does_not_return(self):
        found = self.links("DELL U2720Q", "ROG PG32UCDM")
        link = self.open(found, "PG32UCDM")
        self.assertIs(found[1], link)
        self.assertEqual([1], self.destroyed)

    def test_no_match_releases_them_all(self):
        found = self.links("DELL U2720Q", "LG 27GP950")
        self.assertIsInstance(self.open(found, "PG32UCDM"), ddc.UnavailableLink)
        self.assertEqual([1, 2], sorted(self.destroyed))

    def test_a_single_monitor_is_handed_back_open(self):
        found = self.links("ROG PG32UCDM")
        self.assertIs(found[0], self.open(found, ""))
        self.assertEqual([], self.destroyed)


@unittest.skipUnless(sys.platform == "win32", "dxva2 structs are Windows-only")
class PhysicalMonitorLayoutTests(unittest.TestCase):
    def test_the_array_stride_matches_physical_monitor(self):
        """GetPhysicalMonitorsFromHMONITOR fills an array of these: a HANDLE and a
        128-character description. A wrong size shifts every monitor after the first."""
        import ctypes

        self.assertEqual(ctypes.sizeof(ctypes.c_void_p) + 128 * 2, ctypes.sizeof(ddc._PhysicalMonitor))


if __name__ == "__main__":
    unittest.main()
