"""Whether this process is elevated, and asking Windows to restart it that way.

Almost nothing here needs administrator rights, and implying otherwise would be bad
advice. Every colour operation goes through ``WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER``
(see :mod:`.windows_api`), which is per-user by definition, and the system colour
directory grants standard users write access under its default ACL, so profiles install
perfectly well as an ordinary user. Two things genuinely do need elevation, and both
used to fail in ways the user was left to work out alone:

* ``InstallColorProfileW`` on a machine where that directory's ACL has been tightened.
  It fails with Win32 error 5 and nothing is installed. The error text used to say
  "close the app and start it as administrator", which is a chore, not a remedy.
* The watchdog's Task Scheduler registration, which writes a task into the root task
  folder. It fails with ``E_ACCESSDENIED`` and falls back to the ``HKCU`` Run key. That
  fallback works, but it starts the watchdog the instant the user signs in rather than
  after the ten-second delay the scheduled task uses to let the display stack settle.

So this is not a recommendation to run elevated. The button it backs disappears once
there is nothing left for it to buy.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

IS_WINDOWS = sys.platform == "win32"

#: ``GetTokenInformation``'s TokenElevation class, and the access right to ask for it.
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20

#: ``ShellExecuteW`` reports failure by returning 32 or less, and 5 in particular when
#: the user dismissed the UAC prompt. Dismissing it is a decision, not a fault.
SHELL_EXECUTE_MIN_SUCCESS = 32
SE_ERR_ACCESSDENIED = 5
SW_SHOWNORMAL = 1

#: What elevation is actually good for here, in the order a user meets it.
BUYS = (
    "registering the watchdog as a scheduled task, which starts it ten seconds after "
    "sign-in rather than immediately, so the display stack has settled first",
    "installing a profile on a machine where the Windows colour folder has been locked "
    "down, which Windows reports as 'access denied'",
)


class Relaunch(Enum):
    """What came of asking Windows for an elevated copy."""

    STARTED = "started"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class RelaunchResult:
    """The outcome, and something worth putting in the status line."""

    outcome: Relaunch
    message: str = ""

    @property
    def started(self) -> bool:
        return self.outcome is Relaunch.STARTED


def is_elevated() -> bool:
    """True when this process's token is elevated.

    Deliberately not "is this user an administrator". With UAC on, an administrator's
    ordinary session runs on a filtered token, and that filtered token fails exactly the
    two writes described above -- so asking about group membership would answer yes and
    the operation would still fail.
    """
    if not IS_WINDOWS:
        return False
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - a Windows without advapi32 is not a thing
        return False

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return False
    try:
        elevated = wintypes.DWORD()
        returned = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION,
            ctypes.byref(elevated),
            ctypes.sizeof(elevated),
            ctypes.byref(returned),
        )
        return bool(ok) and elevated.value != 0
    finally:
        kernel32.CloseHandle(token)


def relaunch_command(
    argv: Sequence[str] | None = None,
    executable: str | None = None,
    frozen: bool | None = None,
) -> tuple[str, str]:
    """The executable and argument string that would start this app again.

    A frozen build is its own executable and takes its arguments unchanged. A source run
    is under an interpreter that was handed ``-m sdr_hdr_profile_creator``; by the time
    it is running, ``sys.argv[0]`` is the path of ``__main__.py``, which the interpreter
    will not accept back as a module. So the module form is rebuilt rather than reused.
    """
    argv = list(sys.argv if argv is None else argv)
    executable = sys.executable if executable is None else executable
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    rest = argv[1:]
    if frozen:
        return executable, subprocess.list2cmdline(rest)
    return executable, subprocess.list2cmdline(["-m", "sdr_hdr_profile_creator", *rest])


def relaunch_elevated(
    argv: Sequence[str] | None = None,
    shell_execute: Callable[..., object] | None = None,
    working_directory: str | None = None,
    elevated: Callable[[], bool] = is_elevated,
) -> RelaunchResult:
    """Ask Windows to start an elevated copy of this app, and say what happened.

    The caller saves state and closes afterwards, and only when the result is
    :attr:`RelaunchResult.started`. Quitting first would throw away the user's work in
    the ordinary case where they dismiss the prompt.
    """
    if not IS_WINDOWS:
        return RelaunchResult(Relaunch.UNAVAILABLE, "Elevation is a Windows feature.")
    if elevated():
        return RelaunchResult(
            Relaunch.UNAVAILABLE, "This app is already running as administrator."
        )

    program, parameters = relaunch_command(argv)

    if shell_execute is None:
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        except OSError as exc:  # pragma: no cover - shell32 is always present
            return RelaunchResult(Relaunch.FAILED, f"Could not load shell32: {exc}")
        shell_execute = shell32.ShellExecuteW
        shell_execute.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        # Declared as returning an HINSTANCE for historical reasons; the value is really
        # a small status code. Taking it as a void pointer keeps the top bits on 64-bit.
        shell_execute.restype = ctypes.c_void_p

    try:
        raw = shell_execute(
            None, "runas", program, parameters, working_directory, SW_SHOWNORMAL
        )
    except OSError as exc:
        return RelaunchResult(Relaunch.FAILED, f"Could not restart elevated: {exc}")

    code = int(raw or 0)
    if code > SHELL_EXECUTE_MIN_SUCCESS:
        return RelaunchResult(Relaunch.STARTED)
    if code == SE_ERR_ACCESSDENIED:
        return RelaunchResult(
            Relaunch.DECLINED,
            "The administrator prompt was dismissed. Nothing was changed.",
        )
    return RelaunchResult(
        Relaunch.FAILED,
        f"Windows would not start an elevated copy (ShellExecuteW returned {code}).",
    )
