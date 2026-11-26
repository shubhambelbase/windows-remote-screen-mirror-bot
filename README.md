<div align="center">

# 🖥️ Windows Remote Screen Mirror Bot

**Control and view your Windows 10/11 desktop in real time from any phone via Telegram Mini App.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-WebSockets-009688.svg)](https://fastapi.tiangolo.com/)
[![Platform Windows](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D6.svg)](https://microsoft.com/windows)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

*Dual-socket low-latency streaming • Trackpad & Direct Touch • Zero port-forwarding via Cloudflare Tunnel • Silent boot support*

</div>

---

## ✨ Highlights

- **⚡ Dual-Socket Low-Latency Architecture (`/ws/control` + `/ws/stream`)**: Isolates `ping`/`pong`, mouse, touch, and keyboard events on `/ws/control` so input never queues behind binary JPEG frames.
- **🛡️ Anti-Bufferbloat Frame Pacing (`MAX_IN_FLIGHT = 2`)**: Client `frame_ack` backpressure plus single-slot latest-frame buffers prevent tunnel queues from building multi-second lag while preserving sharp visual quality.
- **🎯 Static Frame Skipping**: Fast CRC32 pixel + cursor change detection skips redundant JPEG encodes when the desktop is idle (with a 1.5s keepalive refresh).
- **📱 Mobile-First Telegram Mini App UI**:
  - **Trackpad Mode** (cursor glide, tap-to-click, 2-finger scroll/right-click, side scroll strip, Drag Lock) & **Direct Touch Mode** (exact screen tap with visual tap rings)
  - **1× / 1.5× / 2.2× Zoom & Drag-to-Pan**
  - **Zen Fullscreen Mode** to collapse controls and maximize desktop viewing area
  - **Quality Preset Cards** (`Auto Adaptive`, `Ultra HD 1080p`, `Balanced 720p`, `Fast 540p`, `Low 360p`) & **PC Quick Actions** (`Toggle Taskbar`, `Lock Windows`)
  - **Virtual Keyboard Drawer** with quick shortcuts (`Tab`, `Esc`, `Ctrl+A/C/V`, `Win+V`, `Alt+Tab`) and live RTT/Jitter/FPS diagnostics
- **🔐 Standby & Whitelist Security**: Boots in dormant standby mode (no screen capture until `/start` in Telegram), fail-closed `ALLOWED_USER_ID` check, constant-time `SECRET_KEY` validation, and brute-force rate limiting.
- **🔄 Resilient Auto-Recovery**: Cloudflare Quick Tunnel watchdog auto-restarts dropped tunnels and notifies you in Telegram; Win32 single-instance mutex + `.bot.pid` prevents duplicate instances.

---

## 🏗️ Architecture

```
  Telegram Mini App (Phone / Mobile WebView)
     ├── /ws/control  (ping/pong, mouse, touch, keyboard, config)
     └── /ws/stream   (32B TGFR binary JPEG frames + frame_ack pacing)
            │
            ▼  (HTTPS / WSS via Cloudflare Quick Tunnel — no open router ports)
  ┌──────────────────────────────────────────────────────────────┐
  │                 FastAPI + Supervisor (main.py)               │
  │  ├── ScreenCaptureBroadcaster (MSS + CRC32 idle skip)        │
  │  ├── SingleSlotFrameBuffer (per-client latest frame only)    │
  │  ├── AdaptiveStreamController (hysteresis + manual lock)     │
  │  ├── InputController (Win32 SendInput + virtual-screen map)  │
  │  └── Telegram Bot (standby /start, /stop, /screenshot, /cmd) │
  └──────────────────────────────────────────────────────────────┘
```

---

## 🚀 Setup Guide (PowerShell)

### 1. Prerequisites
- **Windows 10 / 11 (64-bit)**
- **Python 3.11+** (`python --version`)
- **Cloudflare Tunnel (`cloudflared`)** installed (e.g. via `winget install Cloudflare.cloudflared`)
- A Telegram Bot Token from **`@BotFather`** and your numeric Telegram User ID from **`@userinfobot`**

### 2. Clone & Install Dependencies
Open **PowerShell** and run:

```powershell
git clone https://github.com/shubhambelbase/windows-remote-screen-mirror-bot.git
cd windows-remote-screen-mirror-bot

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. Configure `.env`
Create `.env` from the template and edit it with your credentials:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set at minimum:
```env
TELEGRAM_BOT_TOKEN=123456789:your_bot_token_here
ALLOWED_USER_ID=123456789
SECRET_KEY=replace_with_a_long_random_secret
```

### 4. Run the Bot
```powershell
.\.venv\Scripts\python.exe execution\main.py
```

Once running:
1. Open your bot in Telegram and send **`/start`**.
2. Tap **`🖥️ Open Remote Desktop`** to launch the live screen mirror Mini App.
3. When finished, send **`/stop`** to return the PC to dormant standby mode.

---

## ⚙️ Optional: Silent Auto-Start on Windows Boot

To start the bot silently in standby mode whenever you log into Windows (zero console windows, using `pythonw.exe`):

```powershell
Copy-Item .\silent_start.vbs "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\TelegramRemoteStudio.vbs"
```

To remove auto-start later:

```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\TelegramRemoteStudio.vbs"
```

---

## 🎮 Telegram Bot Commands

| Command | Description |
| :--- | :--- |
| **`/start`** | Activates remote session and sends the Mini App launch button. |
| **`/stop`** | Deactivates streaming and returns the server to dormant standby mode. |
| **`/status`** | Shows host CPU, RAM, active tunnel URL, and session status. |
| **`/screenshot`** | Captures and sends a full-quality desktop screenshot directly to chat. |
| **`/lock`** | Locks the Windows workstation immediately. |
| **`/cmd <command>`** | Executes a PowerShell command remotely and returns output. |

---

## 🧪 Running Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

*(Note: `tests/test_webrtc_experimental.py` is automatically skipped unless optional `aiortc` is installed).*

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
