"""Windows Input Controller.

Provides native low-latency mouse and keyboard input simulation using Windows user32 APIs.
Features sub-pixel movement accumulation, virtual/multi-monitor coordinate bounds,
asynchronous non-blocking helpers, active window detection, and system telemetry.
"""

import asyncio
import ctypes
from ctypes import wintypes
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from execution.win32_desktop import ensure_interactive_desktop

logger = logging.getLogger(__name__)

# Enable Per-Monitor DPI Awareness so screen metrics and mouse coordinates are 1:1 physical pixels
if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Windows Constants for Mouse Events
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000

# SendInput types
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x90, 0x91, 0xAD, 0xAE, 0xAF,
}

ULONG_PTR = ctypes.c_size_t


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


if os.name == "nt":
    ctypes.windll.user32.SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    ]
    ctypes.windll.user32.SendInput.restype = wintypes.UINT

VK_MAPPING = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "prtscn": 0x2C, "insert": 0x2D, "delete": 0x2E, "win": 0x5B,
    "windows": 0x5B, "meta": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "numlock": 0x90, "scrolllock": 0x91,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
}


class InputController:
    """Dispatches mouse and keyboard events directly into the Windows OS with high responsiveness."""

    def __init__(self):
        self.user32 = ctypes.windll.user32 if os.name == "nt" else None
        self._remainder_x = 0.0
        self._remainder_y = 0.0
        self._held_mouse_buttons: Set[str] = set()
        self._held_keys: Set[int] = set()

    def _send_mouse_input(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0):
        """Dispatches mouse event via SendInput with automatic fallback to mouse_event."""
        if not self.user32:
            return
        try:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.mi.dwFlags = flags
            inp.mi.dx = int(dx)
            inp.mi.dy = int(dy)
            inp.mi.mouseData = int(data)
            res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            if res == 0:
                self.user32.mouse_event(flags, int(dx), int(dy), int(data), 0)
        except Exception:
            self.user32.mouse_event(flags, int(dx), int(dy), int(data), 0)

    def _send_keyboard_input(self, vk: int = 0, scan: int = 0, flags: int = 0):
        """Dispatches keyboard event via SendInput with automatic fallback to keybd_event."""
        if not self.user32:
            return
        actual_flags = int(flags)
        if vk in EXTENDED_VKS:
            actual_flags |= KEYEVENTF_EXTENDEDKEY
        try:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki.wVk = int(vk)
            inp.ki.wScan = int(scan)
            inp.ki.dwFlags = actual_flags
            res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            if res == 0:
                self.user32.keybd_event(int(vk), int(scan), actual_flags, 0)
        except Exception:
            self.user32.keybd_event(int(vk), int(scan), actual_flags, 0)

    def get_screen_size(self) -> Tuple[int, int]:
        """Returns (width, height) of primary display."""
        ensure_interactive_desktop()
        if not self.user32:
            return 1920, 1080
        width = self.user32.GetSystemMetrics(0)
        height = self.user32.GetSystemMetrics(1)
        return width, height

    def get_virtual_screen_bounds(self) -> Tuple[int, int, int, int]:
        """Returns (left, top, width, height) of the entire virtual desktop across monitors."""
        ensure_interactive_desktop()
        if not self.user32:
            return 0, 0, 1920, 1080
        left = self.user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        top = self.user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
        width = self.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = self.user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            w, h = self.get_screen_size()
            return 0, 0, w, h
        return left, top, width, height

    def get_cursor_pos(self) -> Tuple[int, int]:
        """Returns current (x, y) cursor position."""
        ensure_interactive_desktop()
        if not self.user32:
            return 0, 0
        pt = POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def mouse_move_relative(self, dx: float, dy: float, sensitivity: float = 1.0):
        """Moves cursor smoothly across virtual screen bounds with sub-pixel accumulation."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        self._remainder_x += dx * sensitivity
        self._remainder_y += dy * sensitivity

        move_x = int(self._remainder_x)
        move_y = int(self._remainder_y)

        self._remainder_x -= move_x
        self._remainder_y -= move_y

        if move_x != 0 or move_y != 0:
            cx, cy = self.get_cursor_pos()
            vleft, vtop, vw, vh = self.get_virtual_screen_bounds()
            nx = max(vleft, min(vleft + vw - 1, cx + move_x))
            ny = max(vtop, min(vtop + vh - 1, cy + move_y))
            self.user32.SetCursorPos(nx, ny)

    def mouse_move_absolute(self, x: int, y: int):
        """Moves cursor to exact virtual screen pixel coordinates clamped to display bounds."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        vleft, vtop, vw, vh = self.get_virtual_screen_bounds()
        cx = max(vleft, min(vleft + vw - 1, int(x)))
        cy = max(vtop, min(vtop + vh - 1, int(y)))
        self.user32.SetCursorPos(cx, cy)

    def mouse_down(self, button: str = "left"):
        """Presses mouse button down (supports drag lock)."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        btn = button.lower()
        self._held_mouse_buttons.add(btn)
        if btn == "left":
            self._send_mouse_input(MOUSEEVENTF_LEFTDOWN)
        elif btn == "right":
            self._send_mouse_input(MOUSEEVENTF_RIGHTDOWN)
        elif btn == "middle":
            self._send_mouse_input(MOUSEEVENTF_MIDDLEDOWN)

    def mouse_up(self, button: str = "left"):
        """Releases mouse button."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        btn = button.lower()
        self._held_mouse_buttons.discard(btn)
        if btn == "left":
            self._send_mouse_input(MOUSEEVENTF_LEFTUP)
        elif btn == "right":
            self._send_mouse_input(MOUSEEVENTF_RIGHTUP)
        elif btn == "middle":
            self._send_mouse_input(MOUSEEVENTF_MIDDLEUP)

    def release_all_inputs(self):
        """Safety reset: releases all held mouse buttons and keys on reconnect/disconnect."""
        for btn in list(self._held_mouse_buttons):
            self.mouse_up(btn)
        for vk in list(self._held_keys):
            if self.user32:
                self._send_keyboard_input(vk=vk, flags=KEYEVENTF_KEYUP)
        self._held_keys.clear()

    def mouse_click(self, button: str = "left"):
        """Synchronous click with minimal hardware debounce."""
        self.mouse_down(button)
        time.sleep(0.005)
        self.mouse_up(button)

    async def async_mouse_click(self, button: str = "left"):
        """Non-blocking async click."""
        self.mouse_down(button)
        await asyncio.sleep(0.008)
        self.mouse_up(button)

    def mouse_double_click(self, button: str = "left"):
        """Synchronous double click."""
        self.mouse_click(button)
        time.sleep(0.04)
        self.mouse_click(button)

    async def async_mouse_double_click(self, button: str = "left"):
        """Non-blocking async double click."""
        await self.async_mouse_click(button)
        await asyncio.sleep(0.04)
        await self.async_mouse_click(button)

    def mouse_wheel(self, delta_y: int = 0, delta_x: int = 0):
        """Scrolls vertically (delta_y) or horizontally (delta_x)."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        if delta_y != 0:
            self._send_mouse_input(MOUSEEVENTF_WHEEL, data=int(delta_y))
        if delta_x != 0:
            self._send_mouse_input(MOUSEEVENTF_HWHEEL, data=int(delta_x))

    def _resolve_vk(self, key_name: str) -> Optional[int]:
        """Resolves a key name or single character to a Windows Virtual Key code."""
        key_lower = key_name.lower().strip()
        if key_lower in VK_MAPPING:
            return VK_MAPPING[key_lower]
        if len(key_name) == 1 and self.user32:
            ch = key_name.upper()
            vk = self.user32.VkKeyScanW(ord(ch))
            if vk != -1:
                return vk & 0xFF
        return None

    def key_down(self, key_name: str):
        """Simulates key down event."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        vk = self._resolve_vk(key_name)
        if vk is not None:
            self._held_keys.add(vk)
            self._send_keyboard_input(vk=vk, flags=0)
        else:
            for char in key_name:
                self._send_char_event(char, key_up=False)

    def key_up(self, key_name: str):
        """Simulates key up event."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        vk = self._resolve_vk(key_name)
        if vk is not None:
            self._held_keys.discard(vk)
            self._send_keyboard_input(vk=vk, flags=KEYEVENTF_KEYUP)
        else:
            for char in key_name:
                self._send_char_event(char, key_up=True)

    def key_press(self, key_name: str):
        """Simulates a quick key press (down + up)."""
        self.key_down(key_name)
        time.sleep(0.005)
        self.key_up(key_name)

    async def async_key_press(self, key_name: str):
        """Non-blocking async key press."""
        self.key_down(key_name)
        await asyncio.sleep(0.005)
        self.key_up(key_name)

    def hotkey(self, keys: List[str]):
        """Executes a key combo synchronously."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        resolved = []
        for k in keys:
            vk = self._resolve_vk(k)
            if vk is not None:
                resolved.append(vk)

        for vk in resolved:
            self._send_keyboard_input(vk=vk, flags=0)
        time.sleep(0.008)
        for vk in reversed(resolved):
            self._send_keyboard_input(vk=vk, flags=KEYEVENTF_KEYUP)

    async def async_hotkey(self, keys: List[str]):
        """Non-blocking async hotkey execution."""
        ensure_interactive_desktop()
        if not self.user32:
            return
        resolved = []
        for k in keys:
            vk = self._resolve_vk(k)
            if vk is not None:
                resolved.append(vk)

        for vk in resolved:
            self._send_keyboard_input(vk=vk, flags=0)
        await asyncio.sleep(0.01)
        for vk in reversed(resolved):
            self._send_keyboard_input(vk=vk, flags=KEYEVENTF_KEYUP)

    def _send_char_event(self, char: str, key_up: bool = False):
        """Sends unicode character using user32 SendInput or keybd_event."""
        if not self.user32:
            return
        char_code = ord(char)
        flags = KEYEVENTF_UNICODE
        if key_up:
            flags |= KEYEVENTF_KEYUP
        self._send_keyboard_input(vk=0, scan=char_code, flags=flags)

    def type_text(self, text: str):
        """Types unicode string into the active window."""
        ensure_interactive_desktop()
        for char in text:
            if char == "\n":
                self.key_press("enter")
            elif char == "\t":
                self.key_press("tab")
            else:
                self._send_char_event(char, key_up=False)
                time.sleep(0.001)
                self._send_char_event(char, key_up=True)
                time.sleep(0.001)

    async def async_type_text(self, text: str):
        """Non-blocking async text typing."""
        ensure_interactive_desktop()
        for char in text:
            if char == "\n":
                await self.async_key_press("enter")
            elif char == "\t":
                await self.async_key_press("tab")
            else:
                self._send_char_event(char, key_up=False)
                await asyncio.sleep(0.001)
                self._send_char_event(char, key_up=True)
                await asyncio.sleep(0.001)

    def get_active_window_title(self) -> str:
        """Retrieves the title text of the currently focused foreground window."""
        ensure_interactive_desktop()
        if not self.user32:
            return "Windows Desktop"
        try:
            hwnd = self.user32.GetForegroundWindow()
            if not hwnd:
                return "Desktop"
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return "Desktop"
            buff = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value or "Desktop"
        except Exception:
            return "Windows Desktop"

    def get_battery_status(self) -> Dict[str, Any]:
        """Queries battery charge percentage and AC power status."""
        try:
            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                pct = int(status.BatteryLifePercent)
                charging = status.ACLineStatus == 1
                return {
                    "percent": pct if pct <= 100 else 100,
                    "charging": charging,
                    "status": "Charging" if charging else "Discharging",
                }
        except Exception:
            pass
        return {"percent": 100, "charging": True, "status": "AC Power"}

    def set_taskbar_visible(self, visible: bool = True) -> bool:
        """Hides (visible=False) or shows (visible=True) the Windows taskbar."""
        ensure_interactive_desktop()
        if not self.user32:
            return False
        try:
            sw_cmd = 5 if visible else 0
            h_tray = self.user32.FindWindowW("Shell_TrayWnd", None)
            if h_tray:
                self.user32.ShowWindow(h_tray, sw_cmd)

            def enum_cb(hwnd, lparam):
                class_buff = ctypes.create_unicode_buffer(256)
                self.user32.GetClassNameW(hwnd, class_buff, 256)
                if class_buff.value == "Shell_SecondaryTrayWnd":
                    self.user32.ShowWindow(hwnd, sw_cmd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            self.user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
            return True
        except Exception as e:
            logger.debug(f"Taskbar toggle error: {e}")
            return False

    def is_taskbar_visible(self) -> bool:
        """Checks if the Windows primary taskbar is currently visible."""
        ensure_interactive_desktop()
        if not self.user32:
            return True
        try:
            h_tray = self.user32.FindWindowW("Shell_TrayWnd", None)
            if h_tray:
                return bool(self.user32.IsWindowVisible(h_tray))
            return True
        except Exception:
            return True
