"""Scheduled and CLI task orchestration."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from typing import Callable
from zoneinfo import ZoneInfo

import alerts
import config
import database
from cev_client import CEVParseError
from formatter import (
    format_daily,
    format_new_fixture,
    format_reminder,
    format_result,
    format_schedule_change,
    format_weekly,
)
from logger import get_logger
from models import CompetitionConfig, MatchRecord, ProviderBatch
from providers import create_provider
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


def _sync_interval_minutes(
    competition: CompetitionConfig,
    now: dt.datetime | None = None,
    db_path: str | None = None,
) -> int:
    settings = config.section("source_refresh")
    reference = now or utc_now()
    if competition.provider == "api_sports" and db_path:
        matchday_interval = max(
            1, int(config.section("api_sports").get("matchday_interval_minutes", 120))
        )
        for match in database.list_matches(db_path, config.TRACKED_TEAM, include_final=False):
            if match.competition_key != competition.key:
                continue
            scheduled = _parse_utc(match.scheduled_at_utc)
            if scheduled and reference <= scheduled <= reference + dt.timedelta(hours=24):
                return matchday_interval
    if _competition_active(competition, now):
        value = competition.active_interval_minutes
        return max(1, int(value if value is not None else settings.get("active_interval_minutes", 15)))
    value = competition.inactive_interval_minutes
    return max(1, int(value if value is not None else settings.get("inactive_interval_minutes", 360)))


def _component(competition: CompetitionConfig) -> str:
    return f"{competition.provider}:{competition.key}"


def _api_quota_available(
    competition: CompetitionConfig,
    db_path: str,
    sender: Sender,
) -> bool:
    if competition.provider != "api_sports":
        return True
    reserve = max(0, int(config.section("api_sports").get("quota_reserve", 10)))
    today = utc_now().date().isoformat()
    states = [
        database.get_provider_sync_state(db_path, _component(competition)),
        database.get_provider_sync_state(db_path, f"result:{_component(competition)}"),
    ]
    remaining_values = [
        int(state["quota_remaining"])
        for state in states
        if state
        and state.get("requests_date") == today
        and state.get("quota_remaining") is not None
    ]
    if not remaining_values or min(remaining_values) > reserve:
        return True
    alerts.record_failure(
        "quota:api_sports",
        f"Quota API-Sports alla riserva minima di {reserve} richieste",
        db_path=db_path,
        sender=sender,
    )
    return False


def _sync_due(
    competition: CompetitionConfig,
    now: dt.datetime | None = None,
    db_path: str | None = None,
) -> bool:
    target_db = db_path or config.DB_PATH
    state = database.get_provider_sync_state(target_db, _component(competition))
    if not state:
        state = database.get_component_state(target_db, _component(competition))
    if not state or not state.get("last_success_at_utc"):
        return True
    last = _parse_utc(state["last_success_at_utc"])
    interval = dt.timedelta(minutes=_sync_interval_minutes(competition, now, target_db))
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
    if not force and not database.claim_notification_send(
        target_db,
        dedupe_key,
        notification_type,
        stable_key,
    ):
        return False
    if not sender(text):
        if not force:
            database.release_notification_claim(target_db, dedupe_key)
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
    source_state = database.get_component_state(db_path, _component(competition))
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
    client: object | None = None,
    sender: Sender = send_message,
) -> dict[str, int]:
    target_db = db_path or config.DB_PATH
    database.init_db(target_db)
    timeout = int(config.section("source_refresh").get("request_timeout_seconds", 20))
    totals: dict[str, int] = {}
    provider_cache: dict[str, object] = {}

    for competition in config.COMPETITIONS:
        if not competition.enabled:
            continue
        component = _component(competition)
        if not _api_quota_available(competition, target_db, sender):
            continue
        if not force and not _sync_due(competition, now, target_db):
            log.debug("Schedule sync not due: %s", competition.key)
            continue
        provider = client
        if provider is None:
            provider = provider_cache.get(competition.provider)
            if provider is None:
                provider = create_provider(competition, timeout)
                provider_cache[competition.provider] = provider
        request_count = 0
        try:
            if hasattr(provider, "fetch_schedule"):
                batch = provider.fetch_schedule(competition)
            else:
                batch = ProviderBatch(matches=provider.fetch_competition(competition))
            matches = batch.matches
            request_count = batch.request_count
            cached_count = sum(
                match.competition_key == competition.key
                for match in database.list_matches(target_db)
            )
            if competition.provider == "cev" and cached_count and len(matches) < cached_count:
                raise CEVParseError(
                    f"CEV import incomplete: {len(matches)} of {cached_count} cached slots"
                )
            changes = database.upsert_matches(target_db, matches, config.TRACKED_TEAM)
        except Exception as exc:
            request_count = max(request_count, int(getattr(provider, "request_count", 0)))
            database.record_provider_sync(
                target_db,
                component,
                success=False,
                request_count=request_count,
                quota_remaining=getattr(provider, "quota_remaining", None),
            )
            alerts.record_failure(component, exc, db_path=target_db, sender=sender)
            _record_stale_cache_failure(
                competition,
                now=now,
                db_path=target_db,
                sender=sender,
            )
            continue

        totals[competition.key] = len(matches)
        next_due = (now or utc_now()) + dt.timedelta(
            minutes=_sync_interval_minutes(competition, now, target_db)
        )
        database.record_provider_sync(
            target_db,
            component,
            success=True,
            request_count=request_count,
            quota_remaining=batch.quota_remaining,
            payload_hash=batch.payload_hash,
            record_count=len(matches),
            next_due_at_utc=next_due.replace(microsecond=0).isoformat(),
        )
        alerts.record_success(component, db_path=target_db, sender=sender)
        alerts.record_success(
            f"cache:{competition.key}", db_path=target_db, sender=sender
        )
        log.info("Imported %d matches from %s", len(matches), competition.key)
        for warning in batch.warnings:
            log.warning("Provider warning [%s]: %s", component, warning)

        if not notify:
            continue
        new_settings = config.section("new_fixture_notification")
        change_settings = config.section("schedule_change_notification")
        lookahead = int(new_settings.get("lookahead_days", 7))
        for change in changes:
            match = change.match
            if (
                change.became_final
                and change.previous is not None
                and match.involves(config.TRACKED_TEAM)
            ):
                if config.section("result_notification").get("enabled", True):
                    _send_result_notification(
                        match,
                        db_path=target_db,
                        sender=sender,
                    )
                continue
            if (
                change.became_tracked
                or (change.is_new and match.involves(config.TRACKED_TEAM))
            ) and new_settings.get("enabled", True):
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


def _send_result_notification(
    match: MatchRecord,
    *,
    db_path: str,
    sender: Sender,
    force: bool = False,
) -> bool:
    score_key = f"{match.home_sets}-{match.away_sets}:" + ",".join(
        f"{home}-{away}" for home, away in match.set_scores
    )
    return _send_once(
        f"result:{match.stable_key}:{score_key}",
        "result",
        format_result(match),
        stable_key=match.stable_key,
        force=force,
        db_path=db_path,
        sender=sender,
    )


def _matches_between(
    start_local: dt.datetime,
    end_local: dt.datetime,
    db_path: str | None = None,
) -> list[MatchRecord]:
    start_utc = start_local.astimezone(dt.timezone.utc)
    end_utc = end_local.astimezone(dt.timezone.utc)
    result: list[MatchRecord] = []
    for match in database.list_matches(db_path or config.DB_PATH, config.TRACKED_TEAM):
        if match.status in {"cancelled", "postponed"}:
            continue
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
        if match.status in {"cancelled", "postponed"}:
            continue
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
    client: object | None = None,
    sender: Sender = send_message,
) -> list[str]:
    settings = config.section("result_notification")
    if not settings.get("enabled", True) and not force:
        return []
    reference = now or utc_now()
    start_after = dt.timedelta(minutes=int(settings.get("start_polling_minutes_after_start", 90)))
    incident_after = dt.timedelta(hours=int(settings.get("incident_after_hours", 48)))
    stop_after = dt.timedelta(days=int(settings.get("stop_polling_after_days", 7)))
    target_db = db_path or config.DB_PATH
    timeout = int(config.section("source_refresh").get("request_timeout_seconds", 20))
    messages: list[str] = []
    due_by_competition: dict[str, list[MatchRecord]] = defaultdict(list)
    competition_by_key = {
        item.key: item for item in config.COMPETITIONS if item.enabled
    }

    for match in database.list_matches(target_db, config.TRACKED_TEAM, include_final=False):
        if match.status in {"cancelled", "postponed"}:
            continue
        scheduled = _parse_utc(match.scheduled_at_utc)
        if not scheduled or reference > scheduled + stop_after:
            continue
        if match.status != "result_available" and reference < scheduled + start_after:
            continue
        next_check = _parse_utc(match.next_result_check_at_utc)
        if next_check and reference < next_check:
            continue
        if match.competition_key in competition_by_key:
            due_by_competition[match.competition_key].append(match)

    provider_cache: dict[str, object] = {}
    for competition_key, due_matches in due_by_competition.items():
        competition = competition_by_key[competition_key]
        if not _api_quota_available(competition, target_db, sender):
            continue
        provider = client
        if provider is None:
            provider = provider_cache.get(competition.provider)
            if provider is None:
                provider = create_provider(competition, timeout)
                provider_cache[competition.provider] = provider
        group_component = f"result:{_component(competition)}"
        try:
            if hasattr(provider, "fetch_results"):
                batch = provider.fetch_results(competition, due_matches)
            else:
                batch = ProviderBatch(
                    matches=[provider.fetch_match_detail(match) for match in due_matches]
                )
        except Exception as exc:
            database.record_provider_sync(
                target_db,
                group_component,
                success=False,
                request_count=int(getattr(provider, "request_count", 0)),
                quota_remaining=getattr(provider, "quota_remaining", None),
            )
            alerts.record_failure(group_component, exc, db_path=target_db, sender=sender)
            for match in due_matches:
                attempts = match.result_poll_attempts + 1
                next_check = reference + dt.timedelta(minutes=min(60, 15 * (2 ** min(attempts - 1, 2))))
                database.update_result_poll_state(
                    target_db,
                    match,
                    next_check_at_utc=next_check.replace(microsecond=0).isoformat(),
                    attempts=attempts,
                )
            continue
        database.record_provider_sync(
            target_db,
            group_component,
            success=True,
            request_count=batch.request_count,
            quota_remaining=batch.quota_remaining,
            payload_hash=batch.payload_hash,
            record_count=len(batch.matches),
        )
        alerts.record_success(group_component, db_path=target_db, sender=sender)
        changes = database.upsert_matches(target_db, batch.matches, config.TRACKED_TEAM)
        updated = {change.match.stable_key: change.match for change in changes}

        for match in due_matches:
            detailed = updated.get(match.stable_key, match)
            if detailed.status == "final":
                database.update_result_poll_state(
                    target_db, detailed, next_check_at_utc=None, attempts=0
                )
                message = format_result(detailed)
                messages.append(message)
                if send:
                    _send_result_notification(
                        detailed,
                        db_path=target_db,
                        sender=sender,
                        force=force,
                    )
                continue

            attempts = match.result_poll_attempts + 1
            scheduled = _parse_utc(match.scheduled_at_utc)
            delay_minutes = min(60, 15 * (2 ** min(attempts - 1, 2)))
            if scheduled and reference > scheduled + incident_after:
                delay_minutes = 360
                alerts.record_failure(
                    f"result:{match.stable_key}",
                    f"Risultato non disponibile entro {int(incident_after.total_seconds() // 3600)} ore",
                    db_path=target_db,
                    sender=sender,
                )
            next_check = reference + dt.timedelta(minutes=delay_minutes)
            database.update_result_poll_state(
                target_db,
                match,
                next_check_at_utc=next_check.replace(microsecond=0).isoformat(),
                attempts=attempts,
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
