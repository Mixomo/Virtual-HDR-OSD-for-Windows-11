from __future__ import annotations

import ctypes
import logging
import os
import platform
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


@dataclass(slots=True)
class DisplayInfo:
    key: str
    friendly_name: str
    gdi_name: str
    device_path: str
    adapter_low: int
    adapter_high: int
    source_id: int
    target_id: int
    advanced_color_supported: bool
    advanced_color_enabled: bool
    bits_per_color_channel: int
    advanced_color_kind: str = "SDR"

    @property
    def current_mode(self) -> str:
        return "HDR" if self.advanced_color_kind == "HDR" else "SDR"

    @property
    def acm_enabled(self) -> bool:
        """True when Windows is in SDR wide-gamut Auto Color Management mode."""
        return self.advanced_color_kind == "WCG"

    @property
    def stable_key(self) -> str:
        """Identity that survives reboots, unlike ``key``.

        ``key`` embeds the adapter LUID, which Windows reissues on reboot and on
        driver restarts, so anything the user configured against it would be lost.
        The monitor device path is derived from the EDID and stays put, so it is
        the right anchor for remembered per-display settings.
        """
        if self.device_path:
            return self.device_path
        return f"{self.friendly_name}|{self.gdi_name}"

    @property
    def label(self) -> str:
        # Capability and current state in one phrase. Listing both separately
        # produced the unreadable "… · HDR · HDR" for a display in HDR mode.
        if not self.advanced_color_supported:
            status = "SDR only"
        elif self.advanced_color_kind == "HDR":
            status = "HDR on"
        elif self.acm_enabled:
            status = "HDR off · ACM/WCG"
        else:
            status = "HDR off"
        return f"{self.friendly_name}  ·  {self.gdi_name}  ·  {status}"


class WindowsColorError(RuntimeError):
    pass


class NoDefaultProfile(WindowsColorError):
    """The display has no profile associated for that mode.

    Its own type because one caller needs to tell it from a failed call. The measurement
    run's display probe treats an unreadable field as "not read" and ignores it, so if
    a REMOVED association raised the same type as a flaky Win32 call, the watchdog
    dropping the HDR profile mid-run -- a real change to the display -- would be
    swallowed as noise. Still a WindowsColorError, so nothing that catches that stops
    working.
    """


