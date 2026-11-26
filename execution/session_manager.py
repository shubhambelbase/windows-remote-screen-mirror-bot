"""Session Manager
Coordinates remote control and screen capture activation state between Telegram Bot and Server.
Ensures screen capture and controls are dormant on startup until explicitly activated by the
authorized Telegram user via /start.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_session_active: bool = False
_session_user_id: Optional[int] = None


def is_session_active() -> bool:
    """Returns True if remote streaming and controls are actively enabled."""
    return _session_active


def activate_session(user_id: int):
    """Activates the remote desktop session for the authorized Telegram user."""
    global _session_active, _session_user_id
    _session_active = True
    _session_user_id = user_id
    logger.info(f"🟢 Remote control session activated by Telegram user {user_id}")


def deactivate_session():
    """Deactivates remote session, returning the system to dormant standby."""
    global _session_active, _session_user_id
    _session_active = False
    _session_user_id = None
    logger.info("🔴 Remote control session deactivated - standby mode")


def get_active_user_id() -> Optional[int]:
    """Returns the user ID of the current active session, if any."""
    return _session_user_id
