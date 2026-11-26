# SOP: Telegram Windows Remote Control & Screen Mirror System

## Purpose
Provides an ultra-low-latency live screen mirroring and remote control system for a Windows laptop accessible via Telegram Bot and Telegram Mini App (TMA).

## Architecture & Layers
- **Layer 1 (Directive):** `directives/remote_control_system.md`
- **Layer 2 (Orchestration):** Agent / CLI runner orchestrating host services and tunnel.
- **Layer 3 (Execution Scripts):**
  - `execution/server.py`: FastAPI + WebSocket server streaming screen frames and receiving input actions.
  - `execution/screen_capture.py`: Ultra-fast multi-monitor screen capture engine with dynamic JPEG quality compression.
  - `execution/input_controller.py`: Native Windows `SendInput` and `pynput` mouse/keyboard dispatcher.
  - `execution/tunnel.py`: Cloudflare Tunnel supervisor providing a secure public HTTPS/WSS URL.
  - `execution/bot.py`: Telegram bot providing Mini App launcher, authorization checks, and system chat commands.
  - `execution/web/`: Mobile-first responsive Telegram Mini App frontend (HTML/CSS/JS Canvas).

## Key Workflows
1. **Host Server Startup:**
   - Load `.env` configuration (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_ID`, `PORT`, `SECRET_KEY`).
   - Initialize screen capture with primary display geometry.
   - Start FastAPI HTTP & WebSocket server.
2. **Tunnel Lifecycle:**
   - Launch `cloudflared` to establish an HTTPS quick-tunnel to the local port.
   - Extract the public `https://*.trycloudflare.com` URL.
   - Pass the live URL to the Telegram bot.
3. **Telegram Bot Interaction:**
   - User sends `/start` or `/remote` to the bot.
   - Verify `user_id == ALLOWED_USER_ID`. If mismatch, reject and log.
   - Return an inline keyboard button with `web_app=WebAppInfo(url=f"{tunnel_url}?token={auth_token}")`.
   - Provide fallback commands: `/screenshot`, `/lock`, `/vol`, `/cmd`.
4. **Live Stream & Input Loop:**
   - Client connects via WebSocket (`/ws/stream?token=...`).
   - Server streams frames at target FPS (e.g. 20-30 FPS, adaptive queue drop to avoid buffer latency).
   - Client sends JSON input packets:
     - `mouse_move` (relative `dx, dy` or absolute coordinates)
     - `mouse_click` (`left`, `right`, `double`, `down`, `up`)
     - `mouse_wheel` (`delta_y`, `delta_x`)
     - `key_down`, `key_up`, `key_press`, `hotkey`
   - Input controller dispatches directly to Windows user32 SendInput.

## Operational Safeguards & Edge Cases
- **Safety & Whitelist:** Never allow unauthorized Telegram IDs to connect.
- **Network Lag / Frame Drop:** If WebSocket buffer exceeds threshold, drop older frames to preserve real-time live interaction without accumulation delay.
- **DPI Scaling:** Calculate client canvas to server screen coordinates using host scale factors.

## Learnings (2026-11-25)
- `ScreenCaptureEngine` must hold `mss.MSS()` per-thread (`threading.local`) so background broadcaster threads and asyncio `to_thread` snapshots never hit cross-thread Win32 GDI handle errors or silently fall back to slow `ImageGrab`.
- `ScreenCaptureBroadcaster` uses per-worker `threading.Event` stop signals so rapid WebSocket reconnects never spawn duplicate capture threads.
- `AdaptiveStreamController` locks `manual_override = True` when the user selects a fixed quality preset and clears it only when `"auto"` is chosen.
- `main.py` enforces single-instance locking, rotating logs (`logs/remote_studio.log`), and a Cloudflare tunnel watchdog that restarts `cloudflared` and updates the Telegram dashboard link if the tunnel process dies.
