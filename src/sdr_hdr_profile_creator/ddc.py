"""Reading the monitor's own controls over DDC/CI.

The app only reads: the monitor's settings are recorded with every measurement, so a
run can say when the display it describes has changed underneath it. It never writes.

**What this display actually exposes**, read from an ASUS PG32UCDM with HDR on:
brightness (0x10), contrast (0x12), colour preset (0x14), RGB gain (0x16/0x18/0x1A), RGB
black level (0x6C/0x6E/0x70), gamma (0x72) and picture mode (0xDC), 25 codes in total.

**Why there is no write path.** An RGB-gain tuning loop was built and removed. On this
monitor a write is not proof the image changed: red gain written to 70 read back 70 and
moved meter-measured white by 0.0001 in xy -- noise. The monitor accepts, stores and
reports the value, and does not apply it while HDR is on, so only a meter can confirm a
write, and nothing in the app called the loop.

**Reads fail intermittently and mean nothing on one attempt.** The first probe of this
hardware reported brightness, contrast and red and green gain as unsupported; a second
pass over the whole range found all four, stable. A single failed read is a cold link,
not an absent feature, so every read here retries before concluding anything. Getting
this wrong the other way is worse than useless: it would report a monitor as
uncalibratable when it is not.

The Windows entry points live behind a small object so the retry policy can be exercised
against a fake.
"""

from __future__ import annotations

import ctypes
import platform
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator, Protocol

IS_WINDOWS = platform.system() == "Windows"

#: MCCS 2.2 codes this project has a use for. Names as the spec gives them.
LUMINANCE = 0x10
CONTRAST = 0x12
COLOUR_PRESET = 0x14
RED_GAIN = 0x16
GREEN_GAIN = 0x18
BLUE_GAIN = 0x1A
GAMMA = 0x72
PICTURE_MODE = 0xDC

#: The monitor's HDR preset. A VENDOR code, not MCCS, identified on an ASUS PG32UCDM on
#: 2026-09-04 by read-diff: a 256-code scan taken before and after changing Image > HDR
#: Setting in the OSD moved exactly one code out of the 64 that answer. Gaming HDR reads 2,
#: Console HDR reads 3.
#:
#: Worth a name because of what separates those two values. Measured in one controlled
#: test, same session and same instrument: (R+G+B)/white was 2.19 under Gaming HDR and 1.04
#: under Console HDR, every primary falling by about 2.36x while white moved 1.13. The
#: standard codes are blind to it -- PICTURE_MODE and COLOUR_PRESET both read 5 in either
#: state -- so a run record built from them alone reports a monitor that did not change.
#:
#: Vendor codes mean different things on different hardware, so this is recorded and
#: compared against itself, never interpreted as a preset name.
HDR_SETTING = 0xE2

#: How many times to ask before believing a code is unsupported, and how long to wait
#: between attempts. Five at 120 ms is well inside the time a patch needs to settle
#: anyway, so a retry costs nothing the measurement was not already spending.
READ_ATTEMPTS = 5
RETRY_PAUSE_SECONDS = 0.12


@dataclass(frozen=True, slots=True)
class Control:
    """One VCP feature as the monitor currently reports it."""

    code: int
    current: int
    maximum: int


class Link(Protocol):
    """The RAW read a monitor link provides, a single attempt.

    Single attempt on purpose. Retrying belongs in :func:`read_control`, where a fake can
    exercise it -- 29% of single reads fail on the hardware this was written against, so
    the retry policy is the part most worth testing and the part a real-monitor-only
    implementation would leave uncovered.
    """

    def read(self, code: int) -> Control | None: ...