if IS_WINDOWS:
    ULONG = wintypes.ULONG
    UINT32 = wintypes.UINT
    LONG = wintypes.LONG
    BOOL = wintypes.BOOL
    WCHAR = wintypes.WCHAR

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", LONG)]

    class POINTL(ctypes.Structure):
        _fields_ = [("x", LONG), ("y", LONG)]

    class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", UINT32), ("Denominator", UINT32)]

    class DISPLAYCONFIG_2DREGION(ctypes.Structure):
        _fields_ = [("cx", UINT32), ("cy", UINT32)]

    class DISPLAYCONFIG_VIDEO_SIGNAL_INFO(ctypes.Structure):
        _fields_ = [
            ("pixelRate", ctypes.c_uint64),
            ("hSyncFreq", DISPLAYCONFIG_RATIONAL),
            ("vSyncFreq", DISPLAYCONFIG_RATIONAL),
            ("activeSize", DISPLAYCONFIG_2DREGION),
            ("totalSize", DISPLAYCONFIG_2DREGION),
            ("videoStandard", UINT32),
            ("scanLineOrdering", UINT32),
        ]

    class DISPLAYCONFIG_TARGET_MODE(ctypes.Structure):
        _fields_ = [("targetVideoSignalInfo", DISPLAYCONFIG_VIDEO_SIGNAL_INFO)]

    class DISPLAYCONFIG_SOURCE_MODE(ctypes.Structure):
        _fields_ = [
            ("width", UINT32),
            ("height", UINT32),
            ("pixelFormat", UINT32),
            ("position", POINTL),
        ]

    class DISPLAYCONFIG_DESKTOP_IMAGE_INFO(ctypes.Structure):
        _fields_ = [
            ("PathSourceSize", POINTL),
            ("DesktopImageRegion", wintypes.RECT),
            ("DesktopImageClip", wintypes.RECT),
        ]

    class DISPLAYCONFIG_MODE_INFO_UNION(ctypes.Union):
        _fields_ = [
            ("targetMode", DISPLAYCONFIG_TARGET_MODE),
            ("sourceMode", DISPLAYCONFIG_SOURCE_MODE),
            ("desktopImageInfo", DISPLAYCONFIG_DESKTOP_IMAGE_INFO),
        ]

    class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
        _anonymous_ = ("mode",)
        _fields_ = [
            ("infoType", UINT32),
            ("id", UINT32),
            ("adapterId", LUID),
            ("mode", DISPLAYCONFIG_MODE_INFO_UNION),
        ]

    class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", UINT32),
            ("modeInfoIdx", UINT32),
            ("statusFlags", UINT32),
        ]

    class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID),
            ("id", UINT32),
            # The SDK has a union { UINT32 modeInfoIdx; struct { UINT32
            # desktopModeInfoIdx:16; targetModeInfoIdx:16; }; } here. Omitting it
            # made this struct 44 bytes instead of 48, so QueryDisplayConfig wrote
            # 4 bytes per path past the end of the buffer and every field from
            # outputTechnology onward — including the adapter LUID and ids of the
            # second and later displays — was read from the wrong offset.
            ("modeInfoIdx", UINT32),
            ("outputTechnology", UINT32),
            ("rotation", UINT32),
            ("scaling", UINT32),
            ("refreshRate", DISPLAYCONFIG_RATIONAL),
            ("scanLineOrdering", UINT32),
            ("targetAvailable", BOOL),
            ("statusFlags", UINT32),
        ]

    class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
        _fields_ = [
            ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
            ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
            ("flags", UINT32),
        ]

    # QueryDisplayConfig takes an element count, not an element size, so a struct
    # that disagrees with the OS layout cannot be rejected by the API — it silently
    # overruns the buffer and misparses every element after the first. Fail loudly
    # at import instead.
    _EXPECTED_SIZES = (
        (DISPLAYCONFIG_PATH_SOURCE_INFO, 20),
        (DISPLAYCONFIG_PATH_TARGET_INFO, 48),
        (DISPLAYCONFIG_PATH_INFO, 72),
        (DISPLAYCONFIG_VIDEO_SIGNAL_INFO, 48),
        (DISPLAYCONFIG_SOURCE_MODE, 20),
        (DISPLAYCONFIG_TARGET_MODE, 48),
        (DISPLAYCONFIG_DESKTOP_IMAGE_INFO, 40),
        (DISPLAYCONFIG_MODE_INFO, 64),
    )
    for _structure, _expected in _EXPECTED_SIZES:
        _actual = ctypes.sizeof(_structure)
        if _actual != _expected:
            raise RuntimeError(
                f"{_structure.__name__} is {_actual} bytes but Windows expects "
                f"{_expected}; QueryDisplayConfig would overrun its buffer"
            )

    class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
        _fields_ = [
            ("type", UINT32),
            ("size", UINT32),
            ("adapterId", LUID),
            ("id", UINT32),
        ]

    class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("viewGdiDeviceName", WCHAR * 32),
        ]

    class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("flags", UINT32),
            ("outputTechnology", UINT32),
            ("edidManufactureId", wintypes.WORD),
            ("edidProductCodeId", wintypes.WORD),
            ("connectorInstance", UINT32),
            ("monitorFriendlyDeviceName", WCHAR * 64),
            ("monitorDevicePath", WCHAR * 128),
        ]

    class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("value", UINT32),
            ("colorEncoding", UINT32),
            ("bitsPerColorChannel", UINT32),
        ]

    class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("value", UINT32),
            ("colorEncoding", UINT32),
            ("bitsPerColorChannel", UINT32),
            ("activeColorMode", UINT32),
        ]

    class DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("value", UINT32),  # bit 0 = enableAdvancedColor
        ]

    class DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
        _fields_ = [
            ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
            ("SDRWhiteLevel", ULONG),
        ]

    # Same reasoning as the path structs: DisplayConfigGetDeviceInfo trusts the
    # size the caller puts in the header, so a wrong layout is never rejected.
    for _structure, _expected in (
        (DISPLAYCONFIG_DEVICE_INFO_HEADER, 20),
        (DISPLAYCONFIG_SOURCE_DEVICE_NAME, 84),
        (DISPLAYCONFIG_TARGET_DEVICE_NAME, 420),
        (DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO, 32),
        (DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2, 36),
        (DISPLAYCONFIG_SDR_WHITE_LEVEL, 24),
        (DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE, 24),
    ):
        _actual = ctypes.sizeof(_structure)
        if _actual != _expected:
            raise RuntimeError(
                f"{_structure.__name__} is {_actual} bytes but Windows expects {_expected}"
            )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    mscms = ctypes.WinDLL("mscms", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetDisplayConfigBufferSizes.argtypes = [UINT32, ctypes.POINTER(UINT32), ctypes.POINTER(UINT32)]
    user32.GetDisplayConfigBufferSizes.restype = LONG
    user32.QueryDisplayConfig.argtypes = [
        UINT32,
        ctypes.POINTER(UINT32),
        ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
        ctypes.POINTER(UINT32),
        ctypes.POINTER(DISPLAYCONFIG_MODE_INFO),
        ctypes.c_void_p,
    ]
    user32.QueryDisplayConfig.restype = LONG
    user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
    user32.DisplayConfigGetDeviceInfo.restype = LONG
    user32.DisplayConfigSetDeviceInfo.argtypes = [ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
    user32.DisplayConfigSetDeviceInfo.restype = LONG

    mscms.InstallColorProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    mscms.InstallColorProfileW.restype = BOOL
    mscms.UninstallColorProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, BOOL]
    mscms.UninstallColorProfileW.restype = BOOL
    mscms.GetColorDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    mscms.GetColorDirectoryW.restype = BOOL
    if hasattr(mscms, "ColorProfileAddDisplayAssociation"):
        mscms.ColorProfileAddDisplayAssociation.argtypes = [
            UINT32,
            wintypes.LPCWSTR,
            LUID,
            UINT32,
            BOOL,
            BOOL,
        ]
        mscms.ColorProfileAddDisplayAssociation.restype = LONG
    if hasattr(mscms, "ColorProfileSetDisplayDefaultAssociation"):
        mscms.ColorProfileSetDisplayDefaultAssociation.argtypes = [
            UINT32,
            wintypes.LPCWSTR,
            UINT32,
            UINT32,
            LUID,
            UINT32,
        ]
        mscms.ColorProfileSetDisplayDefaultAssociation.restype = LONG
    if hasattr(mscms, "ColorProfileRemoveDisplayAssociation"):
        mscms.ColorProfileRemoveDisplayAssociation.argtypes = [
            UINT32,
            wintypes.LPCWSTR,
            LUID,
            UINT32,
            BOOL,
        ]
        mscms.ColorProfileRemoveDisplayAssociation.restype = LONG

    if hasattr(mscms, "ColorProfileGetDisplayDefault"):
        mscms.ColorProfileGetDisplayDefault.argtypes = [
            UINT32,
            LUID,
            UINT32,
            UINT32,
            UINT32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        mscms.ColorProfileGetDisplayDefault.restype = LONG
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p


QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 = 15
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11
DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE = 10
DISPLAYCONFIG_ADVANCED_COLOR_MODE_SDR = 0
DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG = 1
DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR = 2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER = 1
CPT_ICC = 0
CPST_STANDARD_DISPLAY_COLOR_MODE = 7
CPST_EXTENDED_DISPLAY_COLOR_MODE = 8


def _format_windows_error(prefix: str) -> WindowsColorError:
    error = ctypes.get_last_error()
    detail = f"{prefix}: {ctypes.FormatError(error).strip()} (Win32 {error})"
    if error == 5:
        detail += (
            ". Windows denied profile installation. Press Run as Admin at the top of "
            "the window to restart with the rights it wants; your edits are kept"
        )
    return WindowsColorError(detail)


def _hresult_error(prefix: str, result: int) -> WindowsColorError:
    unsigned = ctypes.c_uint32(result).value
    return WindowsColorError(f"{prefix}: HRESULT 0x{unsigned:08X}")


def _device_info(packet: ctypes.Structure) -> int:
    return int(user32.DisplayConfigGetDeviceInfo(ctypes.byref(packet.header)))


def enumerate_displays() -> list[DisplayInfo]:
    if not IS_WINDOWS:
        return []

    for _attempt in range(4):
        path_count = UINT32()
        mode_count = UINT32()
        result = user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count)
        )
        if result != ERROR_SUCCESS:
            raise _hresult_error("GetDisplayConfigBufferSizes failed", result)

        paths = (DISPLAYCONFIG_PATH_INFO * max(1, path_count.value))()
        modes = (DISPLAYCONFIG_MODE_INFO * max(1, mode_count.value))()
        result = user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(path_count),
            paths,
            ctypes.byref(mode_count),
            modes,
            None,
        )
        if result == ERROR_INSUFFICIENT_BUFFER:
            continue
        if result != ERROR_SUCCESS:
            raise _hresult_error("QueryDisplayConfig failed", result)
        break
    else:
        raise WindowsColorError("Display topology kept changing while it was queried")

    displays: list[DisplayInfo] = []
    seen: set[str] = set()
    for index in range(path_count.value):
        path = paths[index]

        source = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        source.header.size = ctypes.sizeof(source)
        source.header.adapterId = path.sourceInfo.adapterId
        source.header.id = path.sourceInfo.id
        source_result = _device_info(source)
        gdi_name = source.viewGdiDeviceName if source_result == ERROR_SUCCESS else f"DISPLAY {index + 1}"

        target = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        target.header.size = ctypes.sizeof(target)
        target.header.adapterId = path.targetInfo.adapterId
        target.header.id = path.targetInfo.id
        target_result = _device_info(target)
        friendly_name = target.monitorFriendlyDeviceName if target_result == ERROR_SUCCESS else "Display"
        device_path = target.monitorDevicePath if target_result == ERROR_SUCCESS else ""
        if not friendly_name:
            friendly_name = gdi_name

        # Windows 11 exposes a second-generation query that distinguishes HDR
        # from WCG. Prefer it so "current mode" means actual active HDR, then
        # fall back to the older Advanced Color boolean on earlier builds.
        advanced2 = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2()
        advanced2.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2
        advanced2.header.size = ctypes.sizeof(advanced2)
        advanced2.header.adapterId = path.targetInfo.adapterId
        advanced2.header.id = path.targetInfo.id
        advanced2_result = _device_info(advanced2)
        if advanced2_result == ERROR_SUCCESS:
            value = advanced2.value
            supported = bool(value & (1 << 4))  # highDynamicRangeSupported
            active_mode = int(advanced2.activeColorMode)
            kind = {
                DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG: "WCG",
                DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR: "HDR",
            }.get(active_mode, "SDR")
            enabled = kind == "HDR"
            bits = int(advanced2.bitsPerColorChannel)
        else:
            advanced = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
            advanced.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
            advanced.header.size = ctypes.sizeof(advanced)
            advanced.header.adapterId = path.targetInfo.adapterId
            advanced.header.id = path.targetInfo.id
            advanced_result = _device_info(advanced)
            if advanced_result != ERROR_SUCCESS:
                raise WindowsColorError(f"Advanced Color query failed (Win32 {advanced_result})")
            value = advanced.value
            supported = bool(value & 0x1)
            enabled = bool(value & 0x2)
            kind = "HDR" if enabled else "SDR"
            bits = int(advanced.bitsPerColorChannel) if advanced_result == ERROR_SUCCESS else 0

        key = (
            f"{path.targetInfo.adapterId.HighPart & 0xFFFFFFFF:08X}:"
            f"{path.targetInfo.adapterId.LowPart:08X}:{path.sourceInfo.id}:{path.targetInfo.id}"
        )
        if key in seen:
            continue
        seen.add(key)
        displays.append(
            DisplayInfo(
                key=key,
                friendly_name=friendly_name,
                gdi_name=gdi_name,
                device_path=device_path,
                adapter_low=int(path.targetInfo.adapterId.LowPart),
                adapter_high=int(path.targetInfo.adapterId.HighPart),
                source_id=int(path.sourceInfo.id),
                target_id=int(path.targetInfo.id),
                advanced_color_supported=supported,
                advanced_color_enabled=enabled,
                bits_per_color_channel=bits,
                advanced_color_kind=kind,
            )
        )
    return displays


