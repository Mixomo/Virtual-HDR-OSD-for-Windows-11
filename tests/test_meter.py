"""Reading a colorimeter through ArgyllCMS's spotread.

Everything here runs without an instrument attached. The subprocess call is the
one part that cannot be, so it is kept to a thin wrapper while the two things
that would do real harm -- misreading a number, and failing to recognise a
failure -- are tested against output recorded from the real program.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sdr_hdr_profile_creator.meter import (
    MeterError,
    Reading,
    build_command,
    classify_line,
    find_spotread,
    list_instruments,
    parse_reading,
    read_emissive,
)

# spotread prints with plain %f, which is six decimal places.
PEAK = " Result is XYZ: 964.512300 1015.240800 1105.331200, Yxy: 1015.240800 0.312700 0.329000\n"
BLACK = " Result is XYZ: 0.000148 0.000156 0.000170, Yxy: 0.000156 0.312700 0.329000\n"

# Recorded verbatim from Argyll 3.5.0 driving an i1d3 whose ambient diffuser was
# left closed. spotread repeats this forever rather than exiting: 46MB of it in
# 200 seconds, which is why the reader stops at the first line that settles the
# outcome instead of waiting for the process to finish.
WRONG_POSITION = (
    "\nSpot read failed due to the sensor being in the wrong position\n"
    "(Ambient filter should be removed)\n"
)

# Representative instrument response; serial identifier is synthetic.
REAL_PREAMBLE = (
    "Connecting to the instrument ..\n"
    "Product Name:      i1Display3 \n"
    "Serial Number:     SYNTHETIC-0001\n"
    "Firmware Version:  v2.28 \n"
    "Setting no-initial calibrate failed with 'Unsupported function' (No device error)\n"
    "Disable initial-calibrate not supported\n"
    "Init instrument success !\n"
)

REAL_PORT_LIST = (
    "usage: spotread [-options] [logfile]\n"
    " -v                   Verbose mode\n"
    " -c listno            Set instrument port from the following list (default 1)\n"
    "    1 = 'hid:/31 (X-Rite i1 DisplayPro, ColorMunki Display)'\n"
    "    2 = 'COM3'\n"
    "    3 = 'usb:/bus0/dev2 (i1 Pro)' !! Disabled - no firmware !!\n"
    " -t                   Use transmission measurement mode\n"
)


class ParseReadingTests(unittest.TestCase):
    def test_reads_absolute_luminance_from_an_emissive_measurement(self):
        reading = parse_reading(PEAK)
        self.assertAlmostEqual(reading.Y, 1015.2408, places=3)
        self.assertAlmostEqual(reading.nits, 1015.2408, places=3)

    def test_reads_chromaticity(self):
        reading = parse_reading(PEAK)
        self.assertAlmostEqual(reading.x, 0.3127, places=4)
        self.assertAlmostEqual(reading.y, 0.3290, places=4)

    def test_reads_the_full_xyz_triplet(self):
        reading = parse_reading(PEAK)
        self.assertAlmostEqual(reading.X, 964.5123, places=3)
        self.assertAlmostEqual(reading.Z, 1105.3312, places=3)

    def test_handles_a_near_black_reading_without_losing_precision(self):
        """Black level lands in the fourth decimal place; rounding it away would
        put a plausible but wrong minimum luminance into the profile."""
        reading = parse_reading(BLACK)
        self.assertAlmostEqual(reading.Y, 0.000156, places=6)
        self.assertGreater(reading.Y, 0.0)

    def test_finds_the_reading_after_the_instruments_own_preamble(self):
        self.assertAlmostEqual(parse_reading(REAL_PREAMBLE + PEAK).Y, 1015.2408, places=3)

    def test_negative_luminance_is_clamped_to_zero(self):
        """Some instruments report a slightly negative Y for a true black. A
        negative nit value would propagate into the profile as a minimum."""
        reading = parse_reading(
            " Result is XYZ: -0.000010 -0.000008 -0.000012, Yxy: -0.000008 0.31 0.33\n"
        )
        self.assertEqual(reading.Y, 0.0)

    def test_missing_reading_is_an_error_not_a_zero(self):
        """Returning 0 nits here would be indistinguishable from a real black."""
        with self.assertRaises(MeterError):
            parse_reading("Place instrument on spot to be measured\n")

    def test_a_wrong_position_block_explains_the_diffuser(self):
        with self.assertRaises(MeterError) as caught:
            parse_reading(REAL_PREAMBLE + WRONG_POSITION)
        self.assertIn("ambient filter", str(caught.exception).lower())

    def test_a_failed_run_reports_the_instruments_own_message(self):
        with self.assertRaises(MeterError) as caught:
            parse_reading("", "Something specific went wrong", returncode=1)
        self.assertIn("Something specific went wrong", str(caught.exception))

    def test_empty_output_is_an_error(self):
        with self.assertRaises(MeterError):
            parse_reading("")


class ClassifyLineTests(unittest.TestCase):
    """One line at a time, so a runaway can be stopped at the first sign."""

    def test_a_result_line_yields_a_reading(self):
        self.assertIsInstance(classify_line(PEAK), Reading)

    def test_the_wrong_position_line_is_recognised(self):
        """This is the first failure any i1d3 user hits, and spotread never
        gives up on it, so recognising it is what turns a hang into advice."""
        outcome = classify_line("(Ambient filter should be removed)")
        self.assertIsInstance(outcome, str)
        self.assertIn("ambient filter", outcome.lower())

    def test_a_missing_instrument_is_recognised(self):
        outcome = classify_line("No instruments found")
        self.assertIsInstance(outcome, str)
        self.assertIn("plugged in", outcome)

    def test_an_instrument_held_by_other_software_is_recognised(self):
        outcome = classify_line("Failed to open instrument")
        self.assertIsInstance(outcome, str)
        self.assertIn("other calibration software", outcome)

    def test_ordinary_progress_lines_settle_nothing(self):
        for line in REAL_PREAMBLE.splitlines():
            self.assertIsNone(classify_line(line), line)

    def test_the_unsupported_dash_n_warning_is_not_treated_as_a_failure(self):
        """The i1d3 rejects -N and says so on stderr. It is a warning; the run
        continues, and treating it as fatal would break every reading."""
        self.assertIsNone(
            classify_line("Setting no-initial calibrate failed with 'Unsupported function'")
        )


class BuildCommandTests(unittest.TestCase):
    """The flags are the measurement."""

    def test_asks_for_emissive_absolute_and_yxy_and_one_shot(self):
        """-e is what makes Y come back in cd/m2; the white-relative modes would
        return a percentage that looks just as much like a number."""
        command = build_command(Path("spotread"))
        self.assertIn("-e", command)
        self.assertIn("-x", command)
        self.assertIn("-O", command)

    def test_does_not_skip_calibration_by_default(self):
        """The i1d3 rejects -N outright, and the default behaviour works."""
        self.assertNotIn("-N", build_command(Path("spotread")))

    def test_calibration_skip_is_still_available(self):
        self.assertIn("-N", build_command(Path("spotread"), skip_calibration=True))

    def test_port_and_display_type_are_passed_through(self):
        command = build_command(Path("spotread"), port=2, display_type="r")
        self.assertEqual(command[command.index("-c") + 1], "2")
        self.assertEqual(command[command.index("-y") + 1], "r")


class FakePopen:
    """Stands in for spotread, emitting recorded lines."""

    def __init__(self, lines, *, block_forever=False):
        self._lines = list(lines)
        self._block = block_forever
        self.terminated = False
        self.killed = False
        self._done = threading.Event()
        self.stdout = self._iter()

    def _iter(self):
        for line in self._lines:
            yield line
        if self._block:
            # A process that never exits, like spotread mid-retry.
            self._done.wait(30.0)

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True
        self._done.set()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self._done.set()


class ReadEmissiveTests(unittest.TestCase):
    def _read(self, lines, *, block_forever=False, timeout=5.0):
        fake = FakePopen(lines, block_forever=block_forever)
        with mock.patch("subprocess.Popen", return_value=fake):
            try:
                return read_emissive(Path("spotread"), timeout=timeout), fake
            except MeterError as exc:
                return exc, fake

    def test_returns_the_parsed_reading(self):
        result, _ = self._read(REAL_PREAMBLE.splitlines(True) + [PEAK])
        self.assertIsInstance(result, Reading)
        self.assertAlmostEqual(result.nits, 1015.2408, places=3)

    def test_stops_at_the_first_failure_rather_than_waiting_out_the_retries(self):
        """The recorded runaway repeated this until killed. Reporting it at once
        is the difference between advice and a hang."""
        lines = REAL_PREAMBLE.splitlines(True) + WRONG_POSITION.splitlines(True) * 500
        result, _ = self._read(lines, block_forever=True, timeout=20.0)
        self.assertIsInstance(result, MeterError)
        self.assertIn("ambient filter", str(result).lower())

    def test_the_process_is_always_stopped(self):
        """spotread holds the instrument open while it retries, so leaving one
        running would make the next reading fail too."""
        _, fake = self._read(REAL_PREAMBLE.splitlines(True) + [PEAK])
        self.assertTrue(fake.terminated or fake.killed)

    def test_a_process_that_says_nothing_times_out_with_advice(self):
        result, fake = self._read([], block_forever=True, timeout=1.0)
        self.assertIsInstance(result, MeterError)
        self.assertIn("holding it open", str(result))
        self.assertTrue(fake.terminated or fake.killed)

    def test_stop_ends_a_read_in_flight_and_frees_the_instrument(self):
        """Esc used to wait out the read: up to the full timeout, holding the meter."""
        fake = FakePopen(REAL_PREAMBLE.splitlines(True), block_forever=True)
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        with mock.patch("subprocess.Popen", return_value=fake):
            with self.assertRaises(MeterError) as raised:
                read_emissive(Path("spotread"), timeout=30.0, stop=stop)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn("Stopped", str(raised.exception))
        self.assertTrue(fake.terminated or fake.killed)

    def test_a_reading_that_arrives_is_kept_when_stop_is_never_set(self):
        fake = FakePopen(REAL_PREAMBLE.splitlines(True) + [PEAK])
        with mock.patch("subprocess.Popen", return_value=fake):
            result = read_emissive(Path("spotread"), stop=threading.Event())
        self.assertAlmostEqual(result.nits, 1015.2408, places=3)

    def test_a_missing_executable_is_reported_rather_than_raised_raw(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("not found")):
            with self.assertRaises(MeterError):
                read_emissive(Path("spotread"))


class FindSpotreadTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="meter-test-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.name = "spotread.exe" if os.name == "nt" else "spotread"
        self.binary = self.temp / self.name
        self.binary.write_bytes(b"")

    def test_finds_a_configured_directory(self):
        self.assertEqual(find_spotread(self.temp), self.binary)

    def test_finds_a_configured_file_directly(self):
        self.assertEqual(find_spotread(self.binary), self.binary)

    def test_finds_it_on_the_path(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.temp)}):
            self.assertEqual(find_spotread(), self.binary)

    def test_returns_none_when_argyll_is_not_installed(self):
        """Absence must be reportable, so the user can be pointed at the
        download rather than shown a stack trace."""
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with mock.patch("sdr_hdr_profile_creator.meter._SEARCH_DIRS", ()):
                self.assertIsNone(find_spotread())

    def test_a_configured_path_that_does_not_exist_falls_back_to_searching(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.temp)}):
            self.assertEqual(find_spotread(self.temp / "nope"), self.binary)


class ListInstrumentsTests(unittest.TestCase):
    """Argyll lists candidate ports, which is not the same as instruments."""

    def _list(self, stdout="", stderr=""):
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout, stderr),
        ):
            return list_instruments(Path("spotread"))

    def test_reads_the_port_list_argyll_prints_on_stderr(self):
        found = self._list(stderr=REAL_PORT_LIST)
        self.assertEqual(
            [instrument.label for instrument in found],
            ["hid:/31 (X-Rite i1 DisplayPro, ColorMunki Display)"],
        )

    def test_keeps_the_port_number_that_dash_c_expects(self):
        """Dropping it left no way to name which instrument to measure with."""
        self.assertEqual(self._list(stderr=REAL_PORT_LIST)[0].port, 1)

    def test_ignores_a_bare_serial_port_with_nothing_on_it(self):
        """A machine with a COM port lists it beside the real instrument."""
        labels = [instrument.label for instrument in self._list(stderr=REAL_PORT_LIST)]
        self.assertNotIn("COM3", labels)

    def test_ignores_instruments_argyll_marks_as_disabled(self):
        labels = " ".join(i.label for i in self._list(stderr=REAL_PORT_LIST))
        self.assertNotIn("i1 Pro", labels)

    def test_no_instruments_is_an_empty_list_not_an_error(self):
        usage = " -c listno            Set instrument port\n    ** No ports found **\n"
        self.assertEqual(self._list(stderr=usage), [])

    def test_a_broken_install_is_reported(self):
        with mock.patch("subprocess.run", side_effect=OSError("bad exe")):
            with self.assertRaises(MeterError):
                list_instruments(Path("spotread"))


if __name__ == "__main__":
    unittest.main()
