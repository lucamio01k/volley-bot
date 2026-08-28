"""Operational incident tracking and Telegram alert deduplication."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Callable

import config
import database
from formatter import format_operational_error, format_recovery
from logger import get_logger
from telegram_sender import send_message

log = get_logger(__name__)
Sender = Callable[[str], bool]


def _safe_error(error: BaseException | str) -> str:
    raw = str(error)
    if "Traceback (most recent call last)" in raw:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        raw = lines[-1] if lines else error.__class__.__name__
    if re.search(r"<!doctype|<html|<body|</[a-z][^>]*>", raw, re.IGNORECASE):
        kind = error.__class__.__name__ if isinstance(error, BaseException) else "Errore"
        raw = f"{kind}: contenuto HTML omesso"
    if config.TELEGRAM_BOT_TOKEN:
        raw = raw.replace(config.TELEGRAM_BOT_TOKEN, "[redacted]")
    raw = re.sub(r"https?://\S+", "[url]", raw)
    raw = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[redacted]", raw)
    raw = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[redacted]", raw)
    raw = " ".join(raw.split())
    if raw:
        return raw[:220]
    if isinstance(error, BaseException):
        return error.__class__.__name__
    return "Unknown error"


def _fingerprint(component: str, safe_error: str) -> str:
    return hashlib.sha256(f"{component}|{safe_error}".encode("utf-8")).hexdigest()


def _duration(started_at: str | None) -> str:
    if not started_at:
        return "non disponibile"
    try:
        started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return "non disponibile"
    seconds = max(0, int((dt.datetime.now(dt.timezone.utc) - started).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def record_failure(
    component: str,
    error: BaseException | str,
    *,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> dict:
    target_db = db_path or config.DB_PATH
    safe = _safe_error(error)
    state = database.record_component_failure(
        target_db, component, _fingerprint(component, safe), safe
    )
    log.error("Component failure [%s] attempt %s: %s", component, state["failure_count"], safe)

    settings = config.section("operational_alerts")
    threshold = max(1, int(settings.get("failure_threshold", 3)))
    if not settings.get("enabled", True) or state["failure_count"] < threshold or state["alert_sent"]:
        return state

    message = format_operational_error(
        component,
        safe,
        int(state["failure_count"]),
        state.get("last_success_at_utc"),
    )
    sent = sender(message)
    if sent or component == "telegram":
        database.mark_component_alert_sent(target_db, component)
        state["alert_sent"] = 1
    return state


def record_success(
    component: str,
    *,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> None:
    target_db = db_path or config.DB_PATH
    previous = database.record_component_success(target_db, component)
    recovery_due = bool(
        previous
        and (previous.get("alert_sent") or previous.get("recovery_pending"))
    )
    if not recovery_due:
        return

    settings = config.section("operational_alerts")
    if not settings.get("enabled", True) or not settings.get("send_recovery", True):
        database.clear_recovery_pending(target_db, component)
        return

    if sender(format_recovery(component, _duration(previous.get("incident_started_at_utc")))):
        database.clear_recovery_pending(target_db, component)
