"""Adaptive Streaming Controller & Real-Time Network Diagnostics Engine.

Implements multi-profile adaptive bitrate/quality control with hysteresis,
manual override locking, rolling latency telemetry (Min/Avg/P95 RTT, RFC 3550 jitter),
and single-slot frame buffering diagnostics.
"""

import time
import math
import logging
from collections import deque
from dataclasses import dataclass
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


@dataclass
class StreamProfile:
    name: str
    fps: int
    quality: int
    scale: float
    description: str


PROFILES: List[StreamProfile] = [
    StreamProfile(name="poor", fps=8, quality=40, scale=0.45, description="Poor Network (640x360 / 8 FPS)"),
    StreamProfile(name="moderate", fps=15, quality=55, scale=0.65, description="Moderate Network (960x540 / 15 FPS)"),
    StreamProfile(name="good", fps=24, quality=65, scale=0.85, description="Good Network (1280x720 / 24 FPS)"),
    StreamProfile(name="excellent", fps=30, quality=75, scale=1.0, description="Excellent Network (1080p / 30 FPS)"),
]

PROFILE_MAP: Dict[str, int] = {p.name: idx for idx, p in enumerate(PROFILES)}


class RollingStats:
    """Computes rolling statistical metrics (min, avg, P95, jitter) over a fixed window."""

    def __init__(self, window_size: int = 25):
        self.window_size = window_size
        self._samples: deque = deque(maxlen=window_size)
        self.last_rtt: float = 0.0
        self.jitter: float = 0.0

    def add_sample(self, val: float):
        if val <= 0:
            return
        if self._samples:
            diff = abs(val - self.last_rtt)
            self.jitter += (diff - self.jitter) / 16.0
        self.last_rtt = val
        self._samples.append(val)

    def min(self) -> float:
        return min(self._samples) if self._samples else 0.0

    def avg(self) -> float:
        return sum(self._samples) / len(self._samples) if self._samples else 0.0

    def p95(self) -> float:
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        idx = min(len(sorted_samples) - 1, int(math.ceil(0.95 * len(sorted_samples))) - 1)
        return sorted_samples[max(0, idx)]

    def count(self) -> int:
        return len(self._samples)