def _luid(display: DisplayInfo) -> "LUID":
    value = LUID()
    value.LowPart = display.adapter_low
    value.HighPart = display.adapter_high
    return value


def get_color_directory() -> Path:
    if not IS_WINDOWS:
        raise WindowsColorError("Windows color directory is unavailable outside Windows")
    size = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    mscms.GetColorDirectoryW(None, None, ctypes.byref(size))
    if not size.value:
        error = ctypes.get_last_error()
        raise WindowsColorError(f"GetColorDirectoryW size query failed (Win32 {error})")
    buffer = ctypes.create_unicode_buffer(size.value + 2)
    if not mscms.GetColorDirectoryW(None, buffer, ctypes.byref(size)):
        raise _format_windows_error("GetColorDirectoryW failed")
    return Path(buffer.value)


def resolve_profile_path(profile_name: str) -> Path:
    candidate = Path(profile_name)
    if candidate.is_file():
        return candidate
    return get_color_directory() / candidate.name


#: The one write right the colour folder still grants on a file this account does not
#: own. Deliberately not GENERIC_WRITE: that bundles FILE_APPEND_DATA and
#: FILE_WRITE_ATTRIBUTES, which are refused, so asking for it fails outright -- and so
#: does open(path, "wb"), which asks for exactly that.
FILE_WRITE_DATA = 0x0002
# Another process has the file open without sharing writes. Transient, unlike a
# permissions failure, and the watchdog causes one every few seconds.
ERROR_SHARING_VIOLATION = 32
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def overwrite_installed_profile(profile_name: str, payload: bytes) -> bool:
    """Replace an installed profile's bytes in place. True when it now matches.

    ``InstallColorProfileW`` will not overwrite a destination that already exists: it
    returns TRUE and copies nothing. The usual answer is to uninstall first, but a
    profile written by an elevated run is owned by ``BUILTIN\\Administrators`` and this
    account is refused DELETE, so the uninstall does nothing either and the pair
    silently freezes at whatever bytes were installed that day.

    Writing the bytes straight into the existing file is the way out, because the ACL
    still allows FILE_WRITE_DATA even where it denies DELETE and GENERIC_WRITE.

    A shorter payload used to be refused here, on the grounds that without a truncate
    right it would leave the tail of the old profile behind. ``SetEndOfFile`` turns out
    to succeed on a FILE_WRITE_DATA handle against exactly the files this has to write,
    so the refusal cost more than it protected: the embedded state tag carries the
    measured greyscale response now, and the JSON for 198 floats is not the same length
    twice running. A profile 48 bytes shorter than the one installed is ordinary, and
    every one of those applies failed with a message about elevation that had nothing to
    do with it. The truncation is still checked, and a shorter payload that cannot be
    truncated is still refused rather than written half over the old one.

    Never trusts the write. The return value is a read-back comparison, because every
    other step in this story returned success while changing nothing.
    """
    if not IS_WINDOWS:
        raise WindowsColorError("Colour profiles can only be installed on Windows")

    target = get_color_directory() / Path(profile_name).name
    if not target.is_file():
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    kernel32.SetEndOfFile.restype = wintypes.BOOL

    # Retried, for the same reason _write_json_atomic retries: the watchdog re-asserts
    # the association every few seconds and Windows opens the profile to do it, so an
    # apply that collides with one gets a sharing violation rather than a permissions
    # failure. Without this, a Live Apply edit landing in that window was reported as
    # "installed by an earlier elevated run, press Run as Admin" -- advice that is
    # useless for a collision that clears in a few milliseconds.
    handle = INVALID_HANDLE_VALUE
    for attempt in range(5):
        handle = kernel32.CreateFileW(
            str(target), FILE_WRITE_DATA, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None
        )
        if handle != INVALID_HANDLE_VALUE:
            break
        if ctypes.get_last_error() != ERROR_SHARING_VIOLATION:
            return False
        time.sleep(0.04 * (attempt + 1))
    if handle == INVALID_HANDLE_VALUE:
        return False
    try:
        written = wintypes.DWORD(0)
        buffer = ctypes.create_string_buffer(payload, len(payload))
        if not kernel32.WriteFile(handle, buffer, len(payload), ctypes.byref(written), None):
            return False
        if written.value != len(payload):
            return False
        # Cut off whatever the old profile had beyond the new one. Without this a
        # shorter payload leaves a tail that no ICC reader would reach -- the header
        # carries the size -- but that every byte-for-byte comparison would fail on,
        # including the read-back below.
        position = ctypes.c_longlong(0)
        if not kernel32.SetFilePointerEx(
            handle, ctypes.c_longlong(len(payload)), ctypes.byref(position), 0
        ):
            return False
        if not kernel32.SetEndOfFile(handle):
            return False
    finally:
        kernel32.CloseHandle(handle)

    try:
        return target.read_bytes() == payload
    except OSError:
        return False


