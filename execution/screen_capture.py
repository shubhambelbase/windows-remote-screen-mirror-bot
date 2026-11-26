"""Screen Capture Engine.

Provides crystal-clear desktop screen capture with native cursor rendering,
thread-safe per-thread MSS contexts, sub-millisecond timing instrumentation,
and a decoupled single-slot frame broadcaster.
"""

import ctypes
from ctypes import wintypes
import io
import logging
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import mss
from PIL import Image, ImageDraw

from execution.win32_desktop import ensure_interactive_desktop

logger = logging.getLogger(__name__)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HICON),
        ("ptScreenPos", POINT),
    ]


def draw_cursor_on_image(img: Image.Image, cursor_x: int, cursor_y: int):
    """Draws a clean, high-visibility Windows arrow cursor with a drop shadow onto the frame."""
    draw = ImageDraw.Draw(img)
    poly = [
        (cursor_x, cursor_y),
        (cursor_x, cursor_y + 26),
        (cursor_x + 7, cursor_y + 19),
        (cursor_x + 12, cursor_y + 30),
        (cursor_x + 16, cursor_y + 28),
        (cursor_x + 11, cursor_y + 18),
        (cursor_x + 20, cursor_y + 18),
    ]
    shadow = [(px + 2, py + 2) for px, py in poly]
    draw.polygon(shadow, fill="#000000")
    draw.polygon(poly, fill="#FFFFFF", outline="#000000", width=2)
    draw.ellipse((cursor_x - 1, cursor_y - 1, cursor_x + 2, cursor_y + 2), fill="#00E5FF")


@dataclass
class BroadcastFrame:
    frame_id: int
    data: bytes
    timestamp: float
    capture_time_ms: float
    encode_time_ms: float
    orig_w: int
    orig_h: int
    frame_w: int
    frame_h: int
    size_bytes: int

    def pack_with_header(self) -> bytes:
        """Packs frame with 32-byte binary header containing high-precision timing telemetry:
        [magic: 4B 'TGFR'][frame_id: 4B uint32][ts_ms: 8B double][cap_ms: 4B float][enc_ms: 4B float]
        [orig_w: 2B][orig_h: 2B][frame_w: 2B][frame_h: 2B] + JPEG payload.
        """
        header = struct.pack(
            "!4sIdffHHHH",
            b"TGFR",
            self.frame_id,
            self.timestamp * 1000.0,
            self.capture_time_ms,
            self.encode_time_ms,
            self.orig_w,
            self.orig_h,
            self.frame_w,
            self.frame_h,
        )
        return header + self.data


