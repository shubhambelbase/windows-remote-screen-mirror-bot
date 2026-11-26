"""Integration tests for FastAPI server endpoints"""

import pytest
from fastapi.testclient import TestClient
from execution.server import app, SECRET_KEY


client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "resolution" in data


def test_system_info_endpoint():
    # Unauthenticated request must be rejected with 403 Forbidden
    unauth_resp = client.get("/api/system/info")
    assert unauth_resp.status_code == 403

    # Authenticated request with valid SECRET_KEY must succeed
    response = client.get(f"/api/system/info?token={SECRET_KEY}")
    assert response.status_code == 200
    data = response.json()
    assert "screen_size" in data
    assert "monitors" in data


def test_command_endpoint_auth_failure():
    response = client.post("/api/system/command", json={"token": "wrong_token", "action": "lock"})
    assert response.status_code == 403


def test_command_endpoint_unknown_action():
    response = client.post("/api/system/command", json={"token": SECRET_KEY, "action": "invalid_action"})
    assert response.status_code == 400


def test_index_page_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "Windows Remote" in response.text


def test_websocket_stream_and_inputs(monkeypatch):
    from execution.server import input_ctrl
    from execution.session_manager import activate_session

    # Stub actual OS key/click injection during automated test runs so Esc/click don't hit the active terminal
    async def noop_async(*args, **kwargs):
        return None

    monkeypatch.setattr(input_ctrl, "async_key_press", noop_async)
    monkeypatch.setattr(input_ctrl, "async_mouse_click", noop_async)
    monkeypatch.setattr(input_ctrl, "mouse_move_relative", lambda *a, **kw: None)
    monkeypatch.setattr(input_ctrl, "mouse_move_absolute", lambda *a, **kw: None)

    activate_session(1050690561)
    with client.websocket_connect(f"/ws/stream?token={SECRET_KEY}") as ws:
        # Fast-path ping/pong
        ws.send_json({"type": "ping", "t": 12345.0, "rtt": 45.0})
        pong_data = ws.receive_json()
        assert pong_data["type"] == "pong"
        assert pong_data["t"] == 12345.0
        assert "diag" in pong_data

        # Sequence of inputs
        ws.send_json({"type": "mouse_move", "dx": 10, "dy": 10, "sensitivity": 1.0})
        ws.send_json({"type": "mouse_move", "dx": 5, "dy": 5, "sensitivity": 1.0})
        ws.send_json({"type": "mouse_click", "button": "left"})
        ws.send_json({"type": "key_press", "key": "esc"})
        ws.send_json({"type": "direct_touch", "action": "move", "norm_x": 0.5, "norm_y": 0.5})
        ws.send_json({"type": "config", "profile": "poor"})
        ws.send_json({"type": "config", "profile": "auto"})


def test_websocket_standby_and_auth_rejected():
    from starlette.websockets import WebSocketDisconnect
    from execution.session_manager import deactivate_session

    # Bad token must be rejected
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/stream?token=invalid_token_xyz"):
            pass

    # Standby mode must be rejected even with valid token
    deactivate_session()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/stream?token={SECRET_KEY}"):
            pass


def test_webrtc_offer_requires_auth():
    resp = client.post("/api/webrtc/offer", json={"token": "bad", "sdp": "v=0", "type": "offer"})
    assert resp.status_code == 403


def test_index_has_no_store_cache_control():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("cache-control", "")


def test_websocket_control_channel():
    from starlette.websockets import WebSocketDisconnect
    from execution.session_manager import activate_session, deactivate_session

    deactivate_session()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/control?token={SECRET_KEY}"):
            pass

    activate_session(1050690561)
    with client.websocket_connect(f"/ws/control?token={SECRET_KEY}") as ctrl_ws:
        ctrl_ws.send_json({"type": "ping", "t": 999.0, "rtt": 32.0})
        pong = ctrl_ws.receive_json()
        assert pong["type"] == "pong"
        assert pong["t"] == 999.0
        assert "diag" in pong
        ctrl_ws.send_json({"type": "mouse_move", "dx": 4, "dy": -3, "sensitivity": 1.0})
        ctrl_ws.send_json({"type": "config", "profile": "good"})


def test_websocket_stream_frame_ack_flow_control():
    import struct
    from execution.session_manager import activate_session

    activate_session(1050690561)
    with client.websocket_connect(f"/ws/stream?token={SECRET_KEY}") as ws:
        frame1 = ws.receive_bytes()
        assert len(frame1) > 32
        assert frame1[:4] == b"TGFR"
        frame_id_1 = struct.unpack(">I", frame1[4:8])[0]

        ws.send_json({"type": "frame_ack", "id": frame_id_1})

        frame2 = ws.receive_bytes()
        assert len(frame2) > 32
        assert frame2[:4] == b"TGFR"
        frame_id_2 = struct.unpack(">I", frame2[4:8])[0]
        assert frame_id_2 >= frame_id_1
