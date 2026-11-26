"""Unit tests for Telegram Bot helper functions"""

import pytest
from execution.bot import is_authorized, set_public_url, get_remote_keyboard, ALLOWED_USER_ID, SECRET_KEY


def test_authorization_check():
    # Owner should be authorized
    assert is_authorized(ALLOWED_USER_ID) is True

    # Unknown user should be rejected (assuming ALLOWED_USER_ID is set)
    if ALLOWED_USER_ID != 0:
        assert is_authorized(999999999) is False


def test_remote_keyboard_generation():
    test_url = "https://test-tunnel.trycloudflare.com"
    set_public_url(test_url)

    keyboard = get_remote_keyboard()
    assert keyboard is not None
    assert len(keyboard.inline_keyboard) >= 2

    # Verify WebApp button exists and points to tunnel URL with token
    web_app_btn = keyboard.inline_keyboard[0][0]
    assert web_app_btn.web_app is not None
    assert web_app_btn.web_app.url.startswith(test_url)
    assert f"token={SECRET_KEY}" in web_app_btn.web_app.url


def test_authorization_fail_closed_when_zero(monkeypatch):
    from execution import bot

    monkeypatch.setattr(bot, "ALLOWED_USER_ID", 0)
    assert bot.is_authorized(123456) is False
    assert bot.is_authorized(0) is False


def test_bot_commands_async_execution(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from execution import bot

    monkeypatch.setattr(bot, "ALLOWED_USER_ID", 1050690561)
    monkeypatch.setattr(bot.capture_engine, "capture_screenshot", lambda: b"\xff\xd8\xff\xe0fakejpg")

    async def _run():
        msg = MagicMock()
        msg.reply_text = AsyncMock()
        msg.reply_photo = AsyncMock()
        status_msg = MagicMock()
        status_msg.delete = AsyncMock()
        status_msg.edit_text = AsyncMock()
        msg.reply_text.return_value = status_msg

        upd = SimpleNamespace(
            effective_user=SimpleNamespace(id=1050690561),
            effective_message=msg,
        )
        ctx = SimpleNamespace(args=[])
        await bot.screenshot_command(upd, ctx)
        assert msg.reply_photo.await_count == 1
        assert status_msg.delete.await_count == 1

    asyncio.run(_run())