def get_default_profile(display: DisplayInfo, mode: str) -> str:
    if not IS_WINDOWS or not hasattr(mscms, "ColorProfileGetDisplayDefault"):
        raise WindowsColorError("Default-profile read-back is unavailable on this Windows build")
    subtype = CPST_EXTENDED_DISPLAY_COLOR_MODE if mode == "HDR" else CPST_STANDARD_DISPLAY_COLOR_MODE
    allocated = ctypes.c_void_p()
    result = mscms.ColorProfileGetDisplayDefault(
        WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
        _luid(display),
        display.source_id,
        CPT_ICC,
        subtype,
        ctypes.byref(allocated),
    )
    try:
        if result < 0:
            raise _hresult_error("ColorProfileGetDisplayDefault failed", result)
        if not allocated.value:
            raise NoDefaultProfile("Windows returned an empty default profile name")
        return ctypes.wstring_at(allocated.value)
    finally:
        if allocated.value:
            kernel32.LocalFree(allocated.value)


def reapply_existing_default_profile(display: DisplayInfo, mode: str, profile_name: str) -> str:
    """Re-set an already-associated Windows display profile as default.

    This intentionally does not install a profile and does not invent a fallback. It is
    used by the SDR watchdog to restore the STANDARD association that Windows already had.
    """
    if not IS_WINDOWS or not hasattr(mscms, "ColorProfileSetDisplayDefaultAssociation"):
        raise WindowsColorError("Default-profile association API is unavailable on this Windows build")
    if not profile_name:
        raise WindowsColorError("No profile name was supplied")
    # Only reapply profiles that still resolve inside Windows' color directory (or an
    # explicit existing path). This prevents the watchdog from manufacturing associations.
    resolved = resolve_profile_path(profile_name)
    if not resolved.is_file():
        raise WindowsColorError(f"Remembered profile is no longer installed: {profile_name}")
    subtype = CPST_EXTENDED_DISPLAY_COLOR_MODE if mode == "HDR" else CPST_STANDARD_DISPLAY_COLOR_MODE
    result = mscms.ColorProfileSetDisplayDefaultAssociation(
        WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
        Path(profile_name).name,
        CPT_ICC,
        subtype,
        _luid(display),
        display.source_id,
    )
    if result < 0:
        raise _hresult_error("ColorProfileSetDisplayDefaultAssociation failed", result)
    active = get_default_profile(display, mode)
    if Path(active).name.casefold() != Path(profile_name).name.casefold():
        raise WindowsColorError(
            f"Windows read-back mismatch: expected {Path(profile_name).name}, received {active or '<empty>'}"
        )
    return active


