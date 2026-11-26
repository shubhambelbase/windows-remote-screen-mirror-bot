# AGENT CONTEXT: Telegram Windows Remote Control & Screen Mirror Studio

## Overview
Real-time Windows desktop screen mirror and remote control system operated via Telegram Bot and Telegram Mini App (TMA). Uses a single-slot latest-frame buffer, thread-safe MSS capture, adaptive quality profiles with manual override locking, native Win32 input controls, Cloudflare Tunnel with auto-restart watchdog, and experimental WebRTC support.

## Tech Stack & Architecture
- **Language & Runtime:** Python 3.11.9 (`.venv`), Windows 11 64-bit
- **Configuration & Desktop:**
  - `execution/config.py`: Centralized `.env` loader (`HOST`, `PORT=8765`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_ID`, `CLOUDFLARED_PATH`, and ephemeral fallback `SECRET_KEY` if unset).
  - `execution/win32_desktop.py`: Shared `threading.local()` `WinSta0\Default` desktop attachment with explicit handle cleanup.
- **Streaming Pipeline:**
  - `SingleSlotFrameBuffer`: Latest-frame-only single-slot buffer per client preventing socket bufferbloat.
  - `ScreenCaptureEngine` & `ScreenCaptureBroadcaster`: Thread-local `mss.MSS()` instances, CRC32 + cursor idle-frame skipping (`capture_frame_if_changed`, 1.5s keepalive), GDI cursor overlay, 32B binary header (`TGFR`), and per-worker `threading.Event` stop signals.
  - `AdaptiveStreamController`: Multi-profile controller (`excellent`, `good`, `moderate`, `poor`) with hysteresis and `manual_override` lock (cleared on `auto`).
- **Input Pipeline:**
  - `InputController`: Virtual-screen multi-monitor bounds, module-level Win32 ctypes structs, `SendInput` with fallback, and safety release.
- **Transport, Server & Bot:**
  - `execution/server.py`: FastAPI + dual-WebSocket architecture (`/ws/stream` for `TGFR` frames capped at `MAX_IN_FLIGHT_FRAMES=2` via `frame_ack` backpressure; `/ws/control` dedicated to `ping`/`pong`, `config`, and inputs so control never queues behind video frames), constant-time `SECRET_KEY` auth, sliding-window rate guard, async `cmd` offloading, FastAPI `lifespan` cleanup, and monitor-aware `direct_touch` mapping.
  - `execution/tunnel.py`: `CloudflareTunnel` with lowercase/interleaved log regex parsing, `is_alive()`, and `restart()`.
  - `execution/bot.py`: Fail-closed whitelist (`ALLOWED_USER_ID > 0`), async-offloaded screenshots & commands, `global_error_handler`.
  - `execution/main.py`: Unified supervisor with rotating logs (`logs/remote_studio.log`), `pythonw.exe` `os.devnull` stdio + `uvicorn(log_config=None)` safety, Win32 single-instance mutex + `.bot.pid`, Proactor `WinError 10054` suppression, and Cloudflare tunnel auto-reconnect watchdog.
  - `execution/web/index.html`: Upgraded dark mobile workstation TMA UI (`100dvh`, vector SVG icon bar, Zen fullscreen collapse toggle, visual Direct Touch tap rings, zero-lag zoom drag panning, tactile preset cards, PC Quick Actions `taskbar`/`lock`, quick-key keyboard drawer strip, toast feedback, dual-socket `/ws/control` + `/ws/stream` with `frame_ack`).

## Repository Layout
- `directives/remote_control_system.md` - Operational SOP & learnings
- `execution/` - `config.py`, `win32_desktop.py`, `main.py`, `bot.py`, `server.py`, `screen_capture.py`, `adaptive_controller.py`, `input_controller.py`, `session_manager.py`, `tunnel.py`, `webrtc_streamer.py`, `benchmark.py`, `web/index.html`
- `tests/` - `test_adaptive_streaming.py`, `test_bot.py`, `test_input_controller.py`, `test_screen_capture.py`, `test_server.py`, `test_single_slot_buffer.py`, `test_tunnel_and_supervisor.py`, `test_webrtc_experimental.py`
- `requirements.txt` - Pinned minimum dependencies (`fastapi`, `uvicorn`, `websockets`, `python-telegram-bot`, `mss`, `pillow`, `psutil`, `pynput`, `httpx`, `pytest`)
- `start_remote.bat`, `silent_start.vbs`, `install_startup.bat`, `uninstall_startup.bat`

## How to Run & Test
- Run tests: `.\.venv\Scripts\python.exe -m pytest -v` (`37 passed, 1 skipped` when optional `aiortc` is absent)
- Launch manually: `.\.venv\Scripts\python.exe execution\main.py` (or `start_remote.bat`)
- Windows Auto-Startup: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TelegramRemoteStudio.vbs`

## Current Status
- Status: Anti-bufferbloat low-latency upgrade completed: static idle-frame skip (`capture_frame_if_changed`), `MAX_IN_FLIGHT_FRAMES=2` `frame_ack` pacing on `/ws/stream`, and dedicated `/ws/control` WebSocket keeping RTT/inputs unblocked at full quality. Verified pytest suite + restarted live daemon.
- Last Updated: 2026-11-25
