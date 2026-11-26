"""Telegram Bot Controller.

Provides Telegram Mini App launcher, fail-closed whitelist authorization,
non-blocking system telemetry, desktop snapshots, and power/shell commands.
"""

import asyncio
import io
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.config import ALLOWED_USER_ID, PORT, SECRET_KEY, TELEGRAM_BOT_TOKEN
from execution.input_controller import InputController
from execution.screen_capture import ScreenCaptureEngine
from execution.session_manager import activate_session, deactivate_session, is_session_active

logger = logging.getLogger("TelegramBot")

TOKEN = TELEGRAM_BOT_TOKEN

capture_engine = ScreenCaptureEngine()
input_ctrl = InputController()

CURRENT_PUBLIC_URL: Optional[str] = None


def set_public_url(url: str):
    """Sets or updates the active public HTTPS tunnel URL."""
    global CURRENT_PUBLIC_URL
    CURRENT_PUBLIC_URL = url
    logger.info(f"Bot WebApp URL set to: {CURRENT_PUBLIC_URL}")


def is_authorized(user_id: int) -> bool:
    """Fail-closed authorization: verifies user matches ALLOWED_USER_ID (> 0)."""
    if ALLOWED_USER_ID <= 0:
        return False
    return user_id == ALLOWED_USER_ID