def associate_profile(profile_name: str, display: DisplayInfo, mode: str) -> None:
    """Ensure a already-installed profile is associated with the display.

    Setting a profile as the display default does not persist unless the profile
    is also in that display's association list, and removing a profile drops it
    from that list. Re-adding is idempotent and cheap: unlike
    install_and_associate_profile it does not copy the file into the colour
    directory, so it is safe to call on every apply.
    """
    if not IS_WINDOWS or not hasattr(mscms, "ColorProfileAddDisplayAssociation"):
        return
    result = mscms.ColorProfileAddDisplayAssociation(
        WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
        Path(profile_name).name,
        _luid(display),
        display.source_id,
        True,
        mode == "HDR",
    )
    if result < 0:
        raise _hresult_error("ColorProfileAddDisplayAssociation failed", result)


def install_and_associate_profile(profile_path: Path, display: DisplayInfo, mode: str, make_default: bool = True) -> str:
    if not IS_WINDOWS:
        raise WindowsColorError("Windows profile APIs are only available on Windows")
    if not profile_path.is_file():
        raise WindowsColorError(f"Profile not found: {profile_path}")
    required = (
        "ColorProfileAddDisplayAssociation",
        "ColorProfileSetDisplayDefaultAssociation",
        "ColorProfileGetDisplayDefault",
    )
    if any(not hasattr(mscms, name) for name in required):
        raise WindowsColorError("Modern display profile association APIs are unavailable on this Windows build")

    absolute = str(profile_path.resolve())
    if not mscms.InstallColorProfileW(None, absolute):
        raise _format_windows_error("InstallColorProfileW failed")

    profile_name = profile_path.name
    advanced = mode == "HDR"
    subtype = CPST_EXTENDED_DISPLAY_COLOR_MODE if advanced else CPST_STANDARD_DISPLAY_COLOR_MODE
    try:
        result = mscms.ColorProfileAddDisplayAssociation(
            WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            profile_name,
            _luid(display),
            display.source_id,
            True,
            advanced,
        )
        if result < 0:
            raise _hresult_error("ColorProfileAddDisplayAssociation failed", result)

        if make_default:
            result = mscms.ColorProfileSetDisplayDefaultAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
                profile_name,
                CPT_ICC,
                subtype,
                _luid(display),
                display.source_id,
            )
            if result < 0:
                raise _hresult_error("ColorProfileSetDisplayDefaultAssociation failed", result)

            active = get_default_profile(display, mode)
            if Path(active).name.casefold() != profile_name.casefold():
                raise WindowsColorError(
                    f"Windows read-back mismatch: expected {profile_name}, received {active or '<empty>'}"
                )
        return profile_name
    except Exception:
        if hasattr(mscms, "ColorProfileRemoveDisplayAssociation"):
            mscms.ColorProfileRemoveDisplayAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
                profile_name,
                _luid(display),
                display.source_id,
                advanced,
            )
        mscms.UninstallColorProfileW(None, profile_name, True)
        raise


