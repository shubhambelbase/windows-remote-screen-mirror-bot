"""Shared configuration loader for Windows Remote Control Studio."""

import logging
import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DOTENV_PATH = BASE_DIR / ".env"
load_dotenv(DOTENV_PATH)

HOST: str = os.getenv("HOST", "127.0.0.1")
PORT: int = int(os.getenv("PORT", "8765"))
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CLOUDFLARED_PATH: str | None = os.getenv("CLOUDFLARED_PATH") or None


def _parse_allowed_user_id() -> int:
    raw = os.getenv("ALLOWED_USER_ID", "0").strip()
    try:
        return int(raw)
    except ValueError:
        logger.error("Invalid ALLOWED_USER_ID in .env; defaulting to 0 (locked).")
        return 0


ALLOWED_USER_ID: int = _parse_allowed_user_id()


def _load_or_generate_secret_key() -> str:
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    generated = secrets.token_urlsafe(32)
    logger.warning("SECRET_KEY not set in .env; generated ephemeral runtime secret token.")
    return generated


SECRET_KEY: str = _load_or_generate_secret_key()
