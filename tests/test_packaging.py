"""Checks on the shipped files themselves.

These verify things the Python tests cannot: that the three copies of the
watchdog script have not drifted apart, that the resources the app looks up at
runtime are actually packaged, and that the batch/PowerShell entry points refer
to files that exist.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
RESOURCES = ROOT / "src/sdr_hdr_profile_creator/resources"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestFileShapeTests(unittest.TestCase):
    """The suite has to be honest about how much of itself it is running."""

    def test_no_test_file_strands_classes_after_its_main_guard(self):
        """`if __name__ == "__main__": unittest.main()` in the middle of a file collects
        only what is defined above it. Under `unittest discover` the module is merely
        imported, so everything runs and this never shows -- but running one file while
        working on it is the normal thing to do, and that reported a confident pass over
        part of the file.

        It was 405 tests across six files when this was found: 203 of them in
        test_gui.py, 111 in test_pattern_view.py.
        """
        import ast

        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            guard = next(
                (node.lineno for node in tree.body
                 if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)),
                None,
            )
            if guard is None:
                continue
            stranded = sum(
                1
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.lineno > guard
                for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test")
            )
            if stranded:
                offenders.append(f"{path.name}: {stranded} tests after line {guard}")

        self.assertEqual(offenders, [], "move the __main__ guard to the end of the file")


class WatchdogDistributionTests(unittest.TestCase):
    def test_installers_and_uninstallers_are_identical(self):
        for root, copies in (
            (ROOT / "2- OPTIONAL - Install-Watchdog.bat", (RESOURCES / "2- OPTIONAL - Install-Watchdog.bat", ROOT / "watchdogs standalone/Install-Watchdog.bat")),
            (ROOT / "Uninstall-Watchdog.bat", (RESOURCES / "Uninstall-Watchdog.bat", ROOT / "watchdogs standalone/Uninstall-Watchdog.bat")),
        ):
            for copy in copies:
                self.assertEqual(digest(root), digest(copy), str(copy))

    def test_startup_fallback_reuses_the_bounded_supervisor(self):
        install = (ROOT / "2- OPTIONAL - Install-Watchdog.bat").read_text()
        uninstall = (ROOT / "Uninstall-Watchdog.bat").read_text()
        self.assertIn("RegisterTaskDefinition", install)
        self.assertIn("InteractiveToken", install)
        self.assertIn("HKCU Run fallback (shared hidden launcher)", install)
        self.assertIn("WScript.Arguments.Named.Exists",install)
        self.assertIn("failures >= 3",install)
        self.assertIn("$startupWarnings",install)
        for text in (install, uninstall):
            self.assertIn("Launcher.vbs", text)
            self.assertIn("cscript.exe", text)
            self.assertIn("wscript.exe", text)


class StandaloneArchiveTests(unittest.TestCase):
    """The zip is distributed on its own, so it must not go stale or unlicensed."""

    ARCHIVE = ROOT / "watchdogs standalone" / "watchdog-standalone.zip"

    def members(self) -> dict:
        with zipfile.ZipFile(self.ARCHIVE) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_archive_matches_the_scripts_beside_it(self):
        """It shipped a build predating every watchdog fix in this fork."""
        members = self.members()
        for name in ("Install-Watchdog.bat", "Uninstall-Watchdog.bat"):
            with self.subTest(member=name):
                on_disk = (ROOT / "watchdogs standalone" / name).read_bytes()
                self.assertIn(name, members)
                self.assertEqual(
                    hashlib.sha256(members[name]).hexdigest(),
                    hashlib.sha256(on_disk).hexdigest(),
                    f"{name} in the archive differs from the one beside it",
                )

    def test_archive_carries_the_licence(self):
        """GPL-3.0: the archive is distributed separately from the repository."""
        members = self.members()
        self.assertIn("LICENSE", members)
        self.assertEqual(
            hashlib.sha256(members["LICENSE"]).hexdigest(),
            hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(),
        )

    def test_archive_contains_the_watchdog_fixes(self):
        body = self.members()["Install-Watchdog.bat"].decode("utf-8", "replace")
        for needle in ("Resolve-BaseExtendedProfile", "GammaUpdatedAt", "ConvertTo-GammaTimestamp"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


class UvBootstrapTests(unittest.TestCase):
    """The installer downloads uv; what it downloads must be what was pinned.

    It used to fetch Astral's install script and run it with Invoke-Expression, so
    whatever that URL served -- through a TLS-inspecting proxy, or a compromised CDN
    path -- ran with the user's rights and nothing checked it."""

    SCRIPT = ROOT / "Install.ps1"

    def text(self):
        return self.SCRIPT.read_text(encoding="utf-8", errors="replace")

    def test_nothing_downloaded_is_executed_as_code(self):
        # Code only: the comment explaining what was replaced has to be able to name it.
        code = "\n".join(
            line for line in self.text().splitlines() if not line.lstrip().startswith("#")
        ).lower()
        for needle in ("invoke-expression", "| iex", "iex (", "iex $"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, code)

    def test_every_architecture_has_a_full_pin(self):
        pins = dict(re.findall(r'"(x86_64|aarch64|i686)"\s*=\s*"([0-9a-f]+)"', self.text()))
        self.assertEqual({"x86_64", "aarch64", "i686"}, set(pins))
        for arch, digest in pins.items():
            with self.subTest(arch=arch):
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    @staticmethod
    def launches():
        """The environments Install & Run is started from, as Windows PowerShell sees them.

        Started from a PowerShell 7 terminal, it inherits PowerShell 7's module path, and
        commands from the script half of a module -- Get-FileHash among them -- are then
        reported as not recognised. That is also what GitHub's runners do, whose steps run
        in PowerShell 7, and it is how this was found."""
        inherited = os.environ.get("PSModulePath", "")
        own = [p for p in inherited.split(os.pathsep) if p and "\\powershell\\7" not in p.lower()]
        yield "from Explorer or cmd", dict(os.environ, PSModulePath=os.pathsep.join(own))
        pwsh = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PowerShell" / "7" / "Modules"
        if pwsh.is_dir():
            yield "from a PowerShell 7 terminal", dict(
                os.environ, PSModulePath=os.pathsep.join([str(pwsh), *own])
            )

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is unavailable")
    def test_the_download_is_refused_unless_it_matches_the_pin(self):
        """The real function, run in Windows PowerShell -- the shell Install & Run uses --
        with the download stubbed."""
        import subprocess

        sys.path.insert(0, str(ROOT / "tests"))
        from test_watchdog import extract_function

        text = self.text()
        functions = "\n\n".join(
            extract_function(text, name)
            for name in ("Get-UvArchitecture", "Get-Sha256Hex", "Install-PinnedUv")
        )
        for label, environment in self.launches():
            with self.subTest(started=label), tempfile.TemporaryDirectory() as directory:
                functions_path = Path(directory) / "funcs.ps1"
                functions_path.write_text(functions, encoding="utf-8")
                work = Path(directory) / "work"
                work.mkdir()
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(ROOT / "tests" / "install_uv_pin.ps1"),
                     "-FunctionsPath", str(functions_path), "-Work", str(work)],
                    capture_output=True, text=True, timeout=180, env=environment,
                )
                self.assertEqual(
                    0, completed.returncode,
                    f"uv bootstrap harness failed:\n{completed.stdout}\n{completed.stderr}",
                )
                self.assertIn("ALL PASS", completed.stdout)

    def test_the_installer_does_not_depend_on_get_filehash(self):
        """Neither does the portable build, which wrote its .sha256 file without a hash."""
        for script in (self.SCRIPT, ROOT / "3- (Advanced users & developers) - Build Portable EXE.bat"):
            code = "\n".join(
                line for line in script.read_text(encoding="utf-8", errors="replace").splitlines()
                if not line.lstrip().lower().startswith(("#", "rem "))
            )
            with self.subTest(script=script.name):
                self.assertNotIn("get-filehash", code.lower())


