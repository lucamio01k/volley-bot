"""Scheduled and CLI task orchestration."""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Callable
from zoneinfo import ZoneInfo

import alerts
import config
import database
from cev_client import CEVClient, CEVParseError
from formatter import (
    format_daily,
    format_new_fixture,
    format_reminder,
    format_result,
    format_schedule_change,
    format_weekly,
)
from logger import get_logger
from models import CompetitionConfig, MatchRecord
from telegram_sender import send_message, test_connection

log = get_logger(__name__)
Sender = Callable[[str], bool]
SYNC_DUE_TOLERANCE = dt.timedelta(minutes=2)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _local_now(now: dt.datetime | None = None) -> dt.datetime:
    reference = now or utc_now()
    return reference.astimezone(ZoneInfo(config.BOT_TIMEZONE))


def _competition_active(competition: CompetitionConfig, now: dt.datetime | None = None) -> bool:
    today = _local_now(now).date()
    return dt.date.fromisoformat(competition.active_from) <= today <= dt.date.fromisoformat(
        competition.active_to
    )


def _sync_interval_minutes(competition: CompetitionConfig, now: dt.datetime | None = None) -> int:
    settings = config.section("source_refresh")
    if _competition_active(competition, now):
        return max(1, int(settings.get("active_interval_minutes", 15)))
    return max(1, int(settings.get("inactive_interval_minutes", 360)))


def _sync_due(
    competition: CompetitionConfig,
    now: dt.datetime | None = None,
    db_path: str | None = None,
) -> bool:
    state = database.get_component_state(db_path or config.DB_PATH, f"cev:{competition.key}")
    if not state or not state.get("last_success_at_utc"):
        return True
    last = _parse_utc(state["last_success_at_utc"])
    interval = dt.timedelta(minutes=_sync_interval_minutes(competition, now))
    # The scheduler interval starts before the HTTP fetch, while last_success is
    # stored after it completes. Without a small tolerance, a successful fetch
    # lasting a few seconds makes the following hourly run appear "too early"
    # and the effective refresh cadence becomes two hours.
    return not last or (now or utc_now()) - last + SYNC_DUE_TOLERANCE >= interval


