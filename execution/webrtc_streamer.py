"""WebRTC Experimental Streamer & DataChannel Hub
Provides low-latency UDP-based screen streaming via RTP and bidirectional input
control via WebRTC DataChannels with STUN/TURN NAT traversal.
Operates as an experimental alternative alongside the default WebSocket pipeline.
"""

import os
import time
import json
import asyncio
import logging
from typing import Dict, Any, Optional, Set
from fractions import Fraction

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceServer,
    VideoStreamTrack,
)
from aiortc.contrib.media import MediaRelay
from PIL import Image
import av

from execution.screen_capture import ScreenCaptureEngine, ensure_interactive_desktop
from execution.input_controller import InputController

logger = logging.getLogger(__name__)

# Default public STUN servers for NAT traversal
DEFAULT_ICE_SERVERS = [
    RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]),
    RTCIceServer(urls=["stun:stun.cloudflare.com:3478"]),
]


class ScreenVideoStreamTrack(VideoStreamTrack):
    """Custom WebRTC VideoTrack streaming live desktop frames at adaptive framerates."""

    kind = "video"

    def __init__(self, capture_engine: ScreenCaptureEngine, fps: int = 25, scale: float = 0.85):
        super().__init__()
        self.engine = capture_engine
        self.fps = fps
        self.scale = scale
        self._start_time = time.monotonic()
        self._frame_count = 0
        self.time_base = Fraction(1, 90000)

    async def recv(self):
        pts = int((time.monotonic() - self._start_time) * 90000)
        self._frame_count += 1

        # Rate control sleep
        interval = 1.0 / self.fps
        await asyncio.sleep(interval)

        # Capture desktop frame using thread-local MSS context
        try:
            ensure_interactive_desktop()
            sct = self.engine._get_sct()
            target_mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            sct_img = sct.grab(target_mon)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

            if 0.1 <= self.scale < 0.98:
                tw = max(320, int(img.width * self.scale))
                th = max(180, int(img.height * self.scale))
                img = img.resize((tw, th), Image.Resampling.BOX)

            frame = av.VideoFrame.from_image(img)
            frame.pts = pts
            frame.time_base = self.time_base
            return frame
        except Exception as e:
            logger.debug(f"WebRTC frame capture note: {e}")
            # Fallback black frame on grab anomaly
            frame = av.VideoFrame(width=640, height=360, format="yuv420p")
            frame.pts = pts
            frame.time_base = self.time_base
            return frame


class WebRTCSessionManager:
    """Manages WebRTC PeerConnections, STUN/TURN traversal, and DataChannel input dispatch."""

    def __init__(self, capture_engine: ScreenCaptureEngine, input_ctrl: InputController):
        self.engine = capture_engine
        self.input_ctrl = input_ctrl
        self.pcs: Set[RTCPeerConnection] = set()

        turn_url = os.getenv("TURN_URL")
        turn_user = os.getenv("TURN_USERNAME")
        turn_cred = os.getenv("TURN_CREDENTIAL")

        self.ice_servers = list(DEFAULT_ICE_SERVERS)
        if turn_url:
            self.ice_servers.append(
                RTCIceServer(urls=[turn_url], username=turn_user, credential=turn_cred)
            )

    async def handle_offer(self, sdp: str, sdp_type: str) -> Dict[str, Any]:
        """Handles SDP offer from browser and creates SDP answer."""
        config = RTCConfiguration(iceServers=self.ice_servers)
        pc = RTCPeerConnection(configuration=config)
        self.pcs.add(pc)

        # Add video track
        video_track = ScreenVideoStreamTrack(self.engine, fps=24, scale=0.85)
        pc.addTrack(video_track)

        @pc.on("datachannel")
        def on_datachannel(channel):
            logger.info(f"WebRTC DataChannel established: {channel.label}")

            @channel.on("message")
            def on_message(message):
                try:
                    data = json.loads(message)
                    self._handle_channel_input(channel, data)
                except Exception as e:
                    logger.debug(f"DataChannel message parse error: {e}")

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"WebRTC connection state: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                self.pcs.discard(pc)
                self.input_ctrl.release_all_inputs()

        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    def _handle_channel_input(self, channel, data: Dict[str, Any]):
        """Dispatches input packets received via WebRTC DataChannel."""
        msg_type = data.get("type")

        if msg_type == "ping":
            channel.send(json.dumps({
                "type": "pong",
                "t": data.get("t"),
                "st": time.monotonic() * 1000.0,
            }))

        elif msg_type == "mouse_move":
            dx = float(data.get("dx", 0))
            dy = float(data.get("dy", 0))
            sens = float(data.get("sensitivity", 1.0))
            self.input_ctrl.mouse_move_relative(dx, dy, sensitivity=sens)

        elif msg_type == "mouse_click":
            btn = data.get("button", "left")
            self.input_ctrl.mouse_click(btn)

        elif msg_type == "mouse_down":
            self.input_ctrl.mouse_down(data.get("button", "left"))

        elif msg_type == "mouse_up":
            self.input_ctrl.mouse_up(data.get("button", "left"))

        elif msg_type == "mouse_wheel":
            self.input_ctrl.mouse_wheel(
                delta_y=int(data.get("delta_y", 0)),
                delta_x=int(data.get("delta_x", 0)),
            )

        elif msg_type == "direct_touch":
            screen_w, screen_h = self.input_ctrl.get_screen_size()
            action = data.get("action", "move")
            norm_x = max(0.0, min(1.0, float(data.get("norm_x", 0))))
            norm_y = max(0.0, min(1.0, float(data.get("norm_y", 0))))
            target_x = max(0, min(screen_w - 1, int(norm_x * screen_w)))
            target_y = max(0, min(screen_h - 1, int(norm_y * screen_h)))
            self.input_ctrl.mouse_move_absolute(target_x, target_y)
            if action == "down":
                self.input_ctrl.mouse_down("left")
            elif action == "up":
                self.input_ctrl.mouse_up("left")

        elif msg_type == "key_press":
            self.input_ctrl.key_press(data.get("key", ""))

        elif msg_type == "hotkey":
            self.input_ctrl.hotkey(data.get("keys", []))

        elif msg_type == "type_text":
            self.input_ctrl.type_text(data.get("text", ""))

    async def close_all(self):
        """Closes all active WebRTC peer connections."""
        for pc in list(self.pcs):
            await pc.close()
        self.pcs.clear()
