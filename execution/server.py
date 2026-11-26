"""FastAPI Host Server & High-Performance WebSocket Streaming Hub.

Provides real-time screen mirroring using single-slot latest-frame buffers,
decoupled high-priority input handling, network-aware adaptive streaming,
constant-time token validation, and real-time latency diagnostics.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.adaptive_controller import AdaptiveStreamController
from execution.config import SECRET_KEY
from execution.input_controller import InputController
from execution.screen_capture import (
    ScreenCaptureBroadcaster,
    ScreenCaptureEngine,
    SingleSlotFrameBuffer,
)
from execution.session_manager import is_session_active

try:
    from execution.webrtc_streamer import WebRTCSessionManager
except ImportError:
    WebRTCSessionManager = None

logger = logging.getLogger("RemoteServer")

capture_engine = ScreenCaptureEngine()
broadcaster = ScreenCaptureBroadcaster(capture_engine)
input_ctrl = InputController()
stream_controller = AdaptiveStreamController(initial_profile="good")
webrtc_mgr = WebRTCSessionManager(capture_engine, input_ctrl) if WebRTCSessionManager else None

WEB_DIR = Path(__file__).parent / "web"
INDEX_FILE = WEB_DIR / "index.html"

# Simple sliding-window auth failure tracker to blunt token guessing
_auth_failures: Dict[str, List[float]] = {}
AUTH_WINDOW_SECONDS = 60.0
AUTH_MAX_FAILURES = 15


def _verify_token(token: Optional[str], client_ip: str = "unknown") -> bool:
    """Constant-time token check with basic sliding-window brute-force guard."""
    now = time.monotonic()
    recent = [t for t in _auth_failures.get(client_ip, []) if now - t < AUTH_WINDOW_SECONDS]
    if len(recent) >= AUTH_MAX_FAILURES:
        _auth_failures[client_ip] = recent
        raise HTTPException(status_code=429, detail="Too many failed authentication attempts")

    if not SECRET_KEY:
        return True
    if token and secrets.compare_digest(token, SECRET_KEY):
        _auth_failures.pop(client_ip, None)
        return True

    recent.append(now)
    _auth_failures[client_ip] = recent
    return False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    logger.info("Shutting down server capture & WebRTC resources...")
    broadcaster.stop()
    if webrtc_mgr:
        try:
            await webrtc_mgr.close_all()
        except Exception:
            pass
    capture_engine.close()


app = FastAPI(title="Windows Remote Control Server", lifespan=lifespan)


@app.get("/")
async def serve_index(token: Optional[str] = Query(None)):
    """Serves the Mini App HTML interface."""
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="Web interface files not found")
    return FileResponse(INDEX_FILE, headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    screen_w, screen_h = await asyncio.to_thread(input_ctrl.get_screen_size)
    monitors = await asyncio.to_thread(capture_engine.get_monitors)
    return {
        "status": "online",
        "platform": sys.platform,
        "computer_name": os.environ.get("COMPUTERNAME", "Windows-PC"),
        "resolution": f"{screen_w}x{screen_h}",
        "monitors": len(monitors),
        "active_profile": stream_controller.current_profile.name,
        "session_active": is_session_active(),
    }


@app.get("/api/system/info")
async def get_system_info(request: Request, token: Optional[str] = Query(None)):
    """Returns monitor setup, screen resolution, active window, and battery info (Protected)."""
    client_ip = request.client.host if request.client else "unknown"
    if not _verify_token(token, client_ip):
        raise HTTPException(status_code=403, detail="Invalid authorization token")
    screen_w, screen_h = await asyncio.to_thread(input_ctrl.get_screen_size)
    cursor_x, cursor_y = await asyncio.to_thread(input_ctrl.get_cursor_pos)
    window_title = await asyncio.to_thread(input_ctrl.get_active_window_title)
    battery = await asyncio.to_thread(input_ctrl.get_battery_status)
    monitors = await asyncio.to_thread(capture_engine.get_monitors)
    return {
        "screen_size": {"width": screen_w, "height": screen_h},
        "cursor_pos": {"x": cursor_x, "y": cursor_y},
        "active_window": window_title,
        "battery": battery,
        "monitors": monitors,
    }


@app.get("/api/system/diagnostics")
async def get_system_diagnostics(request: Request, token: Optional[str] = Query(None)):
    """Returns real-time network and streaming telemetry (Protected)."""
    client_ip = request.client.host if request.client else "unknown"
    if not _verify_token(token, client_ip):
        raise HTTPException(status_code=403, detail="Invalid authorization token")
    return stream_controller.get_diagnostics()


@app.post("/api/webrtc/offer")
async def webrtc_offer(request: Request, payload: Dict[str, Any]):
    """WebRTC signaling endpoint to exchange SDP offer/answer."""
    client_ip = request.client.host if request.client else "unknown"
    auth_token = payload.get("token")
    if not _verify_token(auth_token if isinstance(auth_token, str) else None, client_ip):
        raise HTTPException(status_code=403, detail="Invalid authorization token")
    if not webrtc_mgr:
        raise HTTPException(status_code=501, detail="WebRTC streamer not initialized")

    sdp = payload.get("sdp")
    sdp_type = payload.get("type", "offer")
    if not sdp:
        raise HTTPException(status_code=400, detail="Missing SDP offer")

    return await webrtc_mgr.handle_offer(sdp, sdp_type)


def _run_ps_command_sync(command_str: str) -> Dict[str, Any]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command_str],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=creationflags,
    )
    return {
        "success": True,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "returncode": res.returncode,
    }


@app.post("/api/system/command")
async def execute_system_command(request: Request, payload: Dict[str, Any]):
    """Executes host level system actions (lock, sleep, taskbar, cmd)."""
    client_ip = request.client.host if request.client else "unknown"
    auth_token = payload.get("token")
    if not _verify_token(auth_token if isinstance(auth_token, str) else None, client_ip):
        raise HTTPException(status_code=403, detail="Invalid authorization token")

    action = str(payload.get("action", "")).lower()

    if action == "lock":
        if os.name == "nt":
            import ctypes
            code = await asyncio.to_thread(ctypes.windll.user32.LockWorkStation)
            return {"success": bool(code), "action": "lock", "code": code}
        return {"success": False, "error": "Not supported on this platform"}

    elif action == "sleep":
        if os.name == "nt":
            import ctypes
            code = await asyncio.to_thread(ctypes.windll.powrprof.SetSuspendState, 0, 1, 0)
            return {"success": bool(code), "action": "sleep", "code": code}
        return {"success": False, "error": "Not supported on this platform"}

    elif action in ("hide_taskbar", "close_taskbar"):
        res = await asyncio.to_thread(input_ctrl.set_taskbar_visible, False)
        return {"success": res, "action": action, "taskbar_visible": False}

    elif action in ("show_taskbar", "open_taskbar"):
        res = await asyncio.to_thread(input_ctrl.set_taskbar_visible, True)
        return {"success": res, "action": action, "taskbar_visible": True}

    elif action == "toggle_taskbar":
        current = await asyncio.to_thread(input_ctrl.is_taskbar_visible)
        res = await asyncio.to_thread(input_ctrl.set_taskbar_visible, not current)
        return {"success": res, "action": action, "taskbar_visible": not current}

    elif action == "cmd":
        command_str = payload.get("command", "")
        if not command_str:
            raise HTTPException(status_code=400, detail="Missing command string")
        try:
            return await asyncio.to_thread(_run_ps_command_sync, str(command_str))
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command execution timed out (15s limit)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown system action: {action}")


async def _accept_authorized_ws(websocket: WebSocket, token: Optional[str]) -> bool:
    """Validates WebSocket auth token and active standby session before accepting."""
    client_ip = websocket.client.host if websocket.client else "unknown"
    try:
        authorized = _verify_token(token, client_ip)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Too many auth failures")
        return False

    if not authorized:
        logger.warning(f"Rejected unauthorized WebSocket connection attempt from {client_ip}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid authorization token")
        return False

    if not is_session_active():
        logger.warning("Rejected WebSocket connection: Remote session is in standby (send /start in Telegram first)")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Standby mode - Send /start in Telegram to activate")
        return False

    await websocket.accept()
    return True


def _coalesce_or_enqueue_input(input_queue: asyncio.Queue, msg_type: str, data: Dict[str, Any]) -> bool:
    """Coalesces consecutive mouse_move events or attempts non-blocking queue insertion.

    Returns True if handled synchronously/dropped, False if caller must await put().
    """
    if msg_type == "mouse_move" and not input_queue.empty():
        try:
            last_item = input_queue._queue[-1]
            if last_item[0] == "mouse_move":
                last_item[1]["dx"] = float(last_item[1].get("dx", 0)) + float(data.get("dx", 0))
                last_item[1]["dy"] = float(last_item[1].get("dy", 0)) + float(data.get("dy", 0))
                if "sensitivity" in data:
                    last_item[1]["sensitivity"] = data["sensitivity"]
                return True
        except Exception:
            pass

    try:
        input_queue.put_nowait((msg_type, data))
        return True
    except asyncio.QueueFull:
        # Drop overflow relative moves rather than stalling critical clicks/keys
        return msg_type == "mouse_move"


async def _run_input_worker(
    input_queue: asyncio.Queue,
    is_active_fn,
    rect_state: Dict[str, Any],
):
    """Consumes queued mouse/keyboard/touch events cleanly without blocking WebSocket reads."""
    while is_active_fn():
        try:
            item = await input_queue.get()
            if item is None:
                break

            msg_type, data = item

            if msg_type == "mouse_move":
                dx = float(data.get("dx", 0))
                dy = float(data.get("dy", 0))
                sens = float(data.get("sensitivity", 1.0))
                input_ctrl.mouse_move_relative(dx, dy, sensitivity=sens)

            elif msg_type == "mouse_click":
                btn = data.get("button", "left")
                if btn == "double":
                    await input_ctrl.async_mouse_double_click("left")
                else:
                    await input_ctrl.async_mouse_click(btn)

            elif msg_type == "mouse_down":
                input_ctrl.mouse_down(data.get("button", "left"))

            elif msg_type == "mouse_up":
                input_ctrl.mouse_up(data.get("button", "left"))

            elif msg_type == "mouse_wheel":
                delta_y = int(round(float(data.get("delta_y", 0))))
                delta_x = int(round(float(data.get("delta_x", 0))))
                input_ctrl.mouse_wheel(delta_y=delta_y, delta_x=delta_x)

            elif msg_type == "direct_touch":
                action = data.get("action", "move")
                norm_x = max(0.0, min(1.0, float(data.get("norm_x", 0))))
                norm_y = max(0.0, min(1.0, float(data.get("norm_y", 0))))

                now_m = time.monotonic()
                if now_m - rect_state["last_check"] > 2.0:
                    rect_state["rect"] = await asyncio.to_thread(
                        capture_engine.get_monitor_rect, broadcaster.monitor
                    )
                    rect_state["last_check"] = now_m

                cached_rect = rect_state["rect"]
                mon_left = cached_rect.get("left", 0)
                mon_top = cached_rect.get("top", 0)
                mon_w = max(1, cached_rect.get("width", 1920))
                mon_h = max(1, cached_rect.get("height", 1080))

                target_x = mon_left + min(mon_w - 1, int(norm_x * mon_w))
                target_y = mon_top + min(mon_h - 1, int(norm_y * mon_h))

                input_ctrl.mouse_move_absolute(target_x, target_y)
                if action == "down":
                    input_ctrl.mouse_down("left")
                elif action == "up":
                    input_ctrl.mouse_up("left")

            elif msg_type == "key_press":
                key_name = data.get("key", "")
                if key_name:
                    await input_ctrl.async_key_press(key_name)

            elif msg_type == "hotkey":
                keys = data.get("keys", [])
                if keys:
                    await input_ctrl.async_hotkey(keys)

            elif msg_type == "type_text":
                text_content = data.get("text", "")
                if text_content:
                    await input_ctrl.async_type_text(str(text_content))

            input_queue.task_done()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Input worker error: {e}")


async def _handle_ping_or_config(
    websocket: WebSocket,
    msg_type: str,
    data: Dict[str, Any],
    rect_state: Dict[str, Any],
):
    """Handles fast-path ping/pong telemetry and stream quality/monitor config changes."""
    if msg_type == "ping":
        client_t = data.get("t")
        client_rtt = data.get("rtt")
        if client_rtt is not None:
            stream_controller.record_rtt(float(client_rtt))

        active_profile = stream_controller.current_profile
        broadcaster.update_config(
            fps=active_profile.fps,
            quality=active_profile.quality,
            scale=active_profile.scale,
        )

        diag = stream_controller.get_diagnostics()
        await websocket.send_text(json.dumps({
            "type": "pong",
            "t": client_t,
            "st": time.monotonic() * 1000.0,
            "diag": diag,
        }))

    elif msg_type == "config":
        profile_name = data.get("profile")
        if profile_name:
            p = stream_controller.set_profile_manual(str(profile_name))
            broadcaster.update_config(fps=p.fps, quality=p.quality, scale=p.scale)
        else:
            fps = int(data.get("fps", 24))
            quality = int(data.get("quality", 65))
            scale = float(data.get("scale", 0.85))
            monitor = int(data.get("monitor", 1))
            broadcaster.update_config(fps=fps, quality=quality, scale=scale, monitor=monitor)
            rect_state["rect"] = await asyncio.to_thread(capture_engine.get_monitor_rect, monitor)
            rect_state["last_check"] = time.monotonic()


MAX_IN_FLIGHT_FRAMES = 4
FRAME_ACK_TIMEOUT_S = 0.45


@app.websocket("/ws/control")
async def websocket_control_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    """Dedicated low-latency control channel for ping/pong, stream config, and input events."""
    if not await _accept_authorized_ws(websocket, token):
        return

    logger.info("Client connected to dedicated control channel (/ws/control)")
    input_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    client_active = True
    rect_state: Dict[str, Any] = {
        "rect": await asyncio.to_thread(capture_engine.get_monitor_rect, broadcaster.monitor),
        "last_check": time.monotonic(),
    }

    async def receive_control_loop():
        while client_active:
            try:
                raw_data = await websocket.receive_text()
                try:
                    data = json.loads(raw_data)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                msg_type = data.get("type")

                if msg_type in ("ping", "config"):
                    await _handle_ping_or_config(websocket, msg_type, data, rect_state)
                elif msg_type:
                    if not _coalesce_or_enqueue_input(input_queue, msg_type, data):
                        await input_queue.put((msg_type, data))
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.debug(f"Control receive loop exception: {e}")
                break

    worker_task = asyncio.create_task(_run_input_worker(input_queue, lambda: client_active, rect_state))
    receiver_task = asyncio.create_task(receive_control_loop())

    try:
        _, pending = await asyncio.wait(
            [worker_task, receiver_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        client_active = False
        input_ctrl.release_all_inputs()
        logger.info("Client disconnected from control channel")


@app.websocket("/ws/stream")
async def websocket_stream_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    """Real-time video frame streaming (with in-flight ACK flow control) & fallback input control."""
    if not await _accept_authorized_ws(websocket, token):
        return

    logger.info("Client connected to live stream")
    stream_controller.record_reconnect()
    # Reset stale auto-degraded profile on fresh reconnect / link refresh
    if not stream_controller.manual_override and stream_controller.profile_index < 2:
        stream_controller.profile_index = 2
        stream_controller.degrade_counter = 0
        stream_controller.promote_counter = 0

    client_buffer = SingleSlotFrameBuffer()
    broadcaster.add_subscriber(client_buffer)

    input_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    client_active = True

    # End-to-end backpressure state: prevents tunnel bufferbloat without cutting image quality
    sent_frame_times: Dict[int, float] = {}
    ack_event = asyncio.Event()
    client_supports_ack = False

    curr_prof = stream_controller.current_profile
    broadcaster.update_config(fps=curr_prof.fps, quality=curr_prof.quality, scale=curr_prof.scale)

    rect_state: Dict[str, Any] = {
        "rect": await asyncio.to_thread(capture_engine.get_monitor_rect, broadcaster.monitor),
        "last_check": time.monotonic(),
    }

    async def client_frame_sender():
        nonlocal client_active
        while client_active:
            try:
                # Pace stream to MAX_IN_FLIGHT_FRAMES unacked frames so cloudflared never queues seconds of video
                while len(sent_frame_times) >= MAX_IN_FLIGHT_FRAMES and client_active:
                    ack_event.clear()
                    if len(sent_frame_times) < MAX_IN_FLIGHT_FRAMES:
                        break
                    try:
                        await asyncio.wait_for(ack_event.wait(), timeout=FRAME_ACK_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        # Evict oldest unacked frame so lost ACKs or non-ACKing clients never stall
                        if sent_frame_times:
                            oldest_id = next(iter(sent_frame_times))
                            sent_frame_times.pop(oldest_id, None)
                        break

                if not client_active:
                    break

                b_frame = await asyncio.to_thread(client_buffer.get_latest, 0.05)
                if b_frame is None:
                    continue

                t0 = time.monotonic()
                payload = b_frame.pack_with_header()
                await websocket.send_bytes(payload)
                send_ms = (time.monotonic() - t0) * 1000.0
                sent_frame_times[b_frame.frame_id] = time.monotonic()

                if not client_supports_ack:
                    stream_controller.record_delivery_time(send_ms)

                stream_controller.record_frame_sent(
                    size_bytes=len(payload),
                    capture_ms=b_frame.capture_time_ms,
                    encode_ms=b_frame.encode_time_ms,
                    proc_ms=send_ms,
                )

                if client_buffer.dropped_count > 0:
                    for _ in range(client_buffer.dropped_count):
                        stream_controller.record_frame_dropped()
                    client_buffer.dropped_count = 0

                await asyncio.sleep(0.001)

            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.debug(f"Frame sender loop exception: {e}")
                break

    async def receive_inputs_loop():
        nonlocal client_supports_ack
        while client_active:
            try:
                raw_data = await websocket.receive_text()
                try:
                    data = json.loads(raw_data)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                msg_type = data.get("type")

                if msg_type in ("ping", "config"):
                    await _handle_ping_or_config(websocket, msg_type, data, rect_state)

                elif msg_type == "frame_ack":
                    client_supports_ack = True
                    frame_id = data.get("id")
                    if frame_id is not None:
                        fid = int(frame_id)
                        sent_ts = sent_frame_times.pop(fid, None)
                        for stale_id in [k for k in sent_frame_times if k <= fid]:
                            sent_frame_times.pop(stale_id, None)
                        if sent_ts is not None:
                            e2e_ms = (time.monotonic() - sent_ts) * 1000.0
                            baseline_rtt = stream_controller.rtt_stats.avg()
                            # Feed excess queuing delay beyond baseline RTT so healthy links keep full HD quality
                            excess_ms = max(1.0, e2e_ms - baseline_rtt) if baseline_rtt > 0 else e2e_ms
                            stream_controller.record_delivery_time(excess_ms)
                        ack_event.set()
                    rtt_val = data.get("rtt")
                    if rtt_val:
                        stream_controller.record_rtt(float(rtt_val))

                elif msg_type:
                    if not _coalesce_or_enqueue_input(input_queue, msg_type, data):
                        await input_queue.put((msg_type, data))

            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.debug(f"Input receive loop exception: {e}")
                break

    sender_task = asyncio.create_task(client_frame_sender())
    worker_task = asyncio.create_task(_run_input_worker(input_queue, lambda: client_active, rect_state))
    receiver_task = asyncio.create_task(receive_inputs_loop())

    try:
        _, pending = await asyncio.wait(
            [sender_task, worker_task, receiver_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        client_active = False
        ack_event.set()
        broadcaster.remove_subscriber(client_buffer)
        client_buffer.clear()
        input_ctrl.release_all_inputs()
        logger.info("Client disconnected from live stream - resources freed")
