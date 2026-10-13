"""Tests for detecting elevation and asking Windows to restart elevated.

Nothing here may actually elevate anything, so ``ShellExecuteW`` is injected. What is
worth pinning down is the parts that are easy to get wrong and impossible to notice:
the verb, the command rebuilt for a source run, and above all that a dismissed UAC
prompt is reported as a decision rather than as a failure -- because the caller closes
the window on success, and a wrong answer there throws the user's work away.
"""

from __future__ import annotations

import unittest
from unittest import mock

from sdr_hdr_profile_creator import elevation


class RelaunchCommandTests(unittest.TestCase):
    def test_a_source_run_is_restarted_through_the_module(self):
        """``sys.argv[0]`` is the path of __main__.py by the time the app is running,
        and handing that back to the interpreter does not start the package."""
        program, params = elevation.relaunch_command(
            argv=[r"C:\src\sdr_hdr_profile_creator\__main__.py"],
            executable=r"C:\venv\pythonw.exe",
            frozen=False,
        )
        self.assertEqual(r"C:\venv\pythonw.exe", program)
        self.assertEqual("-m sdr_hdr_profile_creator", params)

    def test_a_frozen_build_is_restarted_as_itself(self):
        program, params = elevation.relaunch_command(
            argv=[r"C:\app\VirtualHDR.exe", "--display", "1"],
            executable=r"C:\app\VirtualHDR.exe",
            frozen=True,
        )
        self.assertEqual(r"C:\app\VirtualHDR.exe", program)
        self.assertEqual("--display 1", params)

    def test_arguments_containing_spaces_survive_the_round_trip(self):
        """list2cmdline, not " ".join -- a path with a space would otherwise arrive as
        two arguments in the elevated copy."""
        _, params = elevation.relaunch_command(
            argv=["app.exe", r"C:\Program Files\a profile.icm"],
            executable="app.exe",
            frozen=True,
        )
        self.assertEqual(r'"C:\Program Files\a profile.icm"', params)

    def test_the_module_form_keeps_any_arguments_that_were_passed(self):
        _, params = elevation.relaunch_command(
            argv=["__main__.py", "--verbose"], executable="py.exe", frozen=False
        )
        self.assertEqual("-m sdr_hdr_profile_creator --verbose", params)


class RelaunchTests(unittest.TestCase):
    """Every outcome ShellExecuteW can produce, and what the app should conclude."""

    def setUp(self):
        self.calls: list[tuple] = []
        patcher = mock.patch.object(elevation, "IS_WINDOWS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def shell(self, code):
        def run(hwnd, verb, program, parameters, directory, show):
            self.calls.append((hwnd, verb, program, parameters, directory, show))
            return code

        return run

    def relaunch(self, code, elevated=False):
        return elevation.relaunch_elevated(
            argv=["__main__.py"],
            shell_execute=self.shell(code),
            elevated=lambda: elevated,
        )

    def test_a_handle_above_the_threshold_means_the_copy_started(self):
        result = self.relaunch(42)
        self.assertIs(elevation.Relaunch.STARTED, result.outcome)
        self.assertTrue(result.started)

    def test_it_asks_for_the_runas_verb(self):
        """Anything else launches an ordinary copy, which changes nothing and looks
        like success."""
        self.relaunch(42)
        self.assertEqual("runas", self.calls[0][1])

    def test_a_dismissed_prompt_is_a_decision_not_a_failure(self):
        """The caller closes the window when ``started`` is true. Reporting a dismissed
        prompt as success would close it without an elevated copy to take over."""
        result = self.relaunch(elevation.SE_ERR_ACCESSDENIED)
        self.assertIs(elevation.Relaunch.DECLINED, result.outcome)
        self.assertFalse(result.started)
        self.assertIn("dismissed", result.message)

    def test_the_boundary_value_counts_as_failure(self):
        """ShellExecuteW's contract is 'greater than 32', not 'at least 32'."""
        result = self.relaunch(elevation.SHELL_EXECUTE_MIN_SUCCESS)
        self.assertIs(elevation.Relaunch.FAILED, result.outcome)
        self.assertFalse(result.started)

    def test_one_past_the_boundary_counts_as_success(self):
        result = self.relaunch(elevation.SHELL_EXECUTE_MIN_SUCCESS + 1)
        self.assertIs(elevation.Relaunch.STARTED, result.outcome)

    def test_any_other_low_code_is_reported_with_its_number(self):
        result = self.relaunch(2)
        self.assertIs(elevation.Relaunch.FAILED, result.outcome)
        self.assertIn("2", result.message)

    def test_an_already_elevated_process_does_not_start_a_second_one(self):
        result = self.relaunch(42, elevated=True)
        self.assertIs(elevation.Relaunch.UNAVAILABLE, result.outcome)
        self.assertEqual([], self.calls, "ShellExecuteW should not have been reached")

    def test_an_os_error_is_caught_rather_than_reaching_the_ui(self):
        def explode(*_args):
            raise OSError("no shell")

        result = elevation.relaunch_elevated(
            argv=["__main__.py"], shell_execute=explode, elevated=lambda: False
        )
        self.assertIs(elevation.Relaunch.FAILED, result.outcome)
        self.assertIn("no shell", result.message)

    def test_nothing_is_attempted_off_windows(self):
        with mock.patch.object(elevation, "IS_WINDOWS", False):
            result = elevation.relaunch_elevated(
                argv=["x"], shell_execute=self.shell(42), elevated=lambda: False
            )
        self.assertIs(elevation.Relaunch.UNAVAILABLE, result.outcome)
        self.assertEqual([], self.calls)


class ElevationCheckTests(unittest.TestCase):
    def test_the_check_answers_false_rather_than_raising_off_windows(self):
        with mock.patch.object(elevation, "IS_WINDOWS", False):
            self.assertFalse(elevation.is_elevated())

    def test_the_real_check_returns_a_bool(self):
        """It talks to the process token, so the value depends on how the suite was
        started; the type and the absence of an exception do not."""
        self.assertIsInstance(elevation.is_elevated(), bool)

    def test_the_reasons_given_to_the_user_are_the_two_that_are_real(self):
        """If this list grows, it should be because another operation genuinely
        started needing administrator rights -- not to make the button look busier."""
        self.assertEqual(2, len(elevation.BUYS))
        joined = " ".join(elevation.BUYS).lower()
        self.assertIn("scheduled task", joined)
        self.assertIn("access denied", joined)


if __name__ == "__main__":
    unittest.main()
