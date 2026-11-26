"""Tests for WebRTC Experimental Streaming Module and Signaling"""

import pytest
import asyncio
from unittest.mock import MagicMock

pytest.importorskip("aiortc")

from execution.webrtc_streamer import WebRTCSessionManager
from execution.screen_capture import ScreenCaptureEngine
from execution.input_controller import InputController


def test_webrtc_session_manager_init():
    async def _run():
        engine = ScreenCaptureEngine()
        ctrl = InputController()
        mgr = WebRTCSessionManager(engine, ctrl)

        assert len(mgr.ice_servers) >= 2
        assert "stun:" in mgr.ice_servers[0].urls[0]

        await mgr.close_all()
        engine.close()

    asyncio.run(_run())


def test_webrtc_input_dispatch():
    engine = ScreenCaptureEngine()
    ctrl = InputController()
    mgr = WebRTCSessionManager(engine, ctrl)

    mock_channel = MagicMock()

    # Test ping-pong via DataChannel
    mgr._handle_channel_input(mock_channel, {"type": "ping", "t": 12345})
    assert mock_channel.send.called
    sent_payload = mock_channel.send.call_args[0][0]
    assert "pong" in sent_payload

    engine.close()
