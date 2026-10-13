from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_NOREPEAT = 0x4000
VK_1 = 0x31
VK_2 = 0x32
VK_3 = 0x33
HOTKEY_OFF = 0x5641
HOTKEY_ON = 0x5642
HOTKEY_RESTORE = 0x5643
WM_TIMER = 0x0113


class GammaHotkeyListener(QObject):
    """Global Alt+1 / Alt+2 / Alt+3 hotkeys while the GUI is running.

    RegisterHotKey with ``hWnd = NULL`` posts WM_HOTKEY to the registering
    thread's message queue. A dedicated native message-loop thread is therefore
    used instead of relying on Qt's window-message filter. Signals are queued
    back to the GUI thread before any UI or profile state is changed.
    """

    disableRequested = Signal()
    enableRequested = Signal()
    restoreRequested = Signal()
    registrationChanged = Signal(bool, str)

    def __init__(self, on_disable: Callable[[], None], on_enable: Callable[[], None], on_restore: Callable[[], None] | None = None, on_registration: Callable[[bool, str], None] | None = None) -> None:
        super().__init__()
        self.disableRequested.connect(on_disable, Qt.ConnectionType.QueuedConnection)
        self.enableRequested.connect(on_enable, Qt.ConnectionType.QueuedConnection)
        if on_restore is not None:
            self.restoreRequested.connect(on_restore, Qt.ConnectionType.QueuedConnection)
        if on_registration is not None:
            self.registrationChanged.connect(on_registration, Qt.ConnectionType.QueuedConnection)
        self.registered = False
        self._thread_id = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        if os.name == "nt":
            self._thread = threading.Thread(
                target=self._message_loop,
                name="VirtualHDR-OSD-GammaHotkeys",
                daemon=True,
            )
            self._thread.start()
            self._ready.wait(timeout=1.5)

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        user32.KillTimer.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL

        self._thread_id = int(kernel32.GetCurrentThreadId())
        modifiers = MOD_ALT | MOD_NOREPEAT
        registrations = {HOTKEY_OFF: False, HOTKEY_ON: False, HOTKEY_RESTORE: False}
        keys = {HOTKEY_OFF: VK_1, HOTKEY_ON: VK_2, HOTKEY_RESTORE: VK_3}
        timer = user32.SetTimer(None, 0, 2000, None)
        msg = wintypes.MSG()
        previous_registration = None
        try:
            while not self._stop.is_set():
                for identifier, key in keys.items():
                    if not registrations[identifier]:
                        registrations[identifier] = bool(user32.RegisterHotKey(None, identifier, modifiers, key))
                self.registered = all(registrations.values())
                if self.registered != previous_registration:
                    previous_registration = self.registered
                    self.registrationChanged.emit(self.registered, "Alt+1 / Alt+2 / Alt+3 active." if self.registered else "Unavailable chords are retried; available hotkeys remain active.")
                self._ready.set()
                result = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if result <= 0:
                    break
                if int(msg.message) != WM_HOTKEY:
                    continue
                hotkey_id = int(msg.wParam)
                if hotkey_id == HOTKEY_OFF:
                    self.disableRequested.emit()
                elif hotkey_id == HOTKEY_ON:
                    self.enableRequested.emit()
                elif hotkey_id == HOTKEY_RESTORE:
                    self.restoreRequested.emit()
        finally:
            if timer:
                user32.KillTimer(None, timer)
            for identifier, registered in registrations.items():
                if registered:
                    user32.UnregisterHotKey(None, identifier)
            self.registered = False
            self._thread_id = 0

    def close(self) -> None:
        self._stop.set()
        if os.name == "nt" and self._thread_id:
            try:
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.registered = False
