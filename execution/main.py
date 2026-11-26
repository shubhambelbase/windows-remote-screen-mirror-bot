"""Unified Application Runner & Production Supervisor.

Orchestrates the FastAPI server, Cloudflare tunnel (with auto-restart watchdog),
and Telegram bot with single-instance locking, rotating file logs, and crash recovery.
"""

import asyncio
import atexit
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
from typing import Optional

import psutil
import uvicorn

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.bot import (
    ALLOWED_USER_ID,
    build_bot_app,
    get_remote_keyboard,
    set_public_url,
)
from execution.config import CLOUDFLARED_PATH, HOST, PORT, TELEGRAM_BOT_TOKEN
from execution.server import app as fastapi_app
from execution.tunnel import CloudflareTunnel

LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "remote_studio.log"
DEFAULT_PID_FILE = BASE_DIR / ".bot.pid"
PID_FILE = DEFAULT_PID_FILE

logger = logging.getLogger("MainRunner")
_logging_configured = False
_mutex_handle: Optional[int] = None


def configure_logging() -> None:
    """Configures rotating file logs and console stream handler (safe under pythonw.exe)."""
    global _logging_configured
    if _logging_configured:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    logging.raiseExceptions = False

    # Under pythonw.exe (Windows Startup), sys.stdout/sys.stderr are None; bind to devnull
    # so libraries such as uvicorn/click never crash inspecting .isatty().
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if sys.stderr is not None and hasattr(sys.stderr, "write") and getattr(sys.stderr, "name", "") != os.devnull:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _logging_configured = True


def check_single_instance() -> bool:
    """Prevents duplicate server/bot instances using a Win32 mutex and PID lock file."""
    global _mutex_handle

    is_default_pid = os.path.abspath(PID_FILE) == os.path.abspath(DEFAULT_PID_FILE)
    if os.name == "nt" and is_default_pid:
        try:
            k32 = ctypes.windll.kernel32
            handle = k32.CreateMutexW(None, False, "Local\\WindowsRemoteScreenMirrorStudio")
            if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                if handle:
                    k32.CloseHandle(handle)
                logger.warning("Another instance of Windows Remote Studio is already running.")
                return False
            _mutex_handle = handle
        except Exception:
            pass

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid() and psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                cmdline = " ".join(proc.cmdline() or []).lower()
                if "python" in proc.name().lower() and "main.py" in cmdline:
                    logger.warning(f"Another instance is already running with PID {old_pid}.")
                    return False
        except Exception:
            pass

    os.makedirs(os.path.dirname(os.path.abspath(PID_FILE)), exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    def _cleanup():
        global _mutex_handle
        try:
            if os.path.exists(PID_FILE):
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    if f.read().strip() == str(os.getpid()):
                        os.remove(PID_FILE)
        except Exception:
            pass
        if _mutex_handle and os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(_mutex_handle)
            except Exception:
                pass
            _mutex_handle = None

    atexit.register(_cleanup)
    return True


async def _tunnel_watchdog(tunnel: CloudflareTunnel, bot_app, stop_event: asyncio.Event):
    """Monitors Cloudflare tunnel health and auto-restarts if cloudflared drops."""
    fail_streak = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10.0)
            break
        except asyncio.TimeoutError:
            pass

        if not tunnel.is_alive():
            fail_streak += 1
            backoff = min(60, 5 * (2 ** min(fail_streak - 1, 3)))
            logger.warning(f"Cloudflare tunnel process died; restarting (attempt #{fail_streak})...")
            new_url = await asyncio.to_thread(tunnel.restart, 25.0)
            if new_url:
                fail_streak = 0
                set_public_url(new_url)
                if ALLOWED_USER_ID > 0:
                    try:
                        await bot_app.bot.send_message(
                            chat_id=ALLOWED_USER_ID,
                            text=(
                                "🔄 **Cloudflare Tunnel Reconnected**\n"
                                "Your Remote Studio link has been refreshed automatically."
                            ),
                            parse_mode="Markdown",
                            reply_markup=get_remote_keyboard(),
                        )
                    except Exception as notify_err:
                        logger.debug(f"Could not notify user of tunnel reconnect: {notify_err}")
            else:
                logger.warning(f"Tunnel restart failed; waiting {backoff}s before retry.")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
        else:
            fail_streak = 0


async def run_services():
    """Starts FastAPI, Cloudflare tunnel + watchdog, and Telegram bot concurrently."""
    loop = asyncio.get_running_loop()

    def _suppress_proactor_reset(l: asyncio.AbstractEventLoop, ctx: dict):
        exc = ctx.get("exception")
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return
        l.default_exception_handler(ctx)

    loop.set_exception_handler(_suppress_proactor_reset)

    logger.info("==================================================")
    logger.info(" Starting Windows Remote Control Studio")
    logger.info(f" Host: {HOST}:{PORT}")
    logger.info(f" Allowed Telegram User ID: {ALLOWED_USER_ID}")
    logger.info("==================================================")

    tunnel = CloudflareTunnel(port=PORT, cloudflared_path=CLOUDFLARED_PATH)
    logger.info("Establishing secure Cloudflare Tunnel...")
    public_url = await asyncio.to_thread(tunnel.start, 25.0)

    if public_url:
        logger.info(f"✅ Cloudflare Tunnel Active: {public_url}")
        set_public_url(public_url)
    else:
        fallback_url = f"http://{HOST}:{PORT}"
        logger.warning(f"⚠️ Cloudflare tunnel unavailable. Falling back to local URL: {fallback_url}")
        set_public_url(fallback_url)

    bot_app = build_bot_app()

    config = uvicorn.Config(
        app=fastapi_app,
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)

    stop_watchdog = asyncio.Event()
    watchdog_task: Optional[asyncio.Task] = None

    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(
            poll_interval=0.5,
            timeout=20,
            bootstrap_retries=-1,
            drop_pending_updates=False,
        )
        logger.info("🤖 Telegram Bot Polling started.")

        watchdog_task = asyncio.create_task(_tunnel_watchdog(tunnel, bot_app, stop_watchdog))
        await server.serve()

    except asyncio.CancelledError:
        logger.info("Service cancellation requested.")
    finally:
        logger.info("Shutting down services...")
        stop_watchdog.set()
        if watchdog_task and not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except Exception:
                pass
        try:
            if bot_app.updater and bot_app.updater.running:
                await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
        except Exception as e:
            logger.debug(f"Bot shutdown note: {e}")

        tunnel.stop()
        logger.info("✅ All services stopped safely.")


def main():
    configure_logging()

    if not check_single_instance():
        print("\n[!] Windows Remote Studio is already running.")
        sys.exit(2)

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing in .env")
        sys.exit(1)

    failures = 0
    while True:
        cycle_start = time.time()
        try:
            asyncio.run(run_services())
            logger.info("Windows Remote Studio exited normally.")
            break
        except (KeyboardInterrupt, SystemExit):
            logger.info("Stopped by user or system signal.")
            break
        except Exception as e:
            if time.time() - cycle_start > 60:
                failures = 0
            failures += 1
            delay = min(60, 2 ** min(failures, 5))
            logger.error(
                f"Service loop crashed unexpectedly: {e}. Restarting in {delay}s (attempt #{failures})...",
                exc_info=True,
            )
            time.sleep(delay)


if __name__ == "__main__":
    main()
