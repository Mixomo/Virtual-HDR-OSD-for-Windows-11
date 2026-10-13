"""The app's JSON files and the watchdog build id, without Qt.

These were methods on the main window, reachable only through the half-hour GUI suite that
CI does not run. Split out with their paths passed in, they run anywhere.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sdr_hdr_profile_creator import persistence
from sdr_hdr_profile_creator.watchdog_identity import (
    WATCHDOG_PAYLOAD_MARKER,
    watchdog_build_id,
    watchdog_payload,
)
from vhdr_color.model import ApplicationState


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vhdrosd-persist-"))
        self.addCleanup(shutil.rmtree, self.dir, True)


class AtomicWriteTests(TempDirTestCase):
    def test_it_writes_and_leaves_no_temporary_behind(self):
        target = self.dir / "x.json"
        self.assertTrue(persistence.write_json_atomic(target, {"a": 1}))
        self.assertEqual({"a": 1}, json.loads(target.read_text(encoding="utf-8")))
        self.assertEqual(["x.json"], sorted(p.name for p in self.dir.iterdir()))

    def test_a_rename_that_stays_locked_gives_up_and_cleans_up(self):
        target = self.dir / "x.json"
        with mock.patch.object(Path, "replace", side_effect=PermissionError(32, "in use")) as replace, \
             mock.patch.object(persistence.time, "sleep"):
            self.assertFalse(persistence.write_json_atomic(target, {"a": 1}))
        self.assertEqual(5, replace.call_count)
        self.assertEqual([], list(self.dir.iterdir()))


class StateLoadTests(TempDirTestCase):
    def test_no_file_is_a_clean_start(self):
        state, problem, unopened = persistence.load_state(self.dir / "last_gui_state.json")
        self.assertEqual(ApplicationState.neutral().to_dict(), state.to_dict())
        self.assertEqual(("", False), (problem, unopened))

    def test_a_corrupt_file_is_set_aside_and_reported(self):
        path = self.dir / "last_gui_state.json"
        path.write_text("{ not json", encoding="utf-8")
        _state, problem, unopened = persistence.load_state(path)
        self.assertFalse(path.exists())
        self.assertTrue((self.dir / "last_gui_state.unreadable.json").exists())
        self.assertIn("last_gui_state.unreadable.json", problem)
        self.assertFalse(unopened)

    def test_a_file_that_cannot_be_opened_is_left_and_flagged(self):
        path = self.dir / "last_gui_state.json"
        path.write_text(json.dumps({"hdr": {"gamma": 2.4}}), encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(32, "in use")):
            _state, problem, unopened = persistence.load_state(path)
        self.assertTrue(unopened)
        self.assertIn("will not save over it", problem)
        self.assertTrue(path.exists())

    def test_a_lock_that_lets_go_at_startup_is_waited_out(self):
        """At sign-in the lock is usually a scan that releases within a moment."""
        path = self.dir / "last_gui_state.json"
        path.write_text(json.dumps({"hdr": {"gamma": 2.4}}), encoding="utf-8")
        reads = iter([PermissionError(32, "in use"), json.dumps({"hdr": {"gamma": 2.4}})])

        def read_text(_self, encoding=None):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(Path, "read_text", read_text), \
             mock.patch.object(persistence.time, "sleep"):
            state, problem, unopened = persistence.load_state(path)
        self.assertAlmostEqual(2.4, state.hdr.gamma)
        self.assertEqual(("", False), (problem, unopened))

    def test_a_readable_file_loads(self):
        path = self.dir / "last_gui_state.json"
        path.write_text(json.dumps({"hdr": {"gamma": 2.4}}), encoding="utf-8")
        state, problem, unopened = persistence.load_state(path)
        self.assertAlmostEqual(2.4, state.hdr.gamma)
        self.assertEqual(("", False), (problem, unopened))


class OriginalProfilesLoadTests(TempDirTestCase):
    def test_a_file_that_cannot_be_opened_is_none_not_empty(self):
        """Empty would be cached and saved over the only record of what Windows had."""
        path = self.dir / "original_profiles.json"
        path.write_text(json.dumps({"displays": {"k": {"hdr": "A.icm"}}}), encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(32, "in use")):
            self.assertIsNone(persistence.load_original_profiles(path))

    def test_a_malformed_record_is_set_aside(self):
        path = self.dir / "original_profiles.json"
        path.write_text("[]", encoding="utf-8")
        self.assertEqual({}, persistence.load_original_profiles(path))
        self.assertTrue((self.dir / "original_profiles.unreadable.json").exists())

    def test_records_load(self):
        path = self.dir / "original_profiles.json"
        path.write_text(json.dumps({"displays": {"k": {"hdr": "A.icm"}, "": {}, "bad": 3}}),
                        encoding="utf-8")
        self.assertEqual({"k": {"hdr": "A.icm"}}, persistence.load_original_profiles(path))


class RuntimePayloadLoadTests(TempDirTestCase):
    """gamma_hotkeys.json is written back whole, so a failed read must not become {}."""

    def setUp(self):
        super().setUp()
        self.path = self.dir / "gamma_hotkeys.json"
        sleep = mock.patch.object(persistence.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_no_file_is_a_clean_start(self):
        self.assertEqual({}, persistence.load_runtime_payload(self.path))

    def test_a_readable_file_loads(self):
        self.path.write_text(json.dumps({"displays": {"a": {}, "b": {}}}), encoding="utf-8")
        self.assertEqual({"displays": {"a": {}, "b": {}}}, persistence.load_runtime_payload(self.path))

    def test_a_file_that_stays_locked_is_none_and_left_alone(self):
        self.path.write_text(json.dumps({"displays": {"a": {}}}), encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(32, "in use")) as read:
            self.assertIsNone(persistence.load_runtime_payload(self.path))
        self.assertEqual(4, read.call_count)
        self.assertEqual(["gamma_hotkeys.json"], [p.name for p in self.dir.iterdir()])

    def test_a_read_that_lands_mid_rename_is_retried(self):
        good = json.dumps({"displays": {"a": {}, "b": {}}})
        reads = iter([PermissionError(32, "in use"), '{"displays": {"a"', good])

        def read_text(_self, encoding=None):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

        self.path.write_text(good, encoding="utf-8")
        with mock.patch.object(Path, "read_text", read_text):
            self.assertEqual({"displays": {"a": {}, "b": {}}}, persistence.load_runtime_payload(self.path))

    def test_a_file_that_is_not_json_is_set_aside(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual({}, persistence.load_runtime_payload(self.path))
        self.assertFalse(self.path.exists())
        self.assertTrue((self.dir / "gamma_hotkeys.unreadable.json").exists())

    def test_a_json_value_that_is_not_an_object_is_set_aside(self):
        self.path.write_text("[]", encoding="utf-8")
        self.assertEqual({}, persistence.load_runtime_payload(self.path))
        self.assertTrue((self.dir / "gamma_hotkeys.unreadable.json").exists())


class WatchdogIdentityTests(unittest.TestCase):
    def test_the_payload_is_found_from_the_end(self):
        text = f"echo {WATCHDOG_PAYLOAD_MARKER} here\r\n{WATCHDOG_PAYLOAD_MARKER}\r\nparam()\r\n"
        self.assertEqual("\r\nparam()\r\n", watchdog_payload(text))

    def test_no_marker_is_no_payload(self):
        self.assertEqual("", watchdog_payload("@echo off"))

    def test_the_id_ignores_a_bom_and_line_endings(self):
        self.assertEqual(watchdog_build_id("param()\nWrite-Log 1\n"),
                         watchdog_build_id("﻿param()\r\nWrite-Log 1\r\n"))
        self.assertNotEqual(watchdog_build_id("param()\nWrite-Log 1\n"), watchdog_build_id("param()\nWrite-Log 2\n"))
        self.assertEqual("", watchdog_build_id("  \r\n"))


if __name__ == "__main__":
    unittest.main()