def remove_profile(profile_name: str, display: DisplayInfo, mode: str) -> tuple[bool, str]:
    if not IS_WINDOWS or not profile_name:
        return False, "Not removed"
    advanced = mode == "HDR"
    messages: list[str] = []
    association_removed = False

    if hasattr(mscms, "ColorProfileRemoveDisplayAssociation"):
        result = mscms.ColorProfileRemoveDisplayAssociation(
            WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            profile_name,
            _luid(display),
            display.source_id,
            advanced,
        )
        if result >= 0:
            association_removed = True
        else:
            messages.append(f"association HRESULT 0x{ctypes.c_uint32(result).value:08X}")

    if mscms.UninstallColorProfileW(None, profile_name, True):
        messages.append("uninstalled")
        return True, ", ".join(messages)

    error = ctypes.get_last_error()
    messages.append(f"uninstall Win32 {error}")
    return association_removed, ", ".join(messages)


def set_hdr_enabled(display: DisplayInfo, enabled: bool) -> None:
    """Turn HDR on or off for one specific display.

    Win + Alt + B only toggles whichever display Windows considers current, so it
    cannot target a chosen monitor and cannot be made idempotent. This is the
    documented per-target DisplayConfig setter behind the HDR switch in Settings.
    """
    if not IS_WINDOWS:
        raise WindowsColorError("HDR switching is only available on Windows")
    if not display.advanced_color_supported:
        raise WindowsColorError(f"{display.friendly_name} does not report HDR support to Windows")
    packet = DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE()
    packet.header.type = DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE
    packet.header.size = ctypes.sizeof(packet)
    packet.header.adapterId = _luid(display)
    packet.header.id = display.target_id
    packet.value = 1 if enabled else 0
    result = int(user32.DisplayConfigSetDeviceInfo(ctypes.byref(packet.header)))
    if result != ERROR_SUCCESS:
        raise _hresult_error(f"Could not turn HDR {'on' if enabled else 'off'}", result)