class ScreenCaptureEngine:
    """Manages high-fidelity frame grabbing with thread-safe MSS instances, cursor rendering, and encoding."""

    def __init__(self):
        ensure_interactive_desktop()
        self._tls = threading.local()
        self._sct_lock = threading.Lock()
        self._sct_instances: List[mss.MSS] = []
        self.last_capture_time_ms: float = 0.0
        self.last_encode_time_ms: float = 0.0
        # Warm up current thread's MSS instance
        self._get_sct()

    def _get_sct(self) -> mss.MSS:
        """Returns a thread-local MSS capture instance, creating it on first use in this thread."""
        sct = getattr(self._tls, "sct", None)
        if sct is None:
            ensure_interactive_desktop()
            sct = mss.MSS()
            self._tls.sct = sct
            with self._sct_lock:
                self._sct_instances.append(sct)
        return sct

    @property
    def _sct(self) -> mss.MSS:
        return self._get_sct()

    def get_monitors(self) -> List[Dict[str, Any]]:
        """Returns list of available monitors with resolution and offsets."""
        ensure_interactive_desktop()
        sct = self._get_sct()
        monitors = []
        for idx, m in enumerate(sct.monitors):
            monitors.append({
                "index": idx,
                "label": "All Displays" if idx == 0 else f"Display {idx}",
                "top": m["top"],
                "left": m["left"],
                "width": m["width"],
                "height": m["height"],
            })
        return monitors

    def get_monitor_rect(self, monitor_index: int = 1) -> Dict[str, int]:
        """Returns {left, top, width, height} for the requested monitor index."""
        sct = self._get_sct()
        if monitor_index < 0 or monitor_index >= len(sct.monitors):
            monitor_index = 1 if len(sct.monitors) > 1 else 0
        m = sct.monitors[monitor_index]
        return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}

    def _get_cursor_pos(self, target_monitor: Dict[str, int], orig_w: int, orig_h: int) -> Optional[Tuple[int, int]]:
        """Returns cursor position relative to target_monitor, or None if unavailable/out-of-bounds."""
        if os.name != "nt":
            return None
        try:
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            if ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
                cx = ci.ptScreenPos.x - target_monitor["left"]
                cy = ci.ptScreenPos.y - target_monitor["top"]
                if 0 <= cx < orig_w and 0 <= cy < orig_h:
                    return cx, cy

            pt = POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                cx = pt.x - target_monitor["left"]
                cy = pt.y - target_monitor["top"]
                if 0 <= cx < orig_w and 0 <= cy < orig_h:
                    return cx, cy
        except Exception as e:
            logger.debug(f"Cursor position query note: {e}")
        return None

    def _grab_raw_and_sig(
        self,
        monitor_index: int,
        draw_cursor: bool,
        quality: int,
        scale: float,
        format_type: str,
    ) -> Tuple[Image.Image, Tuple[Any, ...], Optional[Tuple[int, int]], float]:
        """Grabs the raw screen image and computes a fast CRC32 + cursor frame signature."""
        ensure_interactive_desktop()
        sct = self._get_sct()

        if monitor_index < 0 or monitor_index >= len(sct.monitors):
            monitor_index = 1 if len(sct.monitors) > 1 else 0

        target_monitor = sct.monitors[monitor_index]

        t0 = time.perf_counter()
        raw_bytes: Optional[bytes] = None
        try:
            sct_img = sct.grab(target_monitor)
            raw_bytes = sct_img.bgra
            img = Image.frombytes("RGB", sct_img.size, raw_bytes, "raw", "BGRX")
        except Exception as mss_err:
            logger.debug(f"MSS capture fallback: {mss_err}")
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab()
            except Exception as grab_err:
                logger.error(f"Failed to grab screen: {grab_err}")
                raise

        orig_w, orig_h = img.size
        cursor_pos = self._get_cursor_pos(target_monitor, orig_w, orig_h) if draw_cursor else None

        # Zero-copy CRC32 in C (~1ms for 1366x768 BGRA) avoids JPEG encode when screen is static
        crc_input = raw_bytes if raw_bytes is not None else img.tobytes()
        frame_sig = (
            zlib.crc32(crc_input),
            orig_w,
            orig_h,
            cursor_pos,
            monitor_index,
            quality,
            scale,
            format_type.upper(),
        )
        return img, frame_sig, cursor_pos, t0

    def _encode_captured_image(
        self,
        img: Image.Image,
        cursor_pos: Optional[Tuple[int, int]],
        quality: int,
        scale: float,
        format_type: str,
        t0: float,
    ) -> Tuple[bytes, int, int, int, int, float, float]:
        """Overlays cursor, resizes if scaled, encodes JPEG/WEBP, and records timing."""
        orig_w, orig_h = img.size
        if cursor_pos is not None:
            draw_cursor_on_image(img, cursor_pos[0], cursor_pos[1])

        frame_w, frame_h = orig_w, orig_h
        if 0.1 <= scale < 0.98:
            frame_w = max(320, int(orig_w * scale))
            frame_h = max(180, int(orig_h * scale))
            img = img.resize((frame_w, frame_h), Image.Resampling.BOX)

        t1 = time.perf_counter()
        cap_time_ms = (t1 - t0) * 1000.0
        self.last_capture_time_ms = cap_time_ms

        t2 = time.perf_counter()
        buffer = io.BytesIO()
        clamped_quality = max(25, min(quality, 95))

        if format_type.upper() == "WEBP":
            img.save(buffer, format="WEBP", quality=clamped_quality, method=0)
        else:
            img.save(
                buffer,
                format="JPEG",
                quality=clamped_quality,
                subsampling=2,
                optimize=False,
            )

        t3 = time.perf_counter()
        enc_time_ms = (t3 - t2) * 1000.0
        self.last_encode_time_ms = enc_time_ms

        return buffer.getvalue(), orig_w, orig_h, frame_w, frame_h, cap_time_ms, enc_time_ms

    def capture_frame_if_changed(
        self,
        last_sig: Optional[Tuple[Any, ...]] = None,
        last_emit_ts: float = 0.0,
        max_idle_s: float = 1.5,
        monitor_index: int = 1,
        quality: int = 65,
        scale: float = 0.85,
        format_type: str = "JPEG",
        draw_cursor: bool = True,
    ) -> Tuple[Optional[Tuple[bytes, int, int, int, int, float, float]], Tuple[Any, ...]]:
        """Captures and encodes only if pixels/cursor/config changed or max_idle_s elapsed."""
        img, frame_sig, cursor_pos, t0 = self._grab_raw_and_sig(
            monitor_index=monitor_index,
            draw_cursor=draw_cursor,
            quality=quality,
            scale=scale,
            format_type=format_type,
        )
        now_m = time.monotonic()
        if last_sig is not None and frame_sig == last_sig and (now_m - last_emit_ts) < max_idle_s:
            return None, frame_sig

        encoded = self._encode_captured_image(
            img=img,
            cursor_pos=cursor_pos,
            quality=quality,
            scale=scale,
            format_type=format_type,
            t0=t0,
        )
        return encoded, frame_sig

    def capture_frame_timed(
        self,
        monitor_index: int = 1,
        quality: int = 65,
        scale: float = 0.85,
        format_type: str = "JPEG",
        draw_cursor: bool = True,
    ) -> Tuple[bytes, int, int, int, int, float, float]:
        """Captures a frame and returns (bytes, orig_w, orig_h, frame_w, frame_h, cap_ms, enc_ms)."""
        img, _, cursor_pos, t0 = self._grab_raw_and_sig(
            monitor_index=monitor_index,
            draw_cursor=draw_cursor,
            quality=quality,
            scale=scale,
            format_type=format_type,
        )
        return self._encode_captured_image(
            img=img,
            cursor_pos=cursor_pos,
            quality=quality,
            scale=scale,
            format_type=format_type,
            t0=t0,
        )

    def capture_frame(
        self,
        monitor_index: int = 1,
        quality: int = 65,
        scale: float = 0.85,
        format_type: str = "JPEG",
        draw_cursor: bool = True,
    ) -> Tuple[bytes, int, int]:
        """Backward-compatible capture returning (frame_bytes, orig_w, orig_h)."""
        data, orig_w, orig_h, _, _, _, _ = self.capture_frame_timed(
            monitor_index=monitor_index,
            quality=quality,
            scale=scale,
            format_type=format_type,
            draw_cursor=draw_cursor,
        )
        return data, orig_w, orig_h

    def capture_screenshot(self, monitor_index: int = 1) -> bytes:
        """Captures a pristine, maximum-quality JPEG with mouse cursor for chat."""
        frame_bytes, _, _ = self.capture_frame(
            monitor_index=monitor_index,
            quality=88,
            scale=1.0,
            format_type="JPEG",
            draw_cursor=True,
        )
        return frame_bytes

    def close(self):
        """Releases all thread-local MSS resources safely."""
        with self._sct_lock:
            for sct in self._sct_instances:
                try:
                    sct.close()
                except Exception:
                    pass
            self._sct_instances.clear()
        if hasattr(self._tls, "sct"):
            self._tls.sct = None


