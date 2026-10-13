"""Present HDR content to a display, and read what that display can actually do.

Qt cannot open an HDR surface. PySide6 6.7 offers only ``DefaultColorSpace`` and
``sRGBColorSpace`` in ``QSurfaceFormat.ColorSpace``, so anything Qt draws is composited
as SDR and cannot exceed the SDR reference white level. Calibration patterns have to
address absolute luminance *above* diffuse white, so this module drives D3D11 and DXGI
directly through ctypes. No compiler, no numpy and no shaders are involved: the patterns
are flat fields, so they are built on the CPU and blitted with ``CopyResource``.

Two swapchain formats can carry HDR:

* ``R10G10B10A2_UNORM`` with the HDR10/PQ colour space is cheaper, but Microsoft
  documents it as available only when the target really is an HDR display.
* ``R16G16B16A16_FLOAT`` with scRGB is documented as the only option that works on every
  display kind, including SDR and WCG panels, where the desktop window manager simply
  clips what it cannot show.

A tool shipped to other people has to take the second. Note that scRGB is the *implicit*
colour space for float formats: ``CheckColorSpaceSupport`` reports it unsupported and
``SetColorSpace1`` returns ``E_INVALIDARG``. Neither is an error, and neither call is made.

In scRGB, 1.0 is D65 at 80 nits on an HDR display, so a luminance in nits is simply
``nits / 80``. On an SDR display 1.0 means that display's own reference white instead,
which is why :attr:`DisplayCapability.is_hdr` must be consulted before a pattern claims
to be showing absolute nits.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import POINTER, byref, c_long, c_ubyte, c_uint, c_void_p, wintypes
from dataclasses import dataclass

from vhdr_color.gamma_correction import SCRGB_WHITE_NITS

IS_WINDOWS = hasattr(ctypes, "WinDLL")

# ST.2084 is defined to 10,000 nits, and that is the ceiling the ICC luminance fields and
# every pattern here work against. Nothing is clamped to one panel's range.
PQ_MAX_NITS = 10000.0

DXGI_FORMAT_R16G16B16A16_FLOAT = 10
DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709 = 0      # SDR, and WCG too: DXGI cannot separate them
DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12  # HDR10


class HdrDisplayError(RuntimeError):
    """Raised when HDR presentation is unavailable, so callers can fall back to SDR."""


@dataclass(frozen=True)
class DisplayCapability:
    """What ``IDXGIOutput6::GetDesc1`` reports about one output.

    The luminance fields come from the panel's own metadata, which is frequently absent
    or plainly wrong on cheaper displays. :attr:`luminance_is_credible` exists so callers
    can mark a reading as untrustworthy rather than silently calibrating against it.
    """

    device_name: str
    left: int
    top: int
    right: int
    bottom: int
    bits_per_color: int
    color_space: int
    min_nits: float
    max_nits: float
    max_full_frame_nits: float
    red_primary: tuple[float, float]
    green_primary: tuple[float, float]
    blue_primary: tuple[float, float]
    white_point: tuple[float, float]

    @property
    def is_hdr(self) -> bool:
        """True when the output is in HDR10 mode.

        DXGI reports a WCG display with auto colour management identically to a plain SDR
        one, so False here means "not HDR", never "not advanced colour".
        """
        return self.color_space == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020

    @property
    def luminance_is_credible(self) -> bool:
        """Whether the reported luminance describes the panel's HDR colour volume.

        DXGI reports the colour volume of the mode the output is *currently in*, not a
        fixed property of the panel. With HDR off, one display here reports 240/240 nits
        and BT.709 primaries; with HDR on, the same display reports 1080/1080 and DCI-P3.
        240 is the SDR reference white, and it is perfectly plausible as a number, so a
        range check alone would accept it and a calibration step would then target the
        wrong peak entirely. HDR mode is therefore part of the test, not a separate one.

        Beyond that, a display reporting zero or an absurd peak has no usable metadata,
        and the calibration steps have to be driven by eye or by a meter instead.
        """
        if not self.is_hdr:
            return False
        return 40.0 <= self.max_nits <= PQ_MAX_NITS and self.max_full_frame_nits > 0.0


def nits_to_scrgb(nits: float) -> float:
    """Absolute luminance to an scRGB channel value, for HDR outputs."""
    return max(0.0, float(nits)) / SCRGB_WHITE_NITS


def scrgb_pixel(red: float, green: float, blue: float) -> bytes:
    """One scRGB pixel as four half floats, the layout R16G16B16A16_FLOAT expects."""
    return struct.pack("<4e", red, green, blue, 1.0)


# ------------------------------------------------------------------------------------
# COM plumbing. None of these interfaces ship a type library, so the vtables are declared
# by hand. IDXGIObject contributes four methods after IUnknown's three, and that offset is
# what trips up every hand-written binding, so the slot numbers are named rather than
# inlined.


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", c_ubyte * 8)]

    def __init__(self, text: str) -> None:
        super().__init__()
        parts = text.strip("{}").split("-")
        self.Data1 = int(parts[0], 16)
        self.Data2 = int(parts[1], 16)
        self.Data3 = int(parts[2], 16)
        for index, value in enumerate(bytes.fromhex(parts[3] + parts[4])):
            self.Data4[index] = value


_IID_IDXGIFactory1 = _GUID("770aae78-f26f-4dba-a829-253c83d1b387")
_IID_IDXGIFactory2 = _GUID("50c83a1c-e072-4c48-87b0-3630fa36a6d0")
_IID_IDXGIDevice = _GUID("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
_IID_IDXGIAdapter = _GUID("2411e7e1-12ac-4ccf-bd14-9798e8534dc0")
_IID_IDXGIOutput6 = _GUID("068346e8-aaec-4b84-add7-137f513f77a1")
_IID_ID3D11Texture2D = _GUID("6f15aaf2-d208-4e89-9ab4-489535d34f9c")

_QUERY_INTERFACE, _RELEASE, _GET_PARENT = 0, 2, 6
_ENUM_OUTPUTS = 7                 # IDXGIAdapter
_ENUM_ADAPTERS1 = 12              # IDXGIFactory1
_CREATE_SWAPCHAIN_FOR_HWND = 15   # IDXGIFactory2
_GET_DESC1 = 27                   # IDXGIOutput6
_CREATE_TEXTURE2D = 5             # ID3D11Device
_COPY_RESOURCE = 47               # ID3D11DeviceContext
_PRESENT, _GET_BUFFER, _RESIZE_BUFFERS = 8, 9, 13   # IDXGISwapChain
_MAKE_WINDOW_ASSOCIATION = 8      # IDXGIFactory
_DXGI_MWA_NO_WINDOW_CHANGES = 0x1
_DXGI_MWA_NO_ALT_ENTER = 0x2
# Present failures meaning the device is gone, so every later call fails the same way.
_DEVICE_LOST = {0x887A0005: "removed", 0x887A0006: "hung", 0x887A0007: "reset"}

_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D11_SDK_VERSION = 7
_DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
_DXGI_SWAP_EFFECT_FLIP_DISCARD = 4


def _vcall(pointer, index: int, argtypes, *args) -> int:
    table = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(table[index])(pointer, *args)


def _release(pointer) -> None:
    if pointer is not None and getattr(pointer, "value", None):
        table = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(table[_RELEASE])(pointer)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("top", ctypes.c_int),
                ("right", ctypes.c_int), ("bottom", ctypes.c_int)]


class _DXGI_OUTPUT_DESC1(ctypes.Structure):
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", _RECT),
        ("AttachedToDesktop", wintypes.BOOL), ("Rotation", c_uint), ("Monitor", c_void_p),
        ("BitsPerColor", c_uint), ("ColorSpace", c_uint),
        ("RedPrimary", ctypes.c_float * 2), ("GreenPrimary", ctypes.c_float * 2),
        ("BluePrimary", ctypes.c_float * 2), ("WhitePoint", ctypes.c_float * 2),
        ("MinLuminance", ctypes.c_float), ("MaxLuminance", ctypes.c_float),
        ("MaxFullFrameLuminance", ctypes.c_float),
    ]


class _DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class _DXGI_SWAP_CHAIN_DESC1(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("Format", c_uint),
                ("Stereo", wintypes.BOOL), ("SampleDesc", _DXGI_SAMPLE_DESC),
                ("BufferUsage", c_uint), ("BufferCount", c_uint), ("Scaling", c_uint),
                ("SwapEffect", c_uint), ("AlphaMode", c_uint), ("Flags", c_uint)]


class _D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint),
                ("ArraySize", c_uint), ("Format", c_uint), ("SampleDesc", _DXGI_SAMPLE_DESC),
                ("Usage", c_uint), ("BindFlags", c_uint), ("CPUAccessFlags", c_uint),
                ("MiscFlags", c_uint)]


class _D3D11_SUBRESOURCE_DATA(ctypes.Structure):
    _fields_ = [("pSysMem", c_void_p), ("SysMemPitch", c_uint), ("SysMemSlicePitch", c_uint)]


def _to_pair(value) -> tuple[float, float]:
    return (round(float(value[0]), 6), round(float(value[1]), 6))


def enumerate_display_capabilities() -> list[DisplayCapability]:
    """Read every attached output's advanced-colour capability.

    Returns an empty list rather than raising when DXGI is unavailable, because the
    caller's fallback is always "carry on without HDR knowledge".
    """
    if not IS_WINDOWS:
        return []
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError:
        return []

    results: list[DisplayCapability] = []
    factory = c_void_p()
    if dxgi.CreateDXGIFactory1(byref(_IID_IDXGIFactory1), byref(factory)) < 0:
        return []
    try:
        adapter_index = 0
        while True:
            adapter = c_void_p()
            if _vcall(factory, _ENUM_ADAPTERS1, [c_uint, POINTER(c_void_p)],
                      adapter_index, byref(adapter)) < 0:
                break
            adapter_index += 1
            try:
                output_index = 0
                while True:
                    output = c_void_p()
                    if _vcall(adapter, _ENUM_OUTPUTS, [c_uint, POINTER(c_void_p)],
                              output_index, byref(output)) < 0:
                        break
                    output_index += 1
                    try:
                        output6 = c_void_p()
                        if _vcall(output, _QUERY_INTERFACE, [POINTER(_GUID), POINTER(c_void_p)],
                                  byref(_IID_IDXGIOutput6), byref(output6)) < 0:
                            continue
                        try:
                            desc = _DXGI_OUTPUT_DESC1()
                            if _vcall(output6, _GET_DESC1, [POINTER(_DXGI_OUTPUT_DESC1)],
                                      byref(desc)) < 0:
                                continue
                            box = desc.DesktopCoordinates
                            results.append(DisplayCapability(
                                device_name=desc.DeviceName,
                                left=box.left, top=box.top, right=box.right, bottom=box.bottom,
                                bits_per_color=int(desc.BitsPerColor),
                                color_space=int(desc.ColorSpace),
                                min_nits=float(desc.MinLuminance),
                                max_nits=float(desc.MaxLuminance),
                                max_full_frame_nits=float(desc.MaxFullFrameLuminance),
                                red_primary=_to_pair(desc.RedPrimary),
                                green_primary=_to_pair(desc.GreenPrimary),
                                blue_primary=_to_pair(desc.BluePrimary),
                                white_point=_to_pair(desc.WhitePoint),
                            ))
                        finally:
                            _release(output6)
                    finally:
                        _release(output)
            finally:
                _release(adapter)
    finally:
        _release(factory)
    return results


def capability_for_device_name(device_name: str) -> DisplayCapability | None:
    """Match a DXGI output to a GDI device name such as ``\\\\.\\DISPLAY1``."""
    for capability in enumerate_display_capabilities():
        if capability.device_name == device_name:
            return capability
    return None


class HdrSurface:
    """An FP16/scRGB swapchain bound to an existing window handle.

    The window is supplied by the caller, which lets a Qt widget host this: set
    ``WA_PaintOnScreen`` and ``WA_NativeWindow`` and return ``None`` from ``paintEngine``,
    and Qt keeps focus, keyboard and fullscreen handling while D3D owns the pixels.
    """

    def __init__(self, hwnd: int, width: int, height: int) -> None:
        if not IS_WINDOWS:
            raise HdrDisplayError("HDR presentation requires Windows")
        self._device = c_void_p()
        self._context = c_void_p()
        self._swapchain = c_void_p()
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self._hwnd = int(hwnd)
        self._create()

    # -- lifecycle -------------------------------------------------------------------

    def _create(self) -> None:
        try:
            d3d11 = ctypes.WinDLL("d3d11")
        except OSError as exc:
            raise HdrDisplayError(f"Direct3D 11 is unavailable: {exc}") from exc

        result = d3d11.D3D11CreateDevice(
            None, _D3D_DRIVER_TYPE_HARDWARE, None, 0, None, 0, _D3D11_SDK_VERSION,
            byref(self._device), None, byref(self._context),
        )
        if result < 0:
            raise HdrDisplayError(f"D3D11CreateDevice failed (0x{result & 0xFFFFFFFF:08X})")

        dxgi_device, adapter, factory = c_void_p(), c_void_p(), c_void_p()
        created_swapchain = False
        try:
            if _vcall(self._device, _QUERY_INTERFACE, [POINTER(_GUID), POINTER(c_void_p)],
                      byref(_IID_IDXGIDevice), byref(dxgi_device)) < 0:
                raise HdrDisplayError("The D3D11 device does not expose IDXGIDevice")
            if _vcall(dxgi_device, _GET_PARENT, [POINTER(_GUID), POINTER(c_void_p)],
                      byref(_IID_IDXGIAdapter), byref(adapter)) < 0:
                raise HdrDisplayError("Could not reach the DXGI adapter")
            if _vcall(adapter, _GET_PARENT, [POINTER(_GUID), POINTER(c_void_p)],
                      byref(_IID_IDXGIFactory2), byref(factory)) < 0:
                raise HdrDisplayError("Could not reach IDXGIFactory2")

            # Flip model is mandatory: it is what makes the swapchain eligible for the
            # desktop window manager's advanced-colour path.
            desc = _DXGI_SWAP_CHAIN_DESC1(
                self._width, self._height, DXGI_FORMAT_R16G16B16A16_FLOAT, 0,
                _DXGI_SAMPLE_DESC(1, 0), _DXGI_USAGE_RENDER_TARGET_OUTPUT, 2, 0,
                _DXGI_SWAP_EFFECT_FLIP_DISCARD, 0, 0,
            )
            result = _vcall(
                factory, _CREATE_SWAPCHAIN_FOR_HWND,
                [c_void_p, c_void_p, POINTER(_DXGI_SWAP_CHAIN_DESC1), c_void_p, c_void_p,
                 POINTER(c_void_p)],
                self._device, c_void_p(self._hwnd), byref(desc), None, None,
                byref(self._swapchain),
            )
            if result < 0:
                raise HdrDisplayError(
                    f"CreateSwapChainForHwnd failed (0x{result & 0xFFFFFFFF:08X})"
                )
            created_swapchain = True
            # Alt+Enter would otherwise have DXGI take this window fullscreen by itself: a
            # display mode change in the middle of a patch, under the meter. Qt already
            # owns the window's fullscreen state, so DXGI is told to stay out of its
            # messages altogether. Best effort -- the surface works without it, only
            # unguarded, and failing a measurement over that would be backwards.
            try:
                _vcall(factory, _MAKE_WINDOW_ASSOCIATION, [c_void_p, c_uint],
                       c_void_p(self._hwnd),
                       _DXGI_MWA_NO_WINDOW_CHANGES | _DXGI_MWA_NO_ALT_ENTER)
            except Exception:  # noqa: BLE001
                pass
        finally:
            _release(factory)
            _release(adapter)
            _release(dxgi_device)
            if not created_swapchain:
                # The device and its context were released nowhere. An exception out
                # of __init__ means the object is never bound, so close() can never be
                # called on it and there is no __del__ -- one ID3D11Device plus its
                # context orphaned per failed attempt, for the life of the process.
                # No swapchain leaks here: it is still null on this path.
                _release(self._context)
                _release(self._device)
                self._context = c_void_p()
                self._device = c_void_p()

    def close(self) -> None:
        _release(self._swapchain)
        _release(self._context)
        _release(self._device)
        self._swapchain = c_void_p()
        self._context = c_void_p()
        self._device = c_void_p()

    def __enter__(self) -> "HdrSurface":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- presentation ----------------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def stride(self) -> int:
        """Bytes per row: four half floats per pixel."""
        return self._width * 8

    def resize(self, width: int, height: int) -> None:
        width, height = max(1, int(width)), max(1, int(height))
        if (width, height) == (self._width, self._height):
            return
        result = _vcall(self._swapchain, _RESIZE_BUFFERS,
                        [c_uint, c_uint, c_uint, c_uint, c_uint], 0, width, height, 0, 0)
        if result < 0:
            raise HdrDisplayError(f"ResizeBuffers failed (0x{result & 0xFFFFFFFF:08X})")
        self._width, self._height = width, height

    def present(self, pixels: bytes, *, vsync: bool = True) -> None:
        """Blit a full frame of scRGB half floats and present it.

        Raises when Present fails. Its answer used to be discarded, so a removed or reset
        device was noticed only if a later call happened to fail -- and a patch that never
        reached the screen was measured as though it had. What this cannot catch is the
        window being covered: Microsoft documents that a flip-model swapchain never
        returns DXGI_STATUS_OCCLUDED, so there is no answer to check.

        A staging texture is created per frame rather than kept alive, because patterns
        change only on user input; the cost is irrelevant next to the clarity of not
        having to invalidate a cache when the size or the pattern changes.
        """
        expected = self.stride * self._height
        if len(pixels) != expected:
            raise HdrDisplayError(
                f"frame is {len(pixels)} bytes, expected {expected} for {self._width}x{self._height}"
            )

        desc = _D3D11_TEXTURE2D_DESC(
            self._width, self._height, 1, 1, DXGI_FORMAT_R16G16B16A16_FLOAT,
            _DXGI_SAMPLE_DESC(1, 0), 0, 0, 0, 0,
        )
        buffer = (ctypes.c_char * len(pixels)).from_buffer_copy(pixels)
        initial = _D3D11_SUBRESOURCE_DATA(ctypes.cast(buffer, c_void_p), self.stride, 0)
        texture = c_void_p()
        result = _vcall(
            self._device, _CREATE_TEXTURE2D,
            [POINTER(_D3D11_TEXTURE2D_DESC), POINTER(_D3D11_SUBRESOURCE_DATA), POINTER(c_void_p)],
            byref(desc), byref(initial), byref(texture),
        )
        if result < 0:
            raise HdrDisplayError(f"CreateTexture2D failed (0x{result & 0xFFFFFFFF:08X})")

        try:
            back = c_void_p()
            result = _vcall(self._swapchain, _GET_BUFFER,
                            [c_uint, POINTER(_GUID), POINTER(c_void_p)],
                            0, byref(_IID_ID3D11Texture2D), byref(back))
            if result < 0:
                raise HdrDisplayError(f"GetBuffer failed (0x{result & 0xFFFFFFFF:08X})")
            try:
                _vcall(self._context, _COPY_RESOURCE, [c_void_p, c_void_p], back, texture)
            finally:
                _release(back)
            result = _vcall(self._swapchain, _PRESENT, [c_uint, c_uint], 1 if vsync else 0, 0)
        finally:
            _release(texture)
        if result < 0:
            code = result & 0xFFFFFFFF
            lost = _DEVICE_LOST.get(code)
            if lost is not None:
                raise HdrDisplayError(
                    f"The graphics device was {lost} (0x{code:08X}), so nothing more can be "
                    "shown in this window. Close it and open it again."
                )
            raise HdrDisplayError(f"Present failed (0x{code:08X})")
