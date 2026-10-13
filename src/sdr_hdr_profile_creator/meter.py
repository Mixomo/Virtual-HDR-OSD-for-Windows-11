"""Reading a colorimeter through ArgyllCMS's ``spotread``.

ArgyllCMS is invoked as a separate program rather than ported. That is how
DisplayCAL does it -- its tree contains no instrument code at all, only the
executable names and a subprocess call -- and it is the right arrangement here
too. Running a program is not derivative use, so Argyll's licence never reaches
this project, and Graeme Gill's maintained code does the colour matching instead
of a reimplementation of it that would be wrong in ways no test here could catch.

A wrong frame from an instrument fails loudly. A misaligned colour matching
function does not: it returns a plausible XYZ that is quietly 5-30% out, and it
would end up in a profile with no record of where it came from.

Output is consumed as it arrives rather than collected at the end. ``spotread``
with ``-O`` retries a failed read forever instead of exiting -- an i1d3 with its
ambient diffuser closed produced 46MB of the same complaint in 200 seconds -- so
waiting for the process to finish means buffering all of it and then reporting a
timeout, when the very first failure already said exactly what was wrong.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# The types belong to the colour core, which has to stay importable without Argyll.
from vhdr_color.measure import MeterError, Reading

# Argyll ships these as plain executables; the name never varies by version.
SPOTREAD_NAMES = ("spotread.exe", "spotread")

# Where Argyll's own layouts put the binaries. It is distributed on Windows as a
# zip with no installer, so a user-chosen directory is at least as likely.
_SEARCH_DIRS = (
    r"C:\Argyll\bin",
    r"C:\Program Files\Argyll\bin",
    r"C:\Program Files (x86)\Argyll\bin",
)

# " Result is XYZ: 0.100 1.234 0.567, Yxy: 1.234 0.3127 0.3290"
_RESULT = re.compile(
    r"Result is XYZ:\s*"
    r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*,\s*"
    r"Yxy:\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)"
)

# Conditions worth naming, because each has a different thing for the user to do.
# spotread repeats these indefinitely rather than giving up, so the first match
# ends the run.
_FAILURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"wrong position|filter should be removed", re.I),
        "The meter's sensor is in the wrong position. Slide the ambient filter "
        "off the lens, then try again.",
    ),
    (
        re.compile(r"No instruments? found|Failed to find|No ports found", re.I),
        "Argyll could not find the meter. Check that it is plugged in, and that "
        "no other calibration software is holding it open.",
    ),
    (
        re.compile(r"Failed to open|Unable to open|instrument access failed", re.I),
        "Argyll found the meter but could not open it. Close any other "
        "calibration software that may be using it, then try again.",
    ),
    (
        re.compile(r"needs a calibration|needs calibrating", re.I),
        "The meter wants to calibrate itself first. Close the ambient filter over "
        "the lens, run a calibration, then reopen it to measure.",
    ),
)

# A runaway cannot be allowed to grow without bound. Generous enough that a
# normal verbose run is never truncated.
_MAX_LINES = 400


@dataclass(frozen=True, slots=True)
class Instrument:
    """One entry from Argyll's port list."""

    port: int
    label: str


