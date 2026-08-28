"""Minimal Telegram Bot API client."""

from __future__ import annotations

import time

import requests

import config
from logger import get_logger

log = get_logger(__name__)


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHANNEL_ID)


def _base_url() -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def send_message(
    text: str,
    channel_id: str | None = None,
    disable_notification: bool = False,
) -> bool:
    if not configured():
        log.error("Telegram is not configured; check TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID")
        return False
    payload = {
        "chat_id": channel_id or config.TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": disable_notification,
    }
    try:
        response = requests.post(f"{_base_url()}/sendMessage", json=payload, timeout=12)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Telegram send failed: %s", exc.__class__.__name__)
        return False
    if not data.get("ok"):
        log.error("Telegram API rejected message: %s", data.get("description", "unknown error"))
        return False
    time.sleep(0.5)
    return True


def test_connection() -> bool:
    if not configured():
        log.error("Telegram is not configured")
        return False
    try:
        response = requests.get(f"{_base_url()}/getMe", timeout=12)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Telegram connection test failed: %s", exc.__class__.__name__)
        return False
    if data.get("ok"):
        log.info("Telegram connected as @%s", data.get("result", {}).get("username", "unknown"))
        return True
    log.error("Telegram token rejected: %s", data.get("description", "unknown error"))
    return False

