"""Tests for Adaptive Streaming Engine and Rolling Network Telemetry"""

import pytest
import time
from execution.adaptive_controller import AdaptiveStreamController, RollingStats, PROFILES


def test_rolling_stats_computation():
    stats = RollingStats(window_size=10)
    samples = [50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    for s in samples:
        stats.add_sample(s)

    assert stats.count() == 6
    assert stats.min() == 50.0
    assert stats.avg() == 75.0
    assert stats.p95() >= 90.0
    assert stats.jitter >= 0.0


def test_adaptive_controller_initial_profile():
    ctrl = AdaptiveStreamController(initial_profile="good")
    assert ctrl.current_profile.name == "good"
    assert ctrl.current_profile.fps == 24
    assert ctrl.current_profile.quality == 65


def test_adaptive_controller_downshift_hysteresis():
    ctrl = AdaptiveStreamController(initial_profile="good")
    # Feed warm-up samples (count reaches 4)
    for _ in range(4):
        ctrl.record_delivery_time(120.0)
        ctrl.record_rtt(400.0)

    # 1 evaluation completed, degrade_counter is 1
    assert ctrl.current_profile.name == "good"
    assert ctrl.degrade_counter == 1

    # 2 more evaluations -> degrade_counter is 3
    ctrl.record_delivery_time(120.0)
    ctrl.record_rtt(400.0)
    ctrl.record_delivery_time(120.0)
    ctrl.record_rtt(400.0)
    assert ctrl.current_profile.name == "good"
    assert ctrl.degrade_counter == 3

    # 4th degraded evaluation triggers downshift to moderate
    ctrl.record_delivery_time(120.0)
    ctrl.record_rtt(450.0)
    assert ctrl.current_profile.name == "moderate"


def test_adaptive_controller_upshift_hysteresis():
    ctrl = AdaptiveStreamController(initial_profile="poor")
    assert ctrl.current_profile.name == "poor"

    # Warm-up (4 samples)
    for _ in range(4):
        ctrl.record_delivery_time(15.0)
        ctrl.record_rtt(45.0)

    assert ctrl.promote_counter == 1

    # Feed healthy samples until promote_threshold (14)
    for _ in range(12):
        ctrl.record_delivery_time(15.0)
        ctrl.record_rtt(45.0)

    assert ctrl.promote_counter == 13
    assert ctrl.current_profile.name == "poor"

    # 14th sample promotes to moderate
    ctrl.record_delivery_time(15.0)
    ctrl.record_rtt(45.0)
    assert ctrl.current_profile.name == "moderate"


def test_manual_profile_override():
    ctrl = AdaptiveStreamController(initial_profile="moderate")
    p = ctrl.set_profile_manual("excellent")
    assert p.name == "excellent"
    assert ctrl.current_profile.fps == 30
    assert ctrl.current_profile.scale == 1.0


def test_diagnostics_telemetry():
    ctrl = AdaptiveStreamController(initial_profile="good")
    ctrl.record_rtt(65.0)
    ctrl.record_frame_sent(size_bytes=45000, capture_ms=8.5, encode_ms=12.2, proc_ms=18.0)
    ctrl.record_frame_dropped()

    diag = ctrl.get_diagnostics()
    assert "rtt_avg_ms" in diag
    assert "rtt_p95_ms" in diag
    assert "jitter_ms" in diag
    assert "bitrate_kbps" in diag
    assert diag["frames_sent"] == 1
    assert diag["frames_dropped"] == 1
    assert diag["capture_ms"] == 8.5
    assert diag["encode_ms"] == 12.2


def test_manual_override_locks_adaptive_shifts_until_auto():
    ctrl = AdaptiveStreamController(initial_profile="good")
    ctrl.set_profile_manual("poor")
    assert ctrl.manual_override is True
    assert ctrl.current_profile.name == "poor"

    # Healthy RTT samples should NOT upshift while manual override is locked
    for _ in range(25):
        ctrl.record_delivery_time(10.0)
        ctrl.record_rtt(20.0)
    assert ctrl.current_profile.name == "poor"

    # Switching back to 'auto' unlocks adaptive shifting
    ctrl.set_profile_manual("auto")
    assert ctrl.manual_override is False