class UnavailableLink:
    """What you get on a machine with no DDC/CI. Reads nothing."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def read(self, code: int) -> Control | None:  # noqa: ARG002
        return None


if IS_WINDOWS:
    _dxva2 = ctypes.windll.dxva2
    _user32 = ctypes.windll.user32

    class _PhysicalMonitor(ctypes.Structure):
        _fields_ = [
            ("handle", wintypes.HANDLE),
            ("description", wintypes.WCHAR * 128),
        ]

    _MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )


class MonitorLink:
    """A real monitor, addressed through dxva2.

    It holds a physical-monitor handle, which Windows allocates for it and keeps until
    DestroyPhysicalMonitor. Nothing ever called that: every open_link leaked one handle per
    attached monitor for the life of the process, twice per measurement. close() releases
    it, and so does dropping the last reference -- which is how every caller uses a link.
    """

    def __init__(self, handle: int, description: str) -> None:
        self._handle = handle
        self.description = description

    def close(self) -> None:
        """Release the handle. Safe to call twice; reads after it simply fail."""
        handle, self._handle = self._handle, 0
        if handle:
            _dxva2.DestroyPhysicalMonitor(wintypes.HANDLE(handle))

    def __enter__(self) -> "MonitorLink":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 -- interpreter shutdown can take dxva2 first
            pass

    def read(self, code: int) -> Control | None:
        """One attempt. The retrying is in :func:`read_control`, so that the policy is
        one tested thing rather than something only real hardware exercises."""
        current = wintypes.DWORD()
        maximum = wintypes.DWORD()
        ok = _dxva2.GetVCPFeatureAndVCPFeatureReply(
            wintypes.HANDLE(self._handle), ctypes.c_ubyte(code), None,
            ctypes.byref(current), ctypes.byref(maximum),
        )
        if not ok:
            return None
        return Control(code=code, current=int(current.value), maximum=int(maximum.value))


def monitors() -> Iterator[MonitorLink]:
    """Every physical monitor the DDC/CI layer can address."""
    if not IS_WINDOWS:
        return

    handles: list[int] = []

    def collect(monitor, _hdc, _rect, _data):
        handles.append(monitor)
        return True

    _user32.EnumDisplayMonitors(None, None, _MONITOR_ENUM_PROC(collect), 0)

    for handle in handles:
        count = wintypes.DWORD()
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(handle, ctypes.byref(count)):
            continue
        if count.value == 0:
            continue
        physical = (_PhysicalMonitor * count.value)()
        if not _dxva2.GetPhysicalMonitorsFromHMONITOR(handle, count.value, physical):
            continue
        for item in physical:
            yield MonitorLink(int(item.handle), item.description)


def open_link(friendly_name: str = "") -> Link:
    """The monitor matching ``friendly_name``, or the only one, or an unavailable link.

    Matching is loose because the DDC/CI description and the Windows friendly name come
    from different places and agree only most of the time. With one monitor attached the
    name is not consulted at all.
    """
    if not IS_WINDOWS:
        return UnavailableLink("DDC/CI is a Windows interface")

    try:
        found = list(monitors())
    except OSError as error:
        return UnavailableLink(f"the display could not be opened: {error}")

    if not found:
        return UnavailableLink("no monitor answered; DDC/CI may be off in its own menu")
    if len(found) == 1:
        return found[0]

    wanted = friendly_name.strip().casefold()
    chosen = next(
        (link for link in found if wanted and wanted in link.description.casefold()), None
    )
    # Every monitor was opened to be asked its name. Release the ones not handed back.
    for link in found:
        if link is not chosen:
            link.close()
    if chosen is not None:
        return chosen
    return UnavailableLink(
        f"{len(found)} monitors answered and none matched {friendly_name!r}"
    )


def read_control(
    link: Link,
    code: int,
    *,
    attempts: int = READ_ATTEMPTS,
    pause: float = RETRY_PAUSE_SECONDS,
) -> Control | None:
    """Ask for a control until it answers, or conclude it is not there.

    The retry is the whole point. Probing this project's own hardware once per code
    reported brightness, contrast, red gain and green gain as unsupported; a second pass
    over the full range found all four, stable across two reads. A single failure is a
    cold link, and treating it as an absent feature would tell someone their perfectly
    calibratable monitor cannot be calibrated.
    """
    for attempt in range(max(1, attempts)):
        control = link.read(code)
        if control is not None:
            return control
        if attempt + 1 < attempts:
            time.sleep(pause)
    return None