def list_installed_profiles() -> list[str]:
    """Filenames of every ICC/ICM profile installed in the Windows colour directory."""
    if not IS_WINDOWS:
        return []
    try:
        directory = get_color_directory()
    except WindowsColorError:
        return []
    try:
        names = [
            entry.name
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix.lower() in (".icc", ".icm")
        ]
    except OSError:
        return []
    return sorted(names, key=str.casefold)


def get_sdr_white_level_nits(display: DisplayInfo) -> float:
    """Return Windows' current SDR reference white for an HDR display.

    This is the public DisplayConfig read API behind the SDR-content-brightness
    setting. Windows exposes the current white level, not a documented public
    setter for that Settings slider.
    """
    if not IS_WINDOWS:
        raise WindowsColorError("SDR white-level query is only available on Windows")
    packet = DISPLAYCONFIG_SDR_WHITE_LEVEL()
    packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL
    packet.header.size = ctypes.sizeof(packet)
    packet.header.adapterId = _luid(display)
    packet.header.id = display.target_id
    result = _device_info(packet)
    if result != ERROR_SUCCESS:
        raise _hresult_error("SDR white-level query failed", result)
    return float(packet.SDRWhiteLevel) / 1000.0 * 80.0


def open_windows_display_settings() -> None:
    if not IS_WINDOWS:
        return
    os.startfile("ms-settings:display")  # type: ignore[attr-defined]


