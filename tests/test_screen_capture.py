"""Unit tests for ScreenCaptureEngine"""

import pytest
import io
from PIL import Image
from execution.screen_capture import ScreenCaptureEngine


def test_screen_capture_engine_initialization():
    engine = ScreenCaptureEngine()
    monitors = engine.get_monitors()
    assert len(monitors) > 0
    assert "width" in monitors[0]
    assert "height" in monitors[0]
    engine.close()


def test_capture_frame():
    engine = ScreenCaptureEngine()
    frame_bytes, orig_w, orig_h = engine.capture_frame(
        monitor_index=1,
        quality=50,
        scale=0.5,
        format_type="JPEG",
    )
    assert len(frame_bytes) > 0
    assert orig_w > 0
    assert orig_h > 0

    # Verify bytes represent a valid JPEG image
    img = Image.open(io.BytesIO(frame_bytes))
    assert img.format == "JPEG"
    assert img.width == orig_w // 2 or abs(img.width - orig_w * 0.5) <= 2
    engine.close()


def test_capture_screenshot():
    engine = ScreenCaptureEngine()
    screenshot_bytes = engine.capture_screenshot(monitor_index=1)
    assert len(screenshot_bytes) > 0
    img = Image.open(io.BytesIO(screenshot_bytes))
    assert img.format == "JPEG"
    assert img.width > 0
    assert img.height > 0
    engine.close()


def test_cross_thread_capture_and_broadcaster_no_zombie_threads():
    import threading
    import time
    from execution.screen_capture import ScreenCaptureBroadcaster, SingleSlotFrameBuffer

    engine = ScreenCaptureEngine()
    errors = []

    def worker():
        try:
            data, w, h = engine.capture_frame(monitor_index=1, quality=40, scale=0.3)
            assert len(data) > 0 and w > 0 and h > 0
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5.0)
    assert errors == []

    broadcaster = ScreenCaptureBroadcaster(engine)
    buf1 = SingleSlotFrameBuffer()
    buf2 = SingleSlotFrameBuffer()

    # Rapid subscribe / unsubscribe cycles must not leave duplicate live capture threads
    broadcaster.add_subscriber(buf1)
    first_thread = broadcaster._thread
    broadcaster.remove_subscriber(buf1)
    broadcaster.add_subscriber(buf2)
    time.sleep(0.15)
    broadcaster.remove_subscriber(buf2)
    broadcaster.stop()

    assert not first_thread.is_alive()
    assert not broadcaster._thread.is_alive()
    engine.close()


def test_capture_frame_if_changed_skips_idle_and_emits_on_change(monkeypatch):
    import time

    class DummySctImg:
        def __init__(self, size, bgra):
            self.size = size
            self.bgra = bgra

    class DummySct:
        monitors = [
            {"left": 0, "top": 0, "width": 64, "height": 64},
            {"left": 0, "top": 0, "width": 64, "height": 64},
        ]

        def __init__(self):
            self.img = DummySctImg((64, 64), b"\x10\x20\x30\xff" * (64 * 64))

        def grab(self, monitor):
            return self.img

    dummy_sct = DummySct()
    cursor_state = {"pos": (10, 10)}

    engine = ScreenCaptureEngine()
    monkeypatch.setattr(engine, "_get_sct", lambda: dummy_sct)
    monkeypatch.setattr(engine, "_get_cursor_pos", lambda m, w, h: cursor_state["pos"])

    # Initial frame always encodes
    res1, sig1 = engine.capture_frame_if_changed(last_sig=None, last_emit_ts=0.0, quality=65, scale=0.85)
    assert res1 is not None and len(res1[0]) > 0

    # Identical pixels + cursor within keepalive window must skip encoding
    now_m = time.monotonic()
    res_skip, sig_skip = engine.capture_frame_if_changed(
        last_sig=sig1, last_emit_ts=now_m, max_idle_s=1.5, quality=65, scale=0.85
    )
    assert res_skip is None
    assert sig_skip == sig1

    # Expired keepalive window forces frame emission
    res_keepalive, _ = engine.capture_frame_if_changed(
        last_sig=sig1, last_emit_ts=now_m - 2.0, max_idle_s=1.5, quality=65, scale=0.85
    )
    assert res_keepalive is not None

    # Cursor movement immediately invalidates signature and emits frame
    cursor_state["pos"] = (25, 30)
    res_cursor, sig_cursor = engine.capture_frame_if_changed(
        last_sig=sig1, last_emit_ts=time.monotonic(), max_idle_s=1.5, quality=65, scale=0.85
    )
    assert res_cursor is not None
    assert sig_cursor != sig1

    # Screen pixel change immediately emits frame
    dummy_sct.img = DummySctImg((64, 64), b"\xff\x80\x40\xff" * (64 * 64))
    res_pixel, sig_pixel = engine.capture_frame_if_changed(
        last_sig=sig_cursor, last_emit_ts=time.monotonic(), max_idle_s=1.5, quality=65, scale=0.85
    )
    assert res_pixel is not None
    assert sig_pixel != sig_cursor
    engine.close()