def find_spotread(configured: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate ``spotread``, or None when Argyll is not installed.

    A configured path wins outright: Argyll ships on Windows as a zip with no
    installer, so it commonly lives somewhere only the user knows about.
    """
    if configured:
        candidate = Path(configured)
        if candidate.is_dir():
            for name in SPOTREAD_NAMES:
                if (candidate / name).is_file():
                    return candidate / name
        elif candidate.is_file():
            return candidate

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in SPOTREAD_NAMES:
            try:
                candidate = Path(directory) / name
                if candidate.is_file():
                    return candidate
            except OSError:
                continue

    for directory in _SEARCH_DIRS:
        for name in SPOTREAD_NAMES:
            candidate = Path(directory) / name
            if candidate.is_file():
                return candidate
    return None


def _no_window() -> int:
    """Keep a console from flashing up for every patch measured."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def list_instruments(spotread: Path, timeout: float = 20.0) -> list[Instrument]:
    """Instruments Argyll can see, with the port number ``-c`` expects.

    ``spotread -?`` prints the port list as part of its usage text, so this needs
    no instrument interaction and cannot disturb one already connected. Argyll
    writes its usage to stderr.

    Argyll lists every candidate *port*, not every instrument: a machine with a
    serial port reports a bare ``COM3`` beside the real entry. Only ports
    carrying a device description are instruments, so bare ones are dropped
    rather than offered as something to measure with.
    """
    try:
        finished = subprocess.run(
            [str(spotread), "-?"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_no_window(),
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeterError(f"Could not run spotread: {exc}") from exc

    instruments: list[Instrument] = []
    combined = (finished.stderr or "").splitlines() + (finished.stdout or "").splitlines()
    for line in combined:
        #     1 = 'hid:/31 (X-Rite i1 DisplayPro, ColorMunki Display)'
        match = re.match(r"\s*(\d+)\s*=\s*'([^']+)'", line)
        if not match:
            continue
        if "!! Disabled" in line:
            # Argyll says so itself, e.g. an i1 Pro with no firmware loaded.
            continue
        label = match.group(2)
        if "(" not in label:
            # A port with nothing identifiable on it.
            continue
        instruments.append(Instrument(port=int(match.group(1)), label=label))
    return instruments


def build_command(
    spotread: Path,
    *,
    port: int | None = None,
    display_type: str | None = None,
    skip_calibration: bool = False,
) -> list[str]:
    """The exact spotread invocation for one emissive reading.

    ``-e`` asks for emissive absolute rather than any of the white-relative
    modes, so Y comes back in cd/m² instead of a percentage. ``-x`` adds Yxy
    beside the XYZ. ``-O`` measures once and exits, and installs a UI callback
    that triggers the reading immediately instead of waiting for a keypress.

    ``skip_calibration`` (``-N``) defaults off because the i1d3 rejects it --
    "Disable initial-calibrate not supported" -- and the default behaviour works.
    It stays available for instruments that honour it.
    """
    command = [str(spotread), "-e", "-x", "-O"]
    if skip_calibration:
        command.append("-N")
    if port is not None:
        command += ["-c", str(port)]
    if display_type:
        command += ["-y", display_type]
    return command


def read_emissive(
    spotread: Path,
    *,
    port: int | None = None,
    display_type: str | None = None,
    skip_calibration: bool = False,
    timeout: float = 60.0,
    stop: threading.Event | None = None,
) -> Reading:
    """Take one emissive reading in absolute units.

    Stops at the first line that settles the outcome, rather than waiting for a
    process that may never exit on its own. Setting ``stop`` ends the wait within
    a tenth of a second and stops spotread, which is how Esc and closing the app
    get the instrument back without waiting out a reading that may take a minute.
    """
    command = build_command(
        spotread,
        port=port,
        display_type=display_type,
        skip_calibration=skip_calibration,
    )
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=_no_window(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeterError(f"Could not run spotread: {exc}") from exc

    lines: list[str] = []
    verdict: list[Reading | str] = []
    settled = threading.Event()

    def pump() -> None:
        try:
            for line in process.stdout or ():
                if len(lines) < _MAX_LINES:
                    lines.append(line)
                outcome = classify_line(line)
                if outcome is not None:
                    verdict.append(outcome)
                    return
        except (OSError, ValueError):
            pass
        finally:
            settled.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    finished = _wait(settled, timeout, stop)

    _terminate(process)
    reader.join(timeout=2.0)

    if verdict:
        outcome = verdict[0]
        if isinstance(outcome, Reading):
            return outcome
        raise MeterError(outcome)

    if not finished and stop is not None and stop.is_set():
        raise MeterError("Stopped before the meter answered.")
    if not finished:
        raise MeterError(
            f"The meter did not return a reading within {timeout:g}s. Check that it "
            "is against the screen and that no other calibration software is "
            "holding it open."
        )
    return parse_reading("".join(lines))


def _wait(settled: threading.Event, timeout: float, stop: threading.Event | None) -> bool:
    """``settled.wait(timeout)``, returning early once ``stop`` is set."""
    if stop is None:
        return settled.wait(timeout)
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        if settled.wait(min(0.1, remaining)):
            return True
    return settled.is_set()


def _terminate(process: subprocess.Popen) -> None:
    """Stop spotread and release the instrument.

    A reading that is never going to succeed leaves the process retrying, and it
    holds the meter open while it does, so the next attempt would fail as well.
    """
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def classify_line(line: str) -> "Reading | str | None":
    """A Reading, an error message, or None when the line settles nothing.

    Separated from the process handling so every branch can be tested against
    recorded output, which is the only part of this checkable without hardware.
    """
    match = _RESULT.search(line)
    if match is not None:
        return _reading_from(match)
    for pattern, message in _FAILURES:
        if pattern.search(line):
            return message
    return None


def _reading_from(match: re.Match[str]) -> Reading:
    X, Y, Z, _, x, y = (float(value) for value in match.groups())
    if Y < 0.0:
        # Argyll reports slightly negative luminance for a true black on some
        # instruments. Zero is the honest floor; a negative nit value would
        # propagate into the profile's minimum luminance.
        Y = 0.0
    return Reading(X=X, Y=Y, Z=Z, x=x, y=y)


def parse_reading(stdout: str, stderr: str = "", returncode: int = 0) -> Reading:
    """Pull a measurement out of a complete block of spotread output."""
    match = _RESULT.search(stdout or "")
    if match is not None:
        return _reading_from(match)

    combined = f"{stdout or ''}\n{stderr or ''}"
    for pattern, message in _FAILURES:
        if pattern.search(combined):
            raise MeterError(message)

    detail = combined.strip().splitlines()
    tail = detail[-1] if detail else "no output"
    if returncode != 0:
        raise MeterError(f"spotread failed (exit {returncode}): {tail}")
    raise MeterError(f"No reading in spotread's output: {tail}")