def open_windows_color_profile_directory() -> None:
    """Open Windows' canonical ICC/ICM profile directory in File Explorer."""
    if not IS_WINDOWS:
        return
    os.startfile(str(get_color_directory()))  # type: ignore[attr-defined]


# The standalone watchdog holds this for as long as it runs; it is how the
# watchdog stops a second copy of itself from starting.
WATCHDOG_SINGLETON_MUTEX = r"Local\ColorProfileModeWatchdogStandalone"


def watchdog_is_running() -> bool:
    """Whether the standalone watchdog is running right now.

    Opening its singleton mutex answers the question the installed-file check
    cannot: the script being on disk, and a scheduled task existing, both stay
    true after the watchdog has exited or been killed. Only this tracks whether
    anything is actually holding the profile associations in place.

    SYNCHRONIZE is the least access that will open a mutex, and the handle is
    closed immediately, so this neither disturbs the watchdog nor keeps the
    object alive if it exits in between.
    """
    if not IS_WINDOWS:
        return False
    SYNCHRONIZE = 0x00100000
    try:
        kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, WATCHDOG_SINGLETON_MUTEX)
    except Exception:
        _log.debug("Could not probe the watchdog mutex", exc_info=True)
        return False
    if not handle:
        # Access denied means the mutex exists but belongs to a process this one may
        # not open -- a watchdog started by an elevated install. That is running, and
        # answering "not running" is what left the app reporting no watchdog while one
        # went on holding the associations.
        ERROR_ACCESS_DENIED = 5
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)
    except Exception:
        _log.debug("Could not close the watchdog mutex handle", exc_info=True)
    return True