class AdaptiveStreamController:
    """Monitors live network performance and dynamically transitions between streaming profiles
    using strict hysteresis, while honoring explicit manual profile locks.
    """

    def __init__(self, initial_profile: str = "good"):
        self.profile_index: int = PROFILE_MAP.get(initial_profile.lower(), 2)
        self.manual_override: bool = False
        self.rtt_stats = RollingStats(window_size=30)
        self.delivery_time_stats = RollingStats(window_size=20)

        self.degrade_counter: int = 0
        self.promote_counter: int = 0
        self.degrade_threshold: int = 4
        self.promote_threshold: int = 14

        self.total_bytes_sent: int = 0
        self.bytes_window_start: float = time.monotonic()
        self.bytes_in_window: int = 0
        self.measured_bitrate_kbps: float = 0.0

        self.frames_sent: int = 0
        self.frames_dropped: int = 0
        self.fps_counter: int = 0
        self.fps_window_start: float = time.monotonic()
        self.measured_fps: float = 0.0

        self.last_capture_ms: float = 0.0
        self.last_encode_ms: float = 0.0
        self.last_server_proc_ms: float = 0.0
        self.last_frame_age_ms: float = 0.0

        self.reconnect_count: int = 0

    @property
    def current_profile(self) -> StreamProfile:
        return PROFILES[self.profile_index]

    def set_auto_mode(self) -> StreamProfile:
        """Re-enables automatic network-adaptive profile switching."""
        self.manual_override = False
        self.degrade_counter = 0
        self.promote_counter = 0
        logger.info("Adaptive profile mode set to AUTO.")
        return self.current_profile

    def set_profile_manual(self, profile_name: str) -> StreamProfile:
        """Manually locks a profile or restores AUTO adaptive mode when 'auto' is passed."""
        key = profile_name.lower().strip()
        if key == "auto":
            return self.set_auto_mode()
        if key in PROFILE_MAP:
            self.profile_index = PROFILE_MAP[key]
            self.manual_override = True
            self.degrade_counter = 0
            self.promote_counter = 0
            logger.info(f"Manual profile locked to: {self.current_profile.name}")
        return self.current_profile

    def record_rtt(self, rtt_ms: float):
        """Records a measured round-trip time sample."""
        self.rtt_stats.add_sample(rtt_ms)
        self._evaluate_network_health()

    def record_delivery_time(self, delivery_ms: float):
        """Records the time taken to transmit a frame over the WebSocket."""
        self.delivery_time_stats.add_sample(delivery_ms)

    def record_frame_sent(self, size_bytes: int, capture_ms: float = 0.0, encode_ms: float = 0.0, proc_ms: float = 0.0):
        """Updates frame transmission counters and calculates rolling bitrate and FPS."""
        now = time.monotonic()
        self.frames_sent += 1
        self.fps_counter += 1
        self.total_bytes_sent += size_bytes
        self.bytes_in_window += size_bytes

        self.last_capture_ms = capture_ms
        self.last_encode_ms = encode_ms
        self.last_server_proc_ms = proc_ms

        time_diff = now - self.bytes_window_start
        if time_diff >= 1.0:
            self.measured_bitrate_kbps = (self.bytes_in_window * 8.0) / (time_diff * 1000.0)
            self.bytes_in_window = 0
            self.bytes_window_start = now

        fps_diff = now - self.fps_window_start
        if fps_diff >= 1.0:
            if fps_diff > 2.5:
                # Reset stale window after idle pause instead of reporting fractional <1 FPS
                self.fps_counter = 1
                self.fps_window_start = now
            else:
                self.measured_fps = round(self.fps_counter / fps_diff, 1)
                self.fps_counter = 0
                self.fps_window_start = now

    def record_frame_dropped(self):
        """Records a frame dropped due to single-slot buffer overwrite."""
        self.frames_dropped += 1

    def record_reconnect(self):
        """Tracks client reconnection occurrences."""
        self.reconnect_count += 1

    def _evaluate_network_health(self):
        """Analyzes rolling RTT, jitter, and delivery duration to adjust streaming profiles."""
        if self.manual_override or self.rtt_stats.count() < 4:
            return

        p95_rtt = self.rtt_stats.p95()
        jitter = self.rtt_stats.jitter
        avg_delivery = self.delivery_time_stats.avg()

        is_congested = (p95_rtt > 320.0 or avg_delivery > 85.0 or jitter > 55.0)
        is_healthy = (p95_rtt < 130.0 and avg_delivery < 35.0 and jitter < 25.0)

        if is_congested:
            self.degrade_counter += 1
            self.promote_counter = 0
            if self.degrade_counter >= self.degrade_threshold:
                self._downshift_profile()
                self.degrade_counter = 0
        elif is_healthy:
            self.promote_counter += 1
            self.degrade_counter = 0
            if self.promote_counter >= self.promote_threshold:
                self._upshift_profile()
                self.promote_counter = 0
        else:
            self.degrade_counter = max(0, self.degrade_counter - 1)
            self.promote_counter = max(0, self.promote_counter - 1)

    def _downshift_profile(self):
        if self.profile_index > 0:
            self.profile_index -= 1
            logger.info(
                f"Adaptive Downshift -> {self.current_profile.name.upper()} "
                f"(P95 RTT: {self.rtt_stats.p95():.1f}ms, Jitter: {self.rtt_stats.jitter:.1f}ms)"
            )

    def _upshift_profile(self):
        if self.profile_index < len(PROFILES) - 1:
            self.profile_index += 1
            logger.info(
                f"Adaptive Upshift -> {self.current_profile.name.upper()} "
                f"(P95 RTT: {self.rtt_stats.p95():.1f}ms, Jitter: {self.rtt_stats.jitter:.1f}ms)"
            )

    def get_diagnostics(self) -> Dict[str, Any]:
        """Returns comprehensive diagnostic telemetry."""
        return {
            "rtt_min_ms": round(self.rtt_stats.min(), 1),
            "rtt_avg_ms": round(self.rtt_stats.avg(), 1),
            "rtt_p95_ms": round(self.rtt_stats.p95(), 1),
            "jitter_ms": round(self.rtt_stats.jitter, 1),
            "fps": self.measured_fps,
            "target_fps": self.current_profile.fps,
            "bitrate_kbps": round(self.measured_bitrate_kbps, 1),
            "frames_sent": self.frames_sent,
            "frames_dropped": self.frames_dropped,
            "profile": self.current_profile.name,
            "manual_override": self.manual_override,
            "quality": self.current_profile.quality,
            "scale": self.current_profile.scale,
            "capture_ms": round(self.last_capture_ms, 2),
            "encode_ms": round(self.last_encode_ms, 2),
            "server_proc_ms": round(self.last_server_proc_ms, 2),
            "reconnect_count": self.reconnect_count,
        }