def _send_once(
    dedupe_key: str,
    notification_type: str,
    text: str,
    *,
    stable_key: str | None = None,
    force: bool = False,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> bool:
    target_db = db_path or config.DB_PATH
    if not force and database.notification_sent(target_db, dedupe_key):
        return False
    if not sender(text):
        return False
    database.mark_notification_sent(target_db, dedupe_key, notification_type, stable_key)
    return True


def _notification_hash(match: MatchRecord) -> str:
    raw = "|".join(
        [
            match.home_team,
            match.away_team,
            match.scheduled_at_utc or "",
            match.venue,
            match.phase,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_upcoming_within(match: MatchRecord, days: int, now: dt.datetime | None = None) -> bool:
    scheduled = _parse_utc(match.scheduled_at_utc)
    if not scheduled:
        return False
    reference = now or utc_now()
    return reference <= scheduled <= reference + dt.timedelta(days=days)


def _record_stale_cache_failure(
    competition: CompetitionConfig,
    *,
    now: dt.datetime | None,
    db_path: str,
    sender: Sender,
) -> None:
    source_state = database.get_component_state(db_path, f"cev:{competition.key}")
    last_success = _parse_utc(
        source_state.get("last_success_at_utc") if source_state else None
    )
    if not last_success:
        return
    reference = now or utc_now()
    max_age = dt.timedelta(
        hours=max(1, int(config.section("source_refresh").get("max_cache_age_hours", 12)))
    )
    age = reference - last_success
    if age <= max_age:
        return
    max_age_hours = max(1, int(max_age.total_seconds() // 3600))
    alerts.record_failure(
        f"cache:{competition.key}",
        f"Cache troppo vecchia oltre la soglia di {max_age_hours} ore",
        db_path=db_path,
        sender=sender,
    )


def sync_schedule(
    *,
    force: bool = False,
    notify: bool = True,
    now: dt.datetime | None = None,
    db_path: str | None = None,
    client: CEVClient | None = None,
    sender: Sender = send_message,
) -> dict[str, int]:
    target_db = db_path or config.DB_PATH
    database.init_db(target_db)
    timeout = int(config.section("source_refresh").get("request_timeout_seconds", 20))
    provider = client or CEVClient(timeout_seconds=timeout)
    totals: dict[str, int] = {}

    for competition in config.COMPETITIONS:
        component = f"cev:{competition.key}"
        if not force and not _sync_due(competition, now, target_db):
            log.debug("Schedule sync not due: %s", competition.key)
            continue
        try:
            matches = provider.fetch_competition(competition)
            cached_count = sum(
                match.competition_key == competition.key
                for match in database.list_matches(target_db)
            )
            if cached_count and len(matches) < cached_count:
                raise CEVParseError(
                    f"CEV import incomplete: {len(matches)} of {cached_count} cached slots"
                )
            changes = database.upsert_matches(target_db, matches, config.TRACKED_TEAM)
        except Exception as exc:
            alerts.record_failure(component, exc, db_path=target_db, sender=sender)
            _record_stale_cache_failure(
                competition,
                now=now,
                db_path=target_db,
                sender=sender,
            )
            continue

        totals[competition.key] = len(matches)
        alerts.record_success(component, db_path=target_db, sender=sender)
        alerts.record_success(
            f"cache:{competition.key}", db_path=target_db, sender=sender
        )
        log.info("Imported %d matches from %s", len(matches), competition.key)

        if not notify:
            continue
        new_settings = config.section("new_fixture_notification")
        change_settings = config.section("schedule_change_notification")
        lookahead = int(new_settings.get("lookahead_days", 7))
        for change in changes:
            match = change.match
            if change.became_tracked and new_settings.get("enabled", True):
                if _is_upcoming_within(match, lookahead, now):
                    key = f"new-fixture:{match.stable_key}:{_notification_hash(match)}"
                    _send_once(
                        key,
                        "new_fixture",
                        format_new_fixture(match, config.BOT_TIMEZONE),
                        stable_key=match.stable_key,
                        db_path=target_db,
                        sender=sender,
                    )
                continue
            if change.schedule_changed and change_settings.get("enabled", True):
                if _is_upcoming_within(match, 30, now):
                    key = f"schedule-change:{match.stable_key}:{_notification_hash(match)}"
                    _send_once(
                        key,
                        "schedule_change",
                        format_schedule_change(match, config.BOT_TIMEZONE),
                        stable_key=match.stable_key,
                        db_path=target_db,
                        sender=sender,
                    )
    return totals


def _matches_between(
    start_local: dt.datetime,
    end_local: dt.datetime,
    db_path: str | None = None,
) -> list[MatchRecord]:
    start_utc = start_local.astimezone(dt.timezone.utc)
    end_utc = end_local.astimezone(dt.timezone.utc)
    result: list[MatchRecord] = []
    for match in database.list_matches(db_path or config.DB_PATH, config.TRACKED_TEAM):
        scheduled = _parse_utc(match.scheduled_at_utc)
        if scheduled and start_utc <= scheduled < end_utc:
            result.append(match)
    return sorted(result, key=lambda item: item.scheduled_at_utc or "")


def build_weekly_message(
    reference_date: dt.date | None = None,
    db_path: str | None = None,
) -> tuple[str, dt.date]:
    local_tz = ZoneInfo(config.BOT_TIMEZONE)
    reference = reference_date or dt.datetime.now(local_tz).date()
    start_date = reference - dt.timedelta(days=reference.weekday())
    end_date_exclusive = start_date + dt.timedelta(days=7)
    start = dt.datetime.combine(start_date, dt.time.min, local_tz)
    end = dt.datetime.combine(end_date_exclusive, dt.time.min, local_tz)
    matches = _matches_between(start, end, db_path)
    return format_weekly(matches, start_date, end_date_exclusive - dt.timedelta(days=1), config.BOT_TIMEZONE), start_date


def task_send_weekly(
    *,
    reference_date: dt.date | None = None,
    force: bool = False,
    send: bool = True,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> str:
    message, start_date = build_weekly_message(reference_date, db_path)
    settings = config.section("weekly_digest")
    if send and (settings.get("enabled", True) or force):
        _send_once(
            f"weekly:{start_date.isoformat()}",
            "weekly",
            message,
            force=force,
            db_path=db_path,
            sender=sender,
        )
    return message


def build_daily_message(
    reference_date: dt.date | None = None,
    db_path: str | None = None,
) -> tuple[str, dt.date, list[MatchRecord]]:
    local_tz = ZoneInfo(config.BOT_TIMEZONE)
    date = reference_date or dt.datetime.now(local_tz).date()
    start = dt.datetime.combine(date, dt.time.min, local_tz)
    end = start + dt.timedelta(days=1)
    matches = _matches_between(start, end, db_path)
    return format_daily(matches, date, config.BOT_TIMEZONE), date, matches


def task_send_daily(
    *,
    reference_date: dt.date | None = None,
    force: bool = False,
    send: bool = True,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> str:
    message, date, matches = build_daily_message(reference_date, db_path)
    settings = config.section("daily_digest")
    should_send = bool(matches) or settings.get("send_when_empty", False)
    if send and should_send and (settings.get("enabled", True) or force):
        _send_once(
            f"daily:{date.isoformat()}",
            "daily",
            message,
            force=force,
            db_path=db_path,
            sender=sender,
        )
    return message


def task_send_reminders(
    *,
    now: dt.datetime | None = None,
    force: bool = False,
    send: bool = True,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> list[str]:
    settings = config.section("match_reminder")
    if not settings.get("enabled", True) and not force:
        return []
    hours = max(1, int(settings.get("hours_before", 2)))
    reference = now or utc_now()
    messages: list[str] = []
    for match in database.list_matches(db_path or config.DB_PATH, config.TRACKED_TEAM, include_final=False):
        scheduled = _parse_utc(match.scheduled_at_utc)
        if not scheduled:
            continue
        reminder_at = scheduled - dt.timedelta(hours=hours)
        if not (reminder_at <= reference < scheduled):
            continue
        message = format_reminder(match, hours, config.BOT_TIMEZONE)
        messages.append(message)
        if send:
            key = f"reminder:{match.stable_key}:{match.scheduled_at_utc}:{hours}"
            _send_once(
                key,
                "reminder",
                message,
                stable_key=match.stable_key,
                force=force,
                db_path=db_path,
                sender=sender,
            )
    return messages


def task_send_results(
    *,
    now: dt.datetime | None = None,
    force: bool = False,
    send: bool = True,
    db_path: str | None = None,
    client: CEVClient | None = None,
    sender: Sender = send_message,
) -> list[str]:
    settings = config.section("result_notification")
    if not settings.get("enabled", True) and not force:
        return []
    reference = now or utc_now()
    start_after = dt.timedelta(minutes=int(settings.get("start_polling_minutes_after_start", 45)))
    max_delay = dt.timedelta(hours=int(settings.get("max_delay_hours", 5)))
    target_db = db_path or config.DB_PATH
    provider = client or CEVClient(
        timeout_seconds=int(config.section("source_refresh").get("request_timeout_seconds", 20))
    )
    messages: list[str] = []

    for match in database.list_matches(target_db, config.TRACKED_TEAM, include_final=False):
        scheduled = _parse_utc(match.scheduled_at_utc)
        if not scheduled or reference < scheduled + start_after:
            continue
        if reference > scheduled + max_delay + dt.timedelta(hours=1):
            continue
        component = f"result:{match.stable_key}"
        if reference > scheduled + max_delay:
            alerts.record_failure(
                component,
                f"Risultato non disponibile entro {int(max_delay.total_seconds() // 3600)} ore",
                db_path=target_db,
                sender=sender,
            )
            continue
        try:
            detailed = provider.fetch_match_detail(match)
        except Exception as exc:
            alerts.record_failure(component, exc, db_path=target_db, sender=sender)
            continue
        alerts.record_success(component, db_path=target_db, sender=sender)
        database.upsert_matches(target_db, [detailed], config.TRACKED_TEAM)
        if detailed.status != "final":
            continue
        message = format_result(detailed)
        messages.append(message)
        if send:
            score_key = f"{detailed.home_sets}-{detailed.away_sets}:" + ",".join(
                f"{home}-{away}" for home, away in detailed.set_scores
            )
            _send_once(
                f"result:{detailed.stable_key}:{score_key}",
                "result",
                message,
                stable_key=detailed.stable_key,
                force=force,
                db_path=target_db,
                sender=sender,
            )
    return messages


def task_telegram_health(
    *,
    db_path: str | None = None,
    sender: Sender = send_message,
) -> bool:
    if test_connection():
        alerts.record_success("telegram", db_path=db_path, sender=sender)
        return True
    alerts.record_failure("telegram", "Connessione Telegram non disponibile", db_path=db_path, sender=sender)
    return False