def get_remote_keyboard() -> InlineKeyboardMarkup:
    """Generates an organized, polished inline keyboard for the bot."""
    web_url = CURRENT_PUBLIC_URL or f"http://127.0.0.1:{PORT}"
    app_url = f"{web_url}/?token={SECRET_KEY}"

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 Open Live Screen Studio",
                web_app=WebAppInfo(url=app_url),
            )
        ],
        [
            InlineKeyboardButton("📸 Screenshot", callback_data="btn_screenshot"),
            InlineKeyboardButton("🪟 Toggle Taskbar", callback_data="btn_taskbar"),
        ],
        [
            InlineKeyboardButton("🔇 Mute / Unmute", callback_data="btn_mute"),
            InlineKeyboardButton("📊 System Telemetry", callback_data="btn_status"),
        ],
        [
            InlineKeyboardButton("🔒 Lock", callback_data="btn_lock"),
            InlineKeyboardButton("💤 Sleep", callback_data="btn_sleep"),
        ],
        [
            InlineKeyboardButton("🛑 Standby / Stop", callback_data="btn_deactivate"),
            InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="btn_refresh"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_dashboard_text() -> str:
    """Builds a formatted, sleek dashboard status card."""
    comp_name = os.environ.get("COMPUTERNAME", "Windows Laptop")
    w, h = input_ctrl.get_screen_size()
    cx, cy = input_ctrl.get_cursor_pos()
    active_win = input_ctrl.get_active_window_title().replace("`", "'")
    battery = input_ctrl.get_battery_status()

    bat_icon = "🔌" if battery["charging"] else "🔋"
    tunnel_status = "🟢 Secure (Cloudflare HTTPS)" if CURRENT_PUBLIC_URL else "🟡 Local Network"
    session_status = "🟢 Active (Ready for Streaming)" if is_session_active() else "🟡 Standby (Dormant)"

    return (
        f"🖥️ **WINDOWS REMOTE STUDIO**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💻 **Host Machine:** `{comp_name}`\n"
        f"⚡ **Session:** `{session_status}`\n"
        f"🪟 **Active Window:** `{active_win}`\n"
        f"🖥️ **Display:** `{w}x{h}` • Cursor: `({cx}, {cy})`\n"
        f"{bat_icon} **Power:** `{battery['percent']}% ({battery['status']})`\n"
        f"🌐 **Link:** `{tunnel_status}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✨ *Tap below to launch real-time screen mirror with touch controls:* \n"
    )


async def _capture_snapshot_data():
    """Offloads screenshot and telemetry gathering so the event loop never stalls."""
    frame_bytes = await asyncio.to_thread(capture_engine.capture_screenshot)
    w, h = await asyncio.to_thread(input_ctrl.get_screen_size)
    cx, cy = await asyncio.to_thread(input_ctrl.get_cursor_pos)
    active_win = (await asyncio.to_thread(input_ctrl.get_active_window_title)).replace("`", "'")
    return frame_bytes, w, h, cx, cy, active_win


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start and /remote commands. Explicitly activates remote capture and controls."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    if not is_authorized(user.id):
        logger.warning(f"Unauthorized access attempt by user {user.id}")
        await msg.reply_text("⛔ **Access Denied:** Private Laptop Control Bot.")
        return

    activate_session(user.id)
    dashboard = await asyncio.to_thread(format_dashboard_text)

    await msg.reply_text(
        "🟢 **Remote Studio Activated!**\n"
        "Screen capture and controls are now unlocked.\n\n" + dashboard,
        parse_mode="Markdown",
        reply_markup=get_remote_keyboard(),
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivates remote session, returning the laptop to dormant standby."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    deactivate_session()
    await asyncio.to_thread(input_ctrl.release_all_inputs)
    await msg.reply_text(
        "🔴 **Remote Studio Deactivated.**\n"
        "Screen capture and controls are now locked in dormant standby mode.\n"
        "Send /start whenever you want to activate remote control again.",
        parse_mode="Markdown",
    )


async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures and sends a full-resolution desktop screenshot."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    status_msg = await msg.reply_text("📸 Capturing crystal-clear screenshot...")
    try:
        frame_bytes, w, h, cx, cy, active_win = await _capture_snapshot_data()
        bio = io.BytesIO(frame_bytes)
        bio.name = "screenshot.jpg"
        bio.seek(0)

        await msg.reply_photo(
            photo=bio,
            caption=(
                f"🖥️ **Desktop Snapshot** (`{w}x{h}`)\n"
                f"🪟 **Window:** `{active_win}`\n"
                f"📍 **Cursor:** `({cx}, {cy})`"
            ),
            parse_mode="Markdown",
            reply_markup=get_remote_keyboard(),
            read_timeout=60,
            write_timeout=60,
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Screenshot error: {e}")
        await status_msg.edit_text(f"❌ Failed to capture screenshot: {e}")


async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Locks Windows desktop workstation."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    if os.name == "nt":
        import ctypes
        await asyncio.to_thread(ctypes.windll.user32.LockWorkStation)
    await msg.reply_text("🔒 **Windows Laptop Locked!**", parse_mode="Markdown")


async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Puts Windows laptop into S3 Standby / Sleep mode."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    await msg.reply_text("💤 **Putting Windows Laptop into Sleep Mode (S3)...**", parse_mode="Markdown")
    if os.name == "nt":
        import ctypes
        await asyncio.to_thread(ctypes.windll.powrprof.SetSuspendState, 0, 1, 0)


async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Safely shuts down the Windows laptop with safety confirmation."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    arg = (context.args[0] if context.args else "").lower()
    if arg in ["now", "confirm", "yes"]:
        await msg.reply_text("🔌 **Shutting down Windows Laptop now... Goodbye!**", parse_mode="Markdown")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(["shutdown", "/s", "/t", "5"], shell=False, creationflags=creationflags)
    else:
        await msg.reply_text(
            "⚠️ **Shutdown Confirmation Required**\n\n"
            "To confirm shutting down the laptop remotely, send:\n"
            "`/shutdown now` or `/shutdown confirm`\n\n"
            "*(Note: Once shut down, you will need physical access or configured Wake-on-LAN/smart plug to power it back on).* ",
            parse_mode="Markdown",
        )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Safely restarts the Windows laptop."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    arg = (context.args[0] if context.args else "").lower()
    if arg in ["now", "confirm", "yes"]:
        await msg.reply_text(
            "🔄 **Restarting Windows Laptop in 5 seconds...**\n*The bot will automatically resume after boot!*",
            parse_mode="Markdown",
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(["shutdown", "/r", "/t", "5"], shell=False, creationflags=creationflags)
    else:
        await msg.reply_text(
            "⚠️ **Restart Confirmation Required**\n\n"
            "To confirm rebooting the laptop remotely, send:\n"
            "`/restart now` or `/restart confirm`\n\n"
            "*(Because auto-startup is installed, the bot will reconnect automatically once Windows boots up).* ",
            parse_mode="Markdown",
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies with live host stats."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    dashboard = await asyncio.to_thread(format_dashboard_text)
    await msg.reply_text(
        dashboard,
        parse_mode="Markdown",
        reply_markup=get_remote_keyboard(),
    )


def _run_ps_sync(full_cmd: str):
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", full_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=creationflags,
    )


async def cmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes a PowerShell command on the laptop without blocking the event loop."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_authorized(user.id):
        return

    if not context.args:
        await msg.reply_text("Usage: `/cmd <powershell command>`", parse_mode="Markdown")
        return

    full_cmd = " ".join(context.args)
    status_msg = await msg.reply_text(f"⏳ Executing: `{full_cmd.replace('`', chr(39))}`...", parse_mode="Markdown")

    try:
        proc = await asyncio.to_thread(_run_ps_sync, full_cmd)
        out = (proc.stdout or proc.stderr or "Command executed with no output.").strip()
        if len(out) > 3500:
            out = out[:3500] + "\n... [truncated]"

        safe_out = out.replace("```", "'''")
        await status_msg.edit_text(
            f"💻 **Output (Exit code {proc.returncode}):**\n```\n{safe_out}\n```",
            parse_mode="Markdown",
        )
    except subprocess.TimeoutExpired:
        await status_msg.edit_text("⏱️ Command timed out after 15 seconds.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks."""
    query = update.callback_query
    if not query:
        return

    user = update.effective_user
    if not user or not is_authorized(user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return

    data = query.data or ""

    if data == "btn_screenshot":
        await query.answer("Capturing screenshot...")
        try:
            frame_bytes, _, _, _, _, active_win = await _capture_snapshot_data()
            bio = io.BytesIO(frame_bytes)
            bio.name = "screenshot.jpg"
            bio.seek(0)
            await query.message.reply_photo(
                photo=bio,
                caption=f"🖥️ **Desktop Snapshot**\n🪟 Active: `{active_win}`",
                parse_mode="Markdown",
                reply_markup=get_remote_keyboard(),
                read_timeout=60,
                write_timeout=60,
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Screenshot failed: {e}")

    elif data == "btn_taskbar":
        current = await asyncio.to_thread(input_ctrl.is_taskbar_visible)
        await asyncio.to_thread(input_ctrl.set_taskbar_visible, not current)
        state_str = "Shown (Visible)" if not current else "Closed (Hidden)"
        await query.answer(f"🪟 Windows Taskbar is now {state_str}!", show_alert=True)

    elif data == "btn_window":
        active_win = await asyncio.to_thread(input_ctrl.get_active_window_title)
        await query.answer(f"🪟 Focused: {active_win}", show_alert=True)

    elif data == "btn_mute":
        await input_ctrl.async_key_press("volumemute")
        await query.answer("🔇 Mute toggled!", show_alert=False)

    elif data == "btn_lock":
        if os.name == "nt":
            import ctypes
            await asyncio.to_thread(ctypes.windll.user32.LockWorkStation)
        await query.answer("🔒 Laptop Locked!", show_alert=True)
        await query.message.reply_text("🔒 **Windows Laptop Locked!**", parse_mode="Markdown")

    elif data == "btn_sleep":
        await query.answer("💤 Putting laptop into Sleep mode...", show_alert=True)
        await query.message.reply_text("💤 **Windows Laptop is entering Sleep mode (S3)...**", parse_mode="Markdown")
        if os.name == "nt":
            import ctypes
            await asyncio.to_thread(ctypes.windll.powrprof.SetSuspendState, 0, 1, 0)

    elif data == "btn_deactivate":
        deactivate_session()
        await asyncio.to_thread(input_ctrl.release_all_inputs)
        await query.answer("🛑 Remote Studio returned to Standby mode.", show_alert=True)
        try:
            dashboard = await asyncio.to_thread(format_dashboard_text)
            await query.edit_message_text(
                dashboard,
                parse_mode="Markdown",
                reply_markup=get_remote_keyboard(),
            )
        except Exception:
            pass

    elif data == "btn_status":
        await query.answer()
        dashboard = await asyncio.to_thread(format_dashboard_text)
        await query.message.reply_text(
            dashboard,
            parse_mode="Markdown",
            reply_markup=get_remote_keyboard(),
        )

    elif data == "btn_refresh":
        await query.answer("Refreshed")
        try:
            dashboard = await asyncio.to_thread(format_dashboard_text)
            await query.edit_message_text(
                dashboard,
                parse_mode="Markdown",
                reply_markup=get_remote_keyboard(),
            )
        except Exception:
            pass
    else:
        await query.answer()


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Suppresses noisy offline stack traces while surfacing unexpected handler exceptions."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"Transient Telegram network error: {err}")
        return
    logger.error("Unhandled Telegram bot exception:", exc_info=err)
    if isinstance(update, Update) and update.effective_message and err is not None:
        try:
            await update.effective_message.reply_text(f"⚠️ Internal error: {str(err)[:250]}")
        except Exception:
            pass


def build_bot_app() -> Application:
    """Builds and configures the telegram bot application with resilient timeouts."""
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured in .env")

    request_config = HTTPXRequest(
        connect_timeout=25.0,
        read_timeout=35.0,
        write_timeout=35.0,
        pool_timeout=20.0,
        connection_pool_size=12,
    )
    app = Application.builder().token(TOKEN).request(request_config).build()

    app.add_handler(CommandHandler(["start", "remote"], start_command))
    app.add_handler(CommandHandler(["stop", "standby"], stop_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("lock", lock_command))
    app.add_handler(CommandHandler("sleep", sleep_command))
    app.add_handler(CommandHandler("shutdown", shutdown_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cmd", cmd_command))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_error_handler(global_error_handler)

    return app
