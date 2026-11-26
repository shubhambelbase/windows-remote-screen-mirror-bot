"""Shared Win32 Interactive Desktop Attachment Helper.

Attaches calling threads once per thread to WinSta0\\Default and closes
opened Win32 handles immediately to prevent USER object exhaustion.
"""

import ctypes
import logging
import os
import threading

logger = logging.getLogger(__name__)

_desktop_attached = threading.local()


def ensure_interactive_desktop() -> bool:
    """Attaches the calling thread to WinSta0 and Default desktop safely."""
    if os.name != "nt":
        return False
    if getattr(_desktop_attached, "attached", False):
        return True
    try:
        user32 = ctypes.windll.user32
        user32.OpenWindowStationW.restype = ctypes.c_void_p
        user32.OpenWindowStationW.argtypes = [ctypes.c_wchar_p, ctypes.c_bool, ctypes.c_uint32]
        user32.SetProcessWindowStation.restype = ctypes.c_bool
        user32.SetProcessWindowStation.argtypes = [ctypes.c_void_p]
        user32.CloseWindowStation.restype = ctypes.c_bool
        user32.CloseWindowStation.argtypes = [ctypes.c_void_p]

        user32.OpenDesktopW.restype = ctypes.c_void_p
        user32.OpenDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        user32.SetThreadDesktop.restype = ctypes.c_bool
        user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
        user32.CloseDesktop.restype = ctypes.c_bool
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]

        winsta_ok = False
        hw = user32.OpenWindowStationW("WinSta0", False, 0x37F)
        if hw:
            try:
                winsta_ok = bool(user32.SetProcessWindowStation(hw))
            finally:
                user32.CloseWindowStation(hw)

        desk_ok = False
        hd = user32.OpenDesktopW("Default", 0, False, 0x1FF)
        if hd:
            try:
                desk_ok = bool(user32.SetThreadDesktop(hd))
            finally:
                user32.CloseDesktop(hd)

        _desktop_attached.attached = bool(winsta_ok or desk_ok)
        return _desktop_attached.attached
    except Exception as e:
        logger.debug(f"Desktop switch exception: {e}")
        return False