class EntryPointTests(unittest.TestCase):
    def test_scripts_reference_files_that_exist(self):
        """Catches instructions pointing at a launcher that was renamed away."""
        pattern = re.compile(r"[\"']?([\w \-&()]+\.bat)[\"']?")
        for script in (ROOT / "Install.ps1", ROOT / "1- Install & Run.bat"):
            text = script.read_text(encoding="utf-8", errors="replace")
            for referenced in set(pattern.findall(text)):
                with self.subTest(script=script.name, referenced=referenced):
                    self.assertTrue(
                        (ROOT / referenced).is_file(),
                        f"{script.name} refers to {referenced!r}, which does not exist",
                    )

    def test_launcher_starts_the_package_entry_point(self):
        launcher = (ROOT / "1- Install & Run.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("-m sdr_hdr_profile_creator", launcher)

    def test_portable_build_is_a_single_windowless_exe(self):
        builder = (ROOT / "3- (Advanced users & developers) - Build Portable EXE.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("--onefile", builder)
        self.assertIn("--windows-console-mode=disable", builder)
        self.assertNotIn("--standalone", builder)

    def test_the_portable_build_installs_only_pinned_packages(self):
        """Two builds of one commit used to resolve whatever was newest that day."""
        builder = (ROOT / "3- (Advanced users & developers) - Build Portable EXE.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        installs = [line for line in builder.splitlines() if " pip install " in line]
        self.assertTrue(installs, "no pip install lines found")
        for line in installs:
            with self.subTest(line=line):
                named = re.findall(r'"([A-Za-z][\w.-]*(?:[=<>!~].*?)?)"', line.split("--no-cache", 1)[1])
                for spec in named:
                    self.assertIn("==", spec, f"{spec} is not pinned")
                if "-r " in line:
                    self.assertIn("--require-hashes", line)
                if line.rstrip().endswith("-e ."):
                    self.assertIn("--no-deps", line, "the project would pull its dependencies unlocked")
        self.assertIn("export --locked", builder)


class VersionTests(unittest.TestCase):
    """One version, written once, and read by everything that reports one.

    It was "0" for the life of the project, so no bug report, meter log or screenshot
    could be matched to the code that produced it."""

    def version(self) -> str:
        from sdr_hdr_profile_creator import __version__

        return __version__

    def test_it_is_a_real_version(self):
        self.assertRegex(self.version(), r"^\d+(?:\.\d+){0,2}$")

    def test_pyproject_declares_the_same_version(self):
        import tomllib

        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(self.version(), project["version"])

    def test_the_lockfile_records_the_same_version(self):
        """Install.ps1 runs uv sync, which re-resolves a lock that disagrees with
        pyproject and rewrites it on the user's machine instead of using the one
        the release shipped."""
        import tomllib

        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        entry = next(p for p in lock["package"] if p["name"] == "virtual-hdr-osd-for-windows")
        self.assertEqual(self.version(), entry["version"])

    def test_a_startup_failure_log_names_the_version_first(self):
        """That file is what gets pasted into a bug report."""
        from sdr_hdr_profile_creator import __main__ as entry

        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}), \
             mock.patch.object(entry.sys, "platform", "linux"), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                entry._report_startup_failure()
            log = (Path(temp) / "Virtual_HDR_OSD_for_Windows" / "startup_error.log").read_text(
                encoding="utf-8"
            )
        self.assertEqual(f"Virtual HDR OSD for Windows {self.version()}", log.splitlines()[0])
        self.assertIn("RuntimeError: boom", log)


class ReadmeAccuracyTests(unittest.TestCase):
    """Documentation drifts silently. These check only claims that would become false if
    the code moved -- not prose, which is the author's business."""

    README = ROOT / "README.md"

    def text(self) -> str:
        return self.README.read_text(encoding="utf-8", errors="replace")

    def test_every_offered_correction_option_is_documented(self):
        from vhdr_color.gamma_correction import CORRECTION_OPTIONS

        for option in CORRECTION_OPTIONS:
            with self.subTest(option=option):
                self.assertIn(option, self.text())

    def test_retired_options_are_not_presented_as_choices(self):
        """They still resolve for old profiles, but offering them would be a lie."""
        for entry in ("- **Unspecified**", "- **SDR** —"):
            with self.subTest(entry=entry):
                self.assertNotIn(entry, self.text())

    def test_every_guided_step_is_named(self):
        from vhdr_color.patterns import GUIDED_SEQUENCE, pattern_by_key

        for key in GUIDED_SEQUENCE:
            with self.subTest(step=key):
                self.assertIn(pattern_by_key(key).title, self.text())

    def test_the_guided_run_table_lists_exactly_the_guided_steps(self):
        """The table went on listing Full-frame white as a step after the code dropped
        it, and every real step was still named, so the check above kept passing. This
        reads the table itself, and the counts written around it."""
        from vhdr_color.patterns import (
            GUIDED_SEQUENCE, MEASUREMENT_SEQUENCE, pattern_by_key,
        )

        section = self.text().split("## The guided run", 1)[1].split("\n## ", 1)[0]
        cells = [line.split("|")[1].strip() for line in section.splitlines() if line.startswith("|")]
        steps = [cell for cell in cells if cell not in ("step", "---")]
        self.assertEqual([pattern_by_key(key).title for key in GUIDED_SEQUENCE], steps)
        self.assertIn(f"{len(GUIDED_SEQUENCE)}-step sequence", section)
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
        self.assertIn(f"The first {words[len(MEASUREMENT_SEQUENCE)]} let you find", section)

    def test_the_documented_pattern_keys_match_the_pattern_count(self):
        from vhdr_color.patterns import PATTERNS

        mentions_zero = "`1`–`9`, `0`" in self.text()
        self.assertEqual(mentions_zero, len(PATTERNS) > 9,
                         "the key hint and the pattern list disagree")

    def test_it_does_not_still_describe_itself_as_only_an_editor(self):
        """It measures luminance and writes it into profiles now."""
        self.assertNotIn("lightweight Windows 11 HDR profile editor", self.text())

    def test_it_no_longer_denies_doing_what_it_does(self):
        """The old text ruled out a colorimeter workflow; a meter driver is planned and
        the by-eye measurements already exist."""
        self.assertNotIn("colorimeter/spectrophotometer workflow", self.text())

    def test_every_source_of_a_figure_is_still_caveated(self):
        """Broadening the claims must not quietly drop the caveats that matter.

        There are three sources now, not one, and each is weak in its own way:
        EDID describes the model rather than the unit, the patterns are judged by
        eye, and a colorimeter without a spectral correction matched to the panel
        can be hundreds of kelvin out on a quantum-dot display."""
        text = self.text()
        self.assertIn("Adjusting by eye", text)
        self.assertIn("does not provide an instrument reading", text)
        self.assertIn("spectral correction", text)

    def test_native_primaries_are_not_claimed_as_measured(self):
        """Effective BT.2020 coverage must not be called raw panel primaries."""
        text = self.text()
        self.assertNotIn("The real primaries, replacing the ones DXGI reports", text)
        self.assertNotIn("primaries through DXGI", text)

    def test_balance_clamp_is_documented_in_the_technical_reference(self):
        """The separate wide-gamut probes must not hide the BT.709 balance clamp."""
        import struct

        from vhdr_color.patterns import PatternContext, measurement_frame

        width, height = 40, 20
        frame = measurement_frame(
            width, height, (1.0, -0.5, 0.0), 80.0, PatternContext(is_hdr=True)
        )
        green = struct.unpack_from("<4e", frame, ((height // 2) * width + width // 2) * 8)[1]
        text = self.text() + (ROOT / "docs" / "TECHNICAL.md").read_text(encoding="utf-8")
        self.assertNotIn("cannot describe", text)
        self.assertNotIn("report the encoding", text)
        self.assertEqual(
            green == 0.0, "BT.709 balance patches discard negative channel values" in text,
            "the README and measurement_frame disagree about the clamp",
        )


if __name__ == "__main__":
    unittest.main()
