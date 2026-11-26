"""Unit tests for InputController"""

import pytest
import asyncio
from execution.input_controller import InputController, VK_MAPPING, ensure_interactive_desktop


def test_input_controller_screen_metrics():
    ctrl = InputController()
    w, h = ctrl.get_screen_size()
    assert w > 0
    assert h > 0


def test_input_controller_cursor_position():
    ctrl = InputController()
    x, y = ctrl.get_cursor_pos()
    w, h = ctrl.get_screen_size()
    # Cursor should generally be within desktop bounds
    assert isinstance(x, int)
    assert isinstance(y, int)


def test_virtual_key_resolution():
    ctrl = InputController()
    assert ctrl._resolve_vk("enter") == 0x0D
    assert ctrl._resolve_vk("backspace") == 0x08
    assert ctrl._resolve_vk("win") == 0x5B
    assert ctrl._resolve_vk("ctrl") == 0x11
    assert ctrl._resolve_vk("alt") == 0x12
    assert ctrl._resolve_vk("tab") == 0x09
    assert ctrl._resolve_vk("esc") == 0x1B

    # Single character resolution
    assert ctrl._resolve_vk("a") is not None
    assert ctrl._resolve_vk("Z") is not None


def test_ensure_interactive_desktop_repeated_calls():
    """Ensure repeatedly invoking desktop attachment does not leak handles or throw."""
    for _ in range(50):
        ensure_interactive_desktop()


def test_mouse_movement_and_release():
    ctrl = InputController()
    # Relative move
    ctrl.mouse_move_relative(5.0, 5.0, sensitivity=1.0)
    # Absolute move
    ctrl.mouse_move_absolute(100, 100)
    # Mouse buttons
    ctrl.mouse_down("left")
    assert "left" in ctrl._held_mouse_buttons
    ctrl.mouse_up("left")
    assert "left" not in ctrl._held_mouse_buttons
    # Wheel
    ctrl.mouse_wheel(delta_y=120)
    # Release safety
    ctrl.mouse_down("left")
    ctrl.release_all_inputs()
    assert len(ctrl._held_mouse_buttons) == 0


def test_keyboard_and_async_methods(monkeypatch):
    ctrl = InputController()
    sent_keys = []
    # Spy/stub Win32 key injection so pytest never sends real Esc/Ctrl+C to the active terminal
    monkeypatch.setattr(
        ctrl,
        "_send_keyboard_input",
        lambda vk=0, scan=0, flags=0: sent_keys.append((vk, flags)) or True,
    )
    monkeypatch.setattr(ctrl, "_send_mouse_input", lambda flags, dx=0, dy=0, data=0: True)

    ctrl.key_down("shift")
    assert ctrl._held_keys
    ctrl.key_up("shift")
    assert len(ctrl._held_keys) == 0

    asyncio.run(ctrl.async_mouse_click("left"))
    asyncio.run(ctrl.async_key_press("esc"))
    asyncio.run(ctrl.async_hotkey(["ctrl", "c"]))

    assert (0x1B, 0) in sent_keys
    assert any(vk == 0x1B and flags != 0 for vk, flags in sent_keys)
    assert (0x11, 0) in sent_keys
    assert any(vk == 0x11 and flags != 0 for vk, flags in sent_keys)

