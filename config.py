"""Load credentials from .env and behavior from settings.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from models import CompetitionConfig

load_dotenv()


def _load_settings() -> dict[str, Any]:
    path = Path(os.getenv("SETTINGS_PATH", "settings.json"))
    if not path.exists():
        raise FileNotFoundError(f"Settings file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("settings.json must contain a JSON object")
    return data


SETTINGS_PATH = os.getenv("SETTINGS_PATH", "settings.json")
DB_PATH = os.getenv("DB_PATH", "data/volleybot.db")
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "data/volleybot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
API_SPORTS_VOLLEYBALL_KEY = os.getenv("API_SPORTS_VOLLEYBALL_KEY", "").strip()

SETTINGS = _load_settings()
BOT_TIMEZONE = str(SETTINGS.get("bot_timezone", "Europe/Rome"))
ZoneInfo(BOT_TIMEZONE)
TRACKED_TEAM = str(SETTINGS.get("tracked_team", "ITALY")).strip().upper()


def section(name: str) -> dict[str, Any]:
    value = SETTINGS.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"settings.json section '{name}' must be an object")
    return value


COMPETITIONS = [CompetitionConfig(**item) for item in SETTINGS.get("competitions", [])]
if not COMPETITIONS:
    raise ValueError("At least one competition must be configured")