class SingleSlotFrameBuffer:
    """Thread-safe single-slot frame buffer for each connected streaming client.
    Guarantees latest-frame-only semantics without socket bufferbloat.
    """

    def __init__(self):
        self._frame: Optional[BroadcastFrame] = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        self.dropped_count: int = 0

    def put(self, frame: BroadcastFrame):
        """Places newest frame in the single slot, replacing any unsent frame."""
        with self._lock:
            if self._frame is not None and self._frame.frame_id != frame.frame_id:
                self.dropped_count += 1
            self._frame = frame
            self._event.set()

    def get_latest(self, timeout: float = 0.1) -> Optional[BroadcastFrame]:
        """Waits for and retrieves the freshest frame, clearing the slot."""
        if self._event.wait(timeout=timeout):
            with self._lock:
                self._event.clear()
                f = self._frame
                self._frame = None
                return f
        return None

    def clear(self):
        with self._lock:
            self._frame = None
            self._event.clear()


class ScreenCaptureBroadcaster:
    """High-speed decoupled background capture worker.
    Uses per-worker stop events so rapid reconnects never spawn duplicate zombie threads.
    """

    def __init__(self, capture_engine: ScreenCaptureEngine):
        self.engine = capture_engine
        self.latest_frame: Optional[bytes] = None
        self.latest_broadcast_frame: Optional[BroadcastFrame] = None
        self.frame_id: int = 0
        self.lock = threading.Lock()
        self.running: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._subscribers: Set[SingleSlotFrameBuffer] = set()
        self._subs_lock = threading.Lock()

        self.fps: int = 24
        self.quality: int = 65
        self.scale: float = 0.85
        self.monitor: int = 1

    def add_subscriber(self, buffer: SingleSlotFrameBuffer):
        """Registers a client buffer to receive live frames."""
        with self._subs_lock:
            self._subscribers.add(buffer)
        self.add_client()

    def remove_subscriber(self, buffer: SingleSlotFrameBuffer):
        """Unregisters a client buffer."""
        with self._subs_lock:
            self._subscribers.discard(buffer)
        self.remove_client()

    def add_client(self):
        with self.lock:
            if not self.running:
                self._stop_event = threading.Event()
                self.running = True
                self._thread = threading.Thread(
                    target=self._capture_worker,
                    args=(self._stop_event,),
                    daemon=True,
                )
                self._thread.start()

    def remove_client(self):
        with self.lock:
            with self._subs_lock:
                active_subs = len(self._subscribers)
            if active_subs == 0 and self.running:
                self.running = False
                self._stop_event.set()

    def stop(self):
        """Stops the capture worker cleanly."""
        with self.lock:
            self.running = False
            self._stop_event.set()
            worker = self._thread
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

    def update_config(
        self,
        fps: Optional[int] = None,
        quality: Optional[int] = None,
        scale: Optional[float] = None,
        monitor: Optional[int] = None,
    ):
        with self.lock:
            if fps is not None:
                self.fps = max(8, min(fps, 35))
            if quality is not None:
                self.quality = max(25, min(quality, 85))
            if scale is not None:
                self.scale = max(0.2, min(scale, 1.0))
            if monitor is not None:
                self.monitor = monitor

    def _capture_worker(self, stop_event: threading.Event):
        ensure_interactive_desktop()
        logger.info("High-speed decoupled background screen capture worker started.")
        last_sig: Optional[Tuple[Any, ...]] = None
        last_emit_ts: float = 0.0
        prev_sub_count: int = 0

        while not stop_event.is_set():
            with self.lock:
                target_fps = self.fps
                q = self.quality
                s = self.scale
                m = self.monitor

            interval = 1.0 / target_fps
            t_start = time.monotonic()

            try:
                with self._subs_lock:
                    subscribers = list(self._subscribers)

                # Force an immediate fresh frame when a new client subscribes
                sub_count = len(subscribers)
                effective_sig = None if sub_count > prev_sub_count else last_sig
                prev_sub_count = sub_count

                captured, new_sig = self.engine.capture_frame_if_changed(
                    last_sig=effective_sig,
                    last_emit_ts=last_emit_ts,
                    max_idle_s=1.5,
                    monitor_index=m,
                    quality=q,
                    scale=s,
                    format_type="JPEG",
                    draw_cursor=True,
                )
                last_sig = new_sig

                if captured is not None:
                    (
                        frame_bytes,
                        orig_w,
                        orig_h,
                        frame_w,
                        frame_h,
                        cap_ms,
                        enc_ms,
                    ) = captured

                    now_ts = time.monotonic()
                    last_emit_ts = now_ts
                    with self.lock:
                        self.frame_id += 1
                        b_frame = BroadcastFrame(
                            frame_id=self.frame_id,
                            data=frame_bytes,
                            timestamp=now_ts,
                            capture_time_ms=cap_ms,
                            encode_time_ms=enc_ms,
                            orig_w=orig_w,
                            orig_h=orig_h,
                            frame_w=frame_w,
                            frame_h=frame_h,
                            size_bytes=len(frame_bytes),
                        )
                        self.latest_frame = frame_bytes
                        self.latest_broadcast_frame = b_frame

                    for sub in subscribers:
                        sub.put(b_frame)

            except Exception as e:
                logger.debug(f"Capture worker frame exception: {e}")

            elapsed = time.monotonic() - t_start
            sleep_time = max(0.001, interval - elapsed)
            stop_event.wait(sleep_time)

        logger.info("Decoupled screen capture worker stopped.")
