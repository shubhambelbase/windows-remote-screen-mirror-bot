"""Tests for Single-Slot Latest-Frame Buffer and Binary Frame Packing"""

import time
import struct
import pytest
from execution.screen_capture import SingleSlotFrameBuffer, BroadcastFrame


def test_single_slot_buffer_overwrite():
    buf = SingleSlotFrameBuffer()

    f1 = BroadcastFrame(
        frame_id=1,
        data=b"frame_1",
        timestamp=time.monotonic(),
        capture_time_ms=5.0,
        encode_time_ms=8.0,
        orig_w=1920,
        orig_h=1080,
        frame_w=1280,
        frame_h=720,
        size_bytes=7,
    )
    f2 = BroadcastFrame(
        frame_id=2,
        data=b"frame_2",
        timestamp=time.monotonic(),
        capture_time_ms=5.1,
        encode_time_ms=8.2,
        orig_w=1920,
        orig_h=1080,
        frame_w=1280,
        frame_h=720,
        size_bytes=7,
    )

    # Put frame 1
    buf.put(f1)
    assert buf.dropped_count == 0

    # Overwrite with frame 2 before consumer reads frame 1
    buf.put(f2)
    assert buf.dropped_count == 1

    # Consumer retrieves latest frame (f2)
    latest = buf.get_latest(timeout=0.1)
    assert latest is not None
    assert latest.frame_id == 2
    assert latest.data == b"frame_2"

    # Slot should now be empty
    empty_check = buf.get_latest(timeout=0.01)
    assert empty_check is None


def test_binary_frame_header_packing():
    b_frame = BroadcastFrame(
        frame_id=42,
        data=b"fake_jpeg_payload",
        timestamp=123.456,
        capture_time_ms=7.25,
        encode_time_ms=11.5,
        orig_w=1920,
        orig_h=1080,
        frame_w=1280,
        frame_h=720,
        size_bytes=len(b"fake_jpeg_payload"),
    )

    packed = b_frame.pack_with_header()
    # 32-byte header + payload
    assert len(packed) == 32 + len(b"fake_jpeg_payload")

    magic, frame_id, ts_ms, cap_ms, enc_ms, orig_w, orig_h, frame_w, frame_h = struct.unpack(
        "!4sIdffHHHH", packed[:32]
    )
    assert magic == b"TGFR"
    assert frame_id == 42
    assert abs(ts_ms - 123456.0) < 1.0
    assert abs(cap_ms - 7.25) < 0.01
    assert abs(enc_ms - 11.5) < 0.01
    assert orig_w == 1920
    assert orig_h == 1080
    assert frame_w == 1280
    assert frame_h == 720
    assert packed[32:] == b"fake_jpeg_payload"
